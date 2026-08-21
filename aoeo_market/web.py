"""Market intelligence website (stdlib-only web server).

Serves the dashboard single-page app from :mod:`aoeo_market.static` and a
JSON API over the snapshot database.  This process is the **single owner** of
the DuckDB file: the ``fetch --store`` CLI posts each snapshot to
``POST /api/snapshot`` instead of touching the database itself, so exactly
one component ever opens the file (read-write, per request)::

    uv run python -m aoeo_market.web --db market.db --port 8000
    uv run python -m aoeo_market.cli fetch --store http://127.0.0.1:8000

That split maps directly onto Kubernetes: the web app runs as a StatefulSet
pod owning the database volume, and ``fetch --store <url>`` runs as a
CronJob that only needs network access to the service.

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
``GET /api/best-value``        items ranked by value for their rarity (cheapest
                               relative to their tier; ``order``, ``dir``,
                               ``include_unrated``)
``GET /api/recently-removed``  listings that vanished between the last two
                               snapshots, classified EXPIRED vs REMOVED
``POST /api/snapshot``         append one snapshot: JSON body
                               ``{"listings": [<Listing.to_dict()>…],
                               "captured_at": <unix seconds, optional>}``
                               -> ``{"snapshot_id": id, "listings": n}``

The write endpoint is unauthenticated: keep the server on a private network
or protect it with a reverse proxy when it is reachable beyond localhost.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import duckdb

from . import store
from .market import Listing

STATIC_DIR = Path(__file__).with_name("static")
_STATIC_FILES = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}

_JSON = "application/json; charset=utf-8"

_LISTING_FIELDS = (
    "transaction_id",
    "seller_empire_id",
    "buyer_character_id",
    "item_id",
    "item_type",
    "item_level",
    "item_count",
    "item_price",
    "item_seed",
    "seconds_till_expiry",
)


class _BadParam(ValueError):
    """Malformed query parameter — reported as HTTP 400."""


class WebApp:
    """Routing + JSON API over one snapshot database (sole writer)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        # DuckDB forbids mixing read-only and read-write connections to the
        # same file in one process, so every connection here is read-write;
        # this process is the single owner of the file by design.  The lock
        # serializes snapshot writes between request threads.
        self._write_lock = threading.Lock()

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
                        order=query.get("order", ["median_unit_price"])[0],
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
            if path == "/api/best-value":
                return self._json(
                    store.best_value(
                        self._conn(),
                        order=query.get("order", ["value_ratio"])[0],
                        direction=query.get("dir", ["desc"])[0],
                        include_unrated=self._int_param(query, "include_unrated", 0) == 1,
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
        except duckdb.Error as exc:
            return self._error(500, f"database error: {exc}")
        except OSError as exc:
            return self._error(500, f"io error: {exc}")

    def handle_post(self, path: str, body: bytes) -> tuple[int, str, bytes]:
        """Route one POST (the snapshot write API) and return the response."""
        if path != "/api/snapshot":
            return self._error(404, f"no route for {path!r}")
        try:
            payload = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return self._error(400, f"invalid JSON: {exc}")
        if not isinstance(payload, dict) or not isinstance(payload.get("listings"), list):
            return self._error(400, 'payload must be {"listings": [...]}')
        captured_at = payload.get("captured_at")
        if captured_at is not None and (isinstance(captured_at, bool) or not isinstance(captured_at, (int, float))):
            return self._error(400, "captured_at must be unix seconds or null")
        try:
            listings = [self._validate_listing(d, i) for i, d in enumerate(payload["listings"])]
        except (TypeError, ValueError) as exc:
            return self._error(400, str(exc))
        try:
            with self._write_lock:
                conn = store.open_store(self.db_path)
                try:
                    snapshot_id = store.record_snapshot(conn, listings, captured_at)
                finally:
                    conn.close()
        except (OSError, duckdb.Error) as exc:
            return self._error(500, f"database error: {exc}")
        return 201, _JSON, json.dumps({"snapshot_id": snapshot_id, "listings": len(listings)}).encode()

    @staticmethod
    def _validate_listing(raw: dict, index: int) -> Listing:
        if not isinstance(raw, dict):
            raise TypeError(f"listings[{index}] must be an object")
        fields: dict[str, int | str] = {}
        for name in _LISTING_FIELDS:
            if name not in raw:
                raise ValueError(f"listings[{index}] is missing {name!r}")
            value = raw[name]
            if name in ("item_id", "item_type"):
                if not isinstance(value, str):
                    raise ValueError(f"listings[{index}].{name} must be a string")
                fields[name] = value
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"listings[{index}].{name} must be an integer")
                fields[name] = int(value)
        return Listing(**fields)  # type: ignore[arg-type]

    def _conn(self) -> duckdb.DuckDBPyConnection:
        # Before the first snapshot, serve the empty state from an in-memory
        # schema instead of erroring.
        if not Path(self.db_path).exists():
            return store.open_memory()
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
        self._respond(status, ctype, body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        parsed = urllib.parse.urlsplit(self.path)
        status, ctype, resp = self.app.handle_post(parsed.path, body)
        self._respond(status, ctype, resp)

    def _respond(self, status: int, ctype: str, body: bytes) -> None:
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
    p.add_argument("--db", default="market.db", help="DuckDB snapshot database (default market.db)")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    args = p.parse_args(argv)

    if not Path(args.db).exists():
        print(f"warning: {args.db} does not exist yet; POST a snapshot to /api/snapshot or run `fetch --store` to create it", file=sys.stderr)

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
