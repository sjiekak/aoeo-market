"""Market intelligence website (stdlib-only web server).

Serves the dashboard single-page app from :mod:`aoeo_market.web.static` and a
JSON API over the snapshot database.  This process is the **single owner** of
the DuckDB file: the ``fetch --store`` CLI posts each snapshot to
``POST /api/snapshot`` instead of touching the database itself, so exactly
one component ever opens the file — one read-write connection held for the
process lifetime (never a second one, because DuckDB forbids mixing connection
modes on the same file)::

    uv run python -m aoeo_market.web --db market.db --port 8000 --write-port 8001
    uv run python -m aoeo_market.cli fetch --store http://127.0.0.1:8001

The reads and the write answer on **two different ports**: the dashboard and
the public ``/api/*`` reads are served on ``--port``, the single write
endpoint ``POST /api/snapshot`` only on ``--write-port``.  Keeping the
unauthenticated ingestion endpoint on its own port lets an operator publish
the dashboard while the fetcher is the only client that can reach the write
port (bind it to another interface with ``--write-host``, or firewall it).

That split maps directly onto Kubernetes: the web app runs as a StatefulSet
pod owning the database volume, and ``fetch --store <url>`` runs as a
CronJob that only needs network access to the write port.

``--since`` (``--start-date``) fixes a **start date** on the whole process: the
read views then only see snapshots captured at or after it, so a long history
can be trimmed to what the dashboard should show.  It is deliberately a
start-up argument and not a query parameter — the API cannot read outside the
window the operator chose, and nothing is deleted: the older snapshots stay in
the file, ingestion keeps appending to it, and a restart without ``--since``
serves them again.

Endpoints are documented in the machine-readable OpenAPI 3.0 reference
served at ``GET /openapi.json`` (generated in :mod:`aoeo_market.web.openapi`
from the routing metadata, so it cannot drift from the implementation).
In short: probes at ``/healthz`` and ``/readyz``; dashboard reads under
``/api/*`` (overview, search, listings, item history, not-on-sale,
best-sellers, best-value, recently-removed); the single write endpoint
``POST /api/snapshot`` (unauthenticated — keep the write port on a private
network or protect it with a reverse proxy when it is reachable beyond
localhost).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import threading
import urllib.parse
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import duckdb

from .. import store
from ..market import Listing
from . import openapi

STATIC_DIR = Path(__file__).with_name("static")
# Extension -> content type for the generic static-file route under /static/.
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webp": "image/webp",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

_JSON = "application/json; charset=utf-8"

# Responses smaller than this are not worth gzipping: the framing overhead and
# the CPU cost more than the bytes saved.
_GZIP_MIN_BYTES = 1024

# The single write endpoint, served only by the write listener (--write-port).
_WRITE_ENDPOINT = "/api/snapshot"

# Seller empire ids are player-identifying, so the read API never exposes the
# real value.  The store still selects it (the ingestion path and the internal
# snapshot joins need it); it is overwritten with this sentinel in ``_json`` —
# the last step before the payload is serialized.  A later change can drop the
# field from the contract entirely; for now the shape stays stable.
REDACTED_SELLER_ID = 0

# A bare calendar date (the common ``--since 2026-09-01`` form) is parsed as
# midnight UTC; anything else goes through the full ISO-8601 parser.
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_start_date(value: str) -> float:
    """Parse a ``--since`` value into Unix seconds (UTC).

    Accepts a calendar date (``2026-09-01``, read as midnight UTC — every
    timestamp in the database is UTC) or a full ISO-8601 instant
    (``2026-09-01T12:30:00Z``); a value without an offset is read as UTC too.
    Raises :class:`ValueError` for anything else, so :func:`main` can report it
    as an argument error.
    """
    text = value.strip()
    try:
        if _DATE_ONLY.match(text):
            day = date.fromisoformat(text)
            parsed = datetime(day.year, day.month, day.day, tzinfo=UTC)
        else:
            parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{value!r} is not a date (YYYY-MM-DD) or an ISO-8601 instant") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _error_body(message: str) -> bytes:
    """The JSON body of an error response (handlers share the app's shape)."""
    return json.dumps({"error": message}).encode()


def _redact_seller_ids(payload: object) -> None:
    """Overwrite every ``seller_empire_id`` in *payload* with the sentinel.

    Walks dicts and lists in place, so it works for a single row, a bare list
    of rows, or a nested document (``/api/item/<id>`` returns current and
    previous listings).  Mutating is safe because every read view builds fresh
    dicts per request.
    """
    if isinstance(payload, dict):
        if "seller_empire_id" in payload:
            payload["seller_empire_id"] = REDACTED_SELLER_ID
        for value in payload.values():
            _redact_seller_ids(value)
    elif isinstance(payload, list):
        for value in payload:
            _redact_seller_ids(value)


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

# Every item has its own page URL under this prefix. The dashboard shell answers
# it and the client opens the matching item view from the path (see
# ``static/app.js``), so an item link is a real page rather than a hash fragment.
_ITEM_PREFIX = "/item/"


def _serves_dashboard(path: str) -> bool:
    """True for the dashboard shell and for an item's own page URL.

    ``/item/`` without an id is not a page, so it falls through to the 404.
    """
    return path in ("/", "/index.html") or (path.startswith(_ITEM_PREFIX) and len(path) > len(_ITEM_PREFIX))


class _BadParam(ValueError):
    """Malformed query parameter — reported as HTTP 400."""


class WebApp:
    """Routing + JSON API over one snapshot database (sole writer).

    *since* is the process-wide start date (Unix seconds, from ``--since``):
    when set, every read is restricted to snapshots captured at or after it.
    The write path ignores it — snapshots keep being appended whatever the
    window — so the cutoff is a pure read-side view of the same file.
    """

    def __init__(self, db_path: str, since: float | None = None):
        self.db_path = db_path
        self.since = since
        # DuckDB forbids mixing read-only and read-write connections to the
        # same file in one process, so every connection here is read-write;
        # this process is the single owner of the file by design.  One
        # connection is opened for the process lifetime (not per request, which
        # re-ran the schema statements every time) and the lock — DuckDB
        # connections are not safe for concurrent use — serializes every query
        # and snapshot write across the request threads.
        self._lock = threading.Lock()
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._in_memory = False
        # The expensive read views are pure functions of the stored snapshots,
        # so one cache keyed by the latest snapshot id serves every request
        # until a new snapshot arrives.
        self._views = store.SnapshotCache()

    def handle(self, path: str, query: dict[str, list[str]] | None = None) -> tuple[int, str, bytes]:
        """Route one GET and return ``(status, content_type, body)``."""
        query = query or {}
        try:
            if path == "/openapi.json":
                return 200, _JSON, openapi.spec_json()
            if path == "/healthz":
                return 200, _JSON, b'{"status": "ok"}'
            if path == "/readyz":
                return self._readyz()
            if _serves_dashboard(path):
                return 200, _STATIC_TYPES[".html"], (STATIC_DIR / "index.html").read_bytes()
            if path.startswith("/static/"):
                return self._static(path[len("/static/") :])
            if path == "/api/overview":
                return self._json(self._query(lambda conn: store.market_overview(conn, since=self.since)))
            if path == "/api/listings":
                rows = self._query(
                    lambda conn: store.active_listings(
                        conn,
                        item_type=query.get("type", [None])[0] or None,
                        q=query.get("q", [None])[0] or None,
                        sort=query.get("sort", ["price"])[0],
                        direction=query.get("dir", ["asc"])[0],
                        cache=self._views,
                        since=self.since,
                    )
                )
                return self._json(self._page(rows, query))
            if path == "/api/not-on-sale":
                return self._json(
                    self._query(
                        lambda conn: store.items_not_on_sale(
                            conn,
                            order=query.get("order", ["median_unit_price"])[0],
                            direction=query.get("dir", ["desc"])[0],
                            since=self.since,
                        )
                    )
                )
            if path == "/api/search":
                return self._json(
                    self._query(
                        lambda conn: store.search_items(
                            conn,
                            query.get("q", [""])[0],
                            limit=self._int_param(query, "limit", store.SEARCH_LIMIT),
                            since=self.since,
                        )
                    )
                )
            if path == "/api/best-sellers":
                return self._json(
                    self._query(
                        lambda conn: store.best_sellers(
                            conn,
                            order=query.get("order", ["median_time"])[0],
                            direction=query.get("dir", ["asc"])[0],
                            min_sales=self._int_param(query, "min_sales", 1),
                            cache=self._views,
                            since=self.since,
                        )
                    )
                )
            if path == "/api/best-value":
                return self._json(
                    self._query(
                        lambda conn: store.crafting_value(
                            conn,
                            order=query.get("order", ["value_ratio"])[0],
                            direction=query.get("dir", ["desc"])[0],
                            cache=self._views,
                            since=self.since,
                        )
                    )
                )
            if path == "/api/recently-removed":
                return self._json(self._query(lambda conn: store.recently_removed(conn, window=self._window_param(query), since=self.since)))
            if path.startswith("/api/item/"):
                item_id = urllib.parse.unquote(path[len("/api/item/") :])
                history = self._query(lambda conn: store.price_history(conn, item_id, since=self.since))
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
        if path != _WRITE_ENDPOINT:
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
            with self._lock:
                if not Path(self.db_path).exists():
                    # The first snapshot creates the file; drop the in-memory
                    # reader so every later request follows the real database.
                    self._open_file_connection()
                snapshot_id = store.record_snapshot(self._conn(), listings, captured_at)
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

    def _readyz(self) -> tuple[int, str, bytes]:
        """Readiness: the database file exists, opens, and answers queries."""
        if not Path(self.db_path).exists():
            return 503, _JSON, json.dumps({"status": "not ready", "database": "not initialized"}).encode()
        try:
            count = self._query(lambda conn: store.snapshot_count(conn, since=self.since))
        except (duckdb.Error, OSError) as exc:
            return 503, _JSON, json.dumps({"status": "not ready", "database": f"error: {exc}"}).encode()
        return 200, _JSON, json.dumps({"status": "ready", "database": "ok", "snapshots": count}).encode()

    def _static(self, name: str) -> tuple[int, str, bytes]:
        """Serve one file from the static directory (safe whitelist by type).

        Allows subdirectories (e.g. ``sprites/materials.webp``) but rejects
        path traversal (``..``, absolute paths, backslashes) and resolves the
        target so a symlink cannot escape ``STATIC_DIR``.
        """
        if not name or ".." in name or "\\" in name or name.startswith(("/", "\\")):
            return self._error(404, f"no static file {name!r}")
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in target.parents:
            return self._error(404, f"no static file {name!r}")
        ctype = _STATIC_TYPES.get(target.suffix.lower())
        if ctype is None:
            return self._error(404, f"no static file {name!r}")
        try:
            return 200, ctype, target.read_bytes()
        except OSError:
            return self._error(404, f"no static file {name!r}")

    def _query(self, run: Callable[[duckdb.DuckDBPyConnection], Any]) -> Any:
        """Run *run* against the process-wide connection.

        The lock serializes queries and writes, because one DuckDB connection
        is not safe to use from two threads at once.  *run* is called inside
        the lock, so it must not do slow non-database work (the JSON
        serialization happens after this returns).
        """
        with self._lock:
            return run(self._conn())

    def _open_file_connection(self) -> None:
        """Replace the cached connection with the on-disk database (lock held)."""
        if self._connection is not None:
            self._connection.close()
        self._connection = store.open_store(self.db_path)
        self._in_memory = False

    def _conn(self) -> duckdb.DuckDBPyConnection:
        """The one connection to the snapshot database; the lock must be held.

        Before the first snapshot the file does not exist yet, so the empty
        state is served from an in-memory schema instead of erroring; the
        cached in-memory connection is swapped for the real one as soon as the
        file appears.
        """
        if self._connection is not None and not (self._in_memory and Path(self.db_path).exists()):
            return self._connection
        if Path(self.db_path).exists():
            self._open_file_connection()
        else:
            if self._connection is not None:
                self._connection.close()
            self._connection = store.open_memory()
            self._in_memory = True
        return self._connection

    def close(self) -> None:
        """Close the process-wide connection (the server owns it for its life)."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def data_validator(self) -> str:
        """A cheap cache validator for the database-backed reads.

        Every read view is a pure function of the stored snapshots, so the
        latest snapshot id tells a client whether its copy is still current.
        Unlike a hash of the body it is known *before* the view is computed, so
        a revalidating request skips the query and the serialization entirely.
        """
        with self._lock:
            latest = store.latest_snapshot(self._conn(), since=self.since)
        snapshot_id = latest["id"] if latest else "none"
        return f'"snap:{snapshot_id}"'

    @staticmethod
    def _page(rows: list[dict], query: dict[str, list[str]]) -> list[dict]:
        """Slice *rows* by the optional ``offset`` and ``limit`` query params.

        Both are opt-in: without them the whole list is returned, which is what
        the dashboard asks for.  ``limit`` lets a large listings result be
        paged instead of transferred whole.
        """
        offset = WebApp._int_param(query, "offset", 0)
        limit = WebApp._int_param(query, "limit", None)
        if offset < 0:
            raise _BadParam("offset must be zero or positive")
        if limit is not None and limit < 1:
            raise _BadParam("limit must be a positive integer")
        window = rows[offset:]
        return window[:limit] if limit is not None else window

    @staticmethod
    def _int_param(query: dict[str, list[str]], name: str, default: int | None) -> int | None:
        raw = query.get(name, [None])[0]
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            raise _BadParam(f"{name} must be an integer") from None

    @staticmethod
    def _window_param(query: dict[str, list[str]]) -> timedelta | None:
        """Parse the ``window`` query param (seconds) into a ``timedelta``."""
        raw = query.get("window", [None])[0]
        if raw is None:
            return None
        try:
            seconds = float(raw)
        except ValueError:
            raise _BadParam("window must be a number of seconds") from None
        if seconds <= 0:
            raise _BadParam("window must be a positive number of seconds")
        return timedelta(seconds=seconds)

    def _json(self, payload) -> tuple[int, str, bytes]:
        # Redact at the very last minute: the store's queries keep the real id
        # for joins and ordering, the client never sees it.
        _redact_seller_ids(payload)
        return 200, _JSON, json.dumps(payload).encode()

    def _error(self, status: int, message: str) -> tuple[int, str, bytes]:
        return status, _JSON, _error_body(message)


class _Handler(BaseHTTPRequestHandler):
    """Shared response plumbing for the two listeners.

    Each listener serves exactly one HTTP method: the dashboard/read listener
    answers ``GET``, the write listener answers ``POST``.  A request for the
    other method is a 404 that names the port its route lives on, so a
    misconfigured client finds out immediately.
    """

    app: WebApp

    def _respond(self, status: int, ctype: str, body: bytes, *, etag: str | None = None) -> None:
        """Send one response, revalidating with ETag and compressing when asked.

        With no *etag* given, a successful body is hashed — that is what
        non-database routes (static files, the OpenAPI document) revalidate by.
        The database-backed routes pass the cheap snapshot validator instead
        (see :meth:`WebApp.data_validator`), so their 304 is decided before the
        view is computed.  ``Cache-Control: no-cache`` makes the client
        revalidate rather than reuse a stale copy, because a new snapshot
        changes the data under the same URL.  When the client accepts gzip the
        body is compressed — the listings payload drops from ~950 KB to ~120 KB.
        """
        if etag is None and status == 200 and body:
            etag = '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"'
        if status == 200 and etag is not None and self.headers.get("If-None-Match") == etag:
            status, body = 304, b""
        encoding = None
        if len(body) >= _GZIP_MIN_BYTES and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body)
            encoding = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if etag is not None and status in (200, 304):
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        if status != 304:
            # A 304 carries no body, so it carries no Content-Length either.
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        """Consume the request body, so the client reads the response."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def log_message(self, fmt: str, *args: object) -> None:
        super().log_message(fmt, *args)


class _ReadHandler(_Handler):
    """The dashboard and the read API (GET, on ``--port``)."""

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        # A read of the database is validated by the snapshot id, checked
        # *before* the view is computed so a revalidation is a cheap 304.  The
        # other routes (the shell, static files, OpenAPI) hash their body
        # instead, since their content does not follow the snapshot.
        validator = self.app.data_validator() if parsed.path.startswith("/api/") and parsed.path != _WRITE_ENDPOINT else None
        if validator is not None and self.headers.get("If-None-Match") == validator:
            self._respond(304, _JSON, b"", etag=validator)
            return
        status, ctype, body = self.app.handle(parsed.path, query)
        self._respond(status, ctype, body, etag=validator)

    def do_POST(self) -> None:
        self._read_body()
        message = f"this port serves the read API only; POST {_WRITE_ENDPOINT} is on the write port (--write-port)"
        self._respond(404, _JSON, _error_body(message))


class _WriteHandler(_Handler):
    """The snapshot write API (POST, on ``--write-port``)."""

    def do_GET(self) -> None:
        self._respond(404, _JSON, _error_body(f"this port serves only POST {_WRITE_ENDPOINT}; read the dashboard on the --port listener"))

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        status, ctype, body = self.app.handle_post(parsed.path, self._read_body())
        self._respond(status, ctype, body)


def create_servers(app: WebApp, host: str, port: int, write_host: str, write_port: int) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    """Bind the read and write listeners over one ``WebApp`` and return ``(read, write)``.

    Neither server is serving yet.  Port ``0`` binds a free port (used by
    tests); both listeners share the app, so its database and write lock are
    common to them.  One process serves one app, which the handler classes
    carry as a class attribute.
    """
    _ReadHandler.app = app
    _WriteHandler.app = app
    read = ThreadingHTTPServer((host, port), _ReadHandler)
    try:
        write = ThreadingHTTPServer((write_host, write_port), _WriteHandler)
    except OSError:
        read.server_close()
        raise
    return read, write


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="aoeo_market.web",
        description="Serve the market intelligence dashboard and the snapshot write API.",
    )
    p.add_argument("--db", default="market.db", help="DuckDB snapshot database (default market.db)")
    p.add_argument(
        "--since",
        "--start-date",
        dest="since",
        metavar="DATE",
        help="serve only snapshots captured at/after DATE (YYYY-MM-DD, midnight UTC, or an ISO-8601 instant); older snapshots stay stored but unserved",
    )
    p.add_argument("--host", default="127.0.0.1", help="read API bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="read API port (default 8000)")
    p.add_argument("--write-host", default=None, help="snapshot write API bind address (default: --host)")
    p.add_argument("--write-port", type=int, default=8001, help="snapshot write API port (default 8001)")
    args = p.parse_args(argv)

    write_host = args.write_host or args.host
    if (write_host, args.write_port) == (args.host, args.port):
        p.error("--write-port must differ from --port when both bind the same host")

    since: float | None = None
    if args.since is not None:
        try:
            since = parse_start_date(args.since)
        except ValueError as exc:
            p.error(f"--since: {exc}")

    if not Path(args.db).exists():
        print(f"warning: {args.db} does not exist yet; run `aoeo_market.cli init-db --db {args.db}` (or POST a snapshot) to create it", file=sys.stderr)

    app = WebApp(args.db, since=since)
    if since is not None and Path(args.db).exists():
        # The window is legal but may hide every snapshot; say so instead of
        # leaving the operator with a dashboard that silently shows nothing.
        # A database that cannot be opened here is not fatal — the first
        # request reports it with the status code that belongs to it.
        try:
            empty_window = app.data_validator() == '"snap:none"'
        except (duckdb.Error, OSError):
            empty_window = False
        if empty_window:
            print(f"warning: no snapshot is at or after {args.since}; every read view will be empty", file=sys.stderr)
    read_server, write_server = create_servers(app, args.host, args.port, write_host, args.write_port)

    writing = threading.Thread(target=write_server.serve_forever, name="snapshot-write", daemon=True)
    writing.start()
    window = "" if since is None else f", snapshots since {args.since}"
    print(f"Serving the Merchant Zeno dashboard on http://{args.host}:{args.port} (db: {args.db}{window})")
    print(f"Serving the snapshot write API on http://{write_host}:{args.write_port} (POST {_WRITE_ENDPOINT})")
    try:
        read_server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        write_server.shutdown()
        write_server.server_close()
        read_server.server_close()
        app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
