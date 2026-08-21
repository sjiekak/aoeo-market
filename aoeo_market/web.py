"""Market intelligence website (stdlib-only web server).

Serves the dashboard single-page app from :mod:`aoeo_market.static` and a
JSON API over the snapshot database written by ``fetch --store``::

    uv run python -m aoeo_market.web --db market.db --port 8000

Endpoints
---------
``GET /``                      the dashboard page
``GET /api/overview``          snapshot stats, supply history, price histogram,
                               type/rarity breakdown, top price movers
``GET /api/listings``          active listings of the latest snapshot
                               (``type``, ``q``, ``sort``, ``dir`` params)
``GET /api/item/<item_id>``    current listings + full price history of one item
``GET /api/not-on-sale``       historical items with no active listing right now
                               (``order``, ``dir`` params)
``GET /api/best-sellers``      items ranked by observed time-to-sale (fastest
                               sellers first; ``order``, ``dir``, ``min_sales``)
``GET /api/recently-removed``  listings that vanished between the last two
                               snapshots, classified EXPIRED vs REMOVED

The server only ever reads the database (each request opens its own WAL
connection), so it can run side by side with the cron fetch that writes it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import store

STATIC_DIR = Path(__file__).with_name("static")
_STATIC_FILES = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}

_JSON = "application/json; charset=utf-8"


class _BadParam(ValueError):
    """Malformed query parameter — reported as HTTP 400."""


class WebApp:
    """Routing + JSON API over one snapshot database."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def handle(self, path: str, query: dict[str, list[str]] | None = None) -> tuple[int, str, bytes]:
        """Route one GET and return ``(status, content_type, body)``."""
        query = query or {}
        try:
            if path in ("/", "/index.html"):
                return 200, _STATIC_FILES["index.html"], (STATIC_DIR / "index.html").read_bytes()
            if path.startswith("/static/"):
                name = path[len("/static/") :]
                if name not in _STATIC_FILES:
                    return self._error(404, f"no static file {name!r}")
                return 200, _STATIC_FILES[name], (STATIC_DIR / name).read_bytes()
            if path == "/api/overview":
                return self._json(store.market_overview(self._conn()))
            if path == "/api/listings":
                return self._json(
                    store.active_listings(
                        self._conn(),
                        item_type=query.get("type", [None])[0] or None,
                        q=query.get("q", [None])[0] or None,
                        sort=query.get("sort", ["price"])[0],
                        direction=query.get("dir", ["asc"])[0],
                    )
                )
            if path == "/api/not-on-sale":
                return self._json(
                    store.items_not_on_sale(
                        self._conn(),
                        order=query.get("order", ["median_price"])[0],
                        direction=query.get("dir", ["desc"])[0],
                    )
                )
            if path == "/api/best-sellers":
                return self._json(
                    store.best_sellers(
                        self._conn(),
                        order=query.get("order", ["median_time"])[0],
                        direction=query.get("dir", ["asc"])[0],
                        min_sales=self._int_param(query, "min_sales", 1),
                    )
                )
            if path == "/api/recently-removed":
                return self._json(store.recently_removed(self._conn()))
            if path.startswith("/api/item/"):
                item_id = urllib.parse.unquote(path[len("/api/item/") :])
                history = store.price_history(self._conn(), item_id)
                if history is None:
                    return self._error(404, f"item {item_id!r} was never observed")
                return self._json(history)
            return self._error(404, f"no route for {path!r}")
        except _BadParam as exc:
            return self._error(400, str(exc))
        except sqlite3.Error as exc:
            return self._error(500, f"database error: {exc}")
        except OSError as exc:
            return self._error(500, f"io error: {exc}")

    def _conn(self) -> sqlite3.Connection:
        return store.open_store(self.db_path)

    @staticmethod
    def _int_param(query: dict[str, list[str]], name: str, default: int) -> int:
        raw = query.get(name, [None])[0]
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            raise _BadParam(f"{name} must be an integer") from None

    def _json(self, payload) -> tuple[int, str, bytes]:
        return 200, _JSON, json.dumps(payload).encode()

    def _error(self, status: int, message: str) -> tuple[int, str, bytes]:
        return status, _JSON, json.dumps({"error": message}).encode()


class _Handler(BaseHTTPRequestHandler):
    app: WebApp

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        status, ctype, body = self.app.handle(parsed.path, urllib.parse.parse_qs(parsed.query))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        super().log_message(fmt, *args)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="aoeo_market.web",
        description="Serve the market intelligence dashboard over the snapshot database.",
    )
    p.add_argument("--db", default="market.db", help="SQLite snapshot database (default market.db)")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    args = p.parse_args(argv)

    if not Path(args.db).exists():
        print(f"warning: {args.db} does not exist yet; run `fetch --store` to create it", file=sys.stderr)

    _Handler.app = WebApp(args.db)
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"Serving AoEO market dashboard on http://{args.host}:{args.port} (db: {args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
