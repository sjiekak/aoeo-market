"""Persistent market snapshot store (DuckDB).

The live ``fetch --store`` command records one immutable snapshot per run —
every active listing plus the wall-clock time it was taken — and the website
reads those snapshots back for the trading-intelligence views.  Snapshots are
append-only: queries never mutate, so the database can be read by the web
server while the cron fetch writes.

DuckDB is a single-file, in-process OLAP engine — no server to deploy, and
the columnar scan engine keeps the analytics fast as the history grows.  The
web server opens **read-only** connections (many processes may read the same
file at once); only the cron writer takes the read-write connection, and
:func:`open_store` briefly retries when the writer's exclusive lock is held.

Schema::

    snapshots(id BIGINT PK, captured_at DOUBLE)        -- one row per fetch
    listings(snapshot_id, transaction_id, ...,         -- active listings per snapshot
             item_key VARCHAR,                         -- lower(item_id): the lookup key
             seconds_till_expiry BIGINT,               -- server countdown at capture
             expires_at TIMESTAMP)                     -- absolute expiry (UTC)
    transaction_summary(transaction_id PK, ...,        -- one row per listing ever seen,
             first_snapshot_id, last_snapshot_id, ...) -- maintained by the writer
    meta(key VARCHAR PK, value VARCHAR)                -- small bookkeeping values

``seconds_till_expiry`` is what the wire record carries — a countdown relative
to the moment of capture — so on its own it cannot say *when* a listing
expires.  Every snapshot therefore also stores ``expires_at``, the absolute
instant computed at capture as ``captured_at + seconds_till_expiry``.  It is a
plain ``TIMESTAMP`` — no timezone is stored — whose wall clock is always UTC;
every read projects it as an ISO-8601 UTC string, so a snapshot preserves the
expiry regardless of the host's local timezone.  Snapshots recorded before the
column existed are filled by the one-shot ``aoeo_market.cli backfill`` command.

``item_key`` is the lowercased item id: the wire keeps the server's spelling
while the catalog and every lookup are lowercase, and a plain column can be
indexed where ``lower(item_id)`` in a predicate cannot.  ``record_snapshot``
stores it with each row; the same ``backfill`` command fills it for rows that
predate it.  ``transaction_summary`` is the per-transaction aggregate the
best-sellers view reads, refreshed inside every snapshot write; the ``backfill``
command also rebuilds it, and ``meta`` records through which snapshot it is
known complete (see :func:`summary_snapshot_id`).

All other times are Unix timestamps (UTC seconds).
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Iterable, Sequence
from datetime import timedelta
from pathlib import Path

import duckdb

from .catalog import craftable_ids, dismantle_of, icon_fields, name_of, rarity_of, recipe_of, type_of
from .catalog import fields as catalog_fields
from .catalog import search as catalog_search
from .market import Listing

_ITEM_KEY_INDEX = "idx_listings_item_key"

_SCHEMA_STATEMENTS = (
    "CREATE SEQUENCE IF NOT EXISTS snapshots_id_seq",
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        id BIGINT PRIMARY KEY DEFAULT nextval('snapshots_id_seq'),
        captured_at DOUBLE NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS listings (
        snapshot_id BIGINT NOT NULL,
        transaction_id BIGINT NOT NULL,
        seller_empire_id BIGINT NOT NULL,
        buyer_character_id BIGINT NOT NULL,
        item_id VARCHAR NOT NULL,
        item_key VARCHAR,
        item_type VARCHAR NOT NULL,
        item_level BIGINT NOT NULL,
        item_count BIGINT NOT NULL,
        item_price BIGINT NOT NULL,
        item_seed BIGINT NOT NULL,
        seconds_till_expiry BIGINT NOT NULL,
        expires_at TIMESTAMP,
        PRIMARY KEY (snapshot_id, transaction_id)
    )
    """,
    # Databases created before the absolute expiry column existed are upgraded
    # in place: the column is added nullable here, and the one-shot
    # ``aoeo_market.cli backfill`` command fills it for the existing rows.
    "ALTER TABLE listings ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP",
    # ``item_key`` is ``lower(item_id)``: the wire id keeps the server's
    # spelling while the catalog and every lookup are lowercase.  A plain column
    # can be indexed and compared directly, whereas ``lower(item_id)`` in a
    # predicate forces a scan of the whole table.  It is nullable for rows
    # stored before it existed; ``cli backfill`` fills those.
    "ALTER TABLE listings ADD COLUMN IF NOT EXISTS item_key VARCHAR",
    "CREATE INDEX IF NOT EXISTS idx_listings_item_price ON listings(item_id, item_price)",
    "CREATE INDEX IF NOT EXISTS idx_listings_snapshot ON listings(snapshot_id)",
    "CREATE INDEX IF NOT EXISTS idx_listings_item_type ON listings(item_type)",
    f"CREATE INDEX IF NOT EXISTS {_ITEM_KEY_INDEX} ON listings(item_key)",
    # The best-sellers view needs one row per listing transaction, which has to
    # be aggregated from every snapshot; the writer keeps that aggregate here so
    # a read is a scan of ~10k rows instead of the whole listing history.  The
    # primary key doubles as the index on the grouping key the view orders by.
    """
    CREATE TABLE IF NOT EXISTS transaction_summary (
        transaction_id BIGINT PRIMARY KEY,
        item_id VARCHAR NOT NULL,
        item_key VARCHAR NOT NULL,
        item_type VARCHAR NOT NULL,
        item_level BIGINT NOT NULL,
        first_snapshot_id BIGINT NOT NULL,
        last_snapshot_id BIGINT NOT NULL,
        unit_price DOUBLE NOT NULL,
        seconds_till_expiry BIGINT NOT NULL
    )
    """,
    # Small key/value table recording through which snapshot the aggregate above
    # is known complete, so a database migrated in place keeps reading the slow
    # (correct) path until ``cli backfill`` rebuilds it.
    "CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)",
)

# Backfill of the absolute expiry for listings stored before the column existed:
# a listing's expiry is the snapshot's capture time plus the countdown it
# carried, so the historical rows get exactly the instant new ones store.  The
# explicit AT TIME ZONE 'UTC' keeps the naive TIMESTAMP on the UTC wall clock.
_BACKFILL_EXPIRES_AT = """
    UPDATE listings
    SET expires_at = to_timestamp(s.captured_at + listings.seconds_till_expiry) AT TIME ZONE 'UTC'
    FROM snapshots AS s
    WHERE s.id = listings.snapshot_id AND listings.expires_at IS NULL
"""

# ``item_key`` is derived, so a database upgraded in place gets it by running
# this once (``cli backfill``); new rows carry it from ``record_snapshot``.
_BACKFILL_ITEM_KEYS = "UPDATE listings SET item_key = lower(item_id) WHERE item_key IS NULL"

# The per-transaction aggregate behind the best-sellers view, in two forms: a
# full rebuild for the backfill command, and an upsert that refreshes just the
# transactions seen in one new snapshot (the only ones whose last-seen row can
# have changed).  Both take the first row of a transaction in snapshot order
# (``arg_min``) for the item fields, matching how the view reads them live.
_SUMMARY_COLUMNS = """
        transaction_id,
        arg_min(item_id, snapshot_id) AS item_id,
        arg_min(lower(item_id), snapshot_id) AS item_key,
        arg_min(item_type, snapshot_id) AS item_type,
        arg_min(item_level, snapshot_id) AS item_level,
        min(snapshot_id) AS first_snapshot_id,
        max(snapshot_id) AS last_snapshot_id,
        arg_max(item_price / greatest(item_count, 1), snapshot_id) AS unit_price,
        arg_max(seconds_till_expiry, snapshot_id) AS seconds_till_expiry
"""
_REBUILD_TRANSACTION_SUMMARY = f"INSERT INTO transaction_summary SELECT {_SUMMARY_COLUMNS} FROM listings GROUP BY transaction_id"
_UPSERT_TRANSACTION_SUMMARY = f"""
    INSERT OR REPLACE INTO transaction_summary
    SELECT {_SUMMARY_COLUMNS}
    FROM listings
    WHERE transaction_id IN (SELECT transaction_id FROM listings WHERE snapshot_id = ?)
    GROUP BY transaction_id
"""
# Through which snapshot the aggregate is complete; see ``_maintain_summary``.
_SUMMARY_META_KEY = "transaction_summary_snapshot"

# ``expires_at`` is a naive UTC TIMESTAMP, so no timezone is stored with it.
# The session default is still pinned to UTC defensively, so any timezone-aware
# expression (or a database upgraded from an earlier TIMESTAMPTZ column) reads
# and writes UTC rather than the host's local zone.
_UTC_TIMEZONE = "SET TimeZone='UTC'"

_EXPIRES_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _expires_at(alias: str = "") -> str:
    """SQL projection of the naive-UTC expiry as canonical ISO-8601 text."""
    return f"strftime({alias + '.' if alias else ''}expires_at, '{_EXPIRES_AT_FORMAT}') AS expires_at"


def _listing_columns(alias: str = "") -> str:
    """``SELECT`` projection of a full listing row plus its UTC absolute expiry."""
    col = f"{alias}." if alias else ""
    stored = (
        "snapshot_id",
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
    return ", ".join([*(f"{col}{name}" for name in stored), _expires_at(alias)])


# One day: the observer's expiry window — a listing that vanishes with less
# than this left on its countdown is read as EXPIRED, otherwise sold/withdrawn.
EXPIRY_WINDOW_SECONDS = 86400.0

_LOCK_ATTEMPTS = 30
_LOCK_DELAY = 0.1  # seconds; the writer's exclusive lock is held for milliseconds


def backfill_expires_at(conn: duckdb.DuckDBPyConnection) -> int:
    """Fill ``expires_at`` for listings stored before the column existed.

    Each row's instant is its snapshot's ``captured_at`` plus the countdown the
    listing carried, so the backfilled history matches what a capture stores
    today.  Run once as ``aoeo_market.cli backfill``; it is idempotent and
    returns immediately when no listing has a NULL expiry.  Returns the number
    of rows filled.
    """
    if conn.execute("SELECT 1 FROM listings WHERE expires_at IS NULL LIMIT 1").fetchone() is None:
        return 0
    pending = int(conn.execute("SELECT COUNT(*) FROM listings WHERE expires_at IS NULL").fetchone()[0])
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(_BACKFILL_EXPIRES_AT)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return pending


def backfill_item_keys(conn: duckdb.DuckDBPyConnection) -> int:
    """Fill ``item_key`` for listings stored before the column existed.

    Idempotent and cheap when there is nothing to fill (the ``LIMIT 1`` probe
    finds no NULL).  Returns the number of rows filled.

    The update rewrites every row, so the index is dropped for the duration:
    maintaining it row by row costs ~40× the rebuild (seconds instead of
    milliseconds on a full history).  The drop must happen *outside* the
    transaction — dropped inside one, DuckDB still maintains it.  A crash
    between the drop and the rebuild is harmless: ``open_store`` recreates any
    index the schema declares.
    """
    if conn.execute("SELECT 1 FROM listings WHERE item_key IS NULL LIMIT 1").fetchone() is None:
        return 0
    pending = int(conn.execute("SELECT COUNT(*) FROM listings WHERE item_key IS NULL").fetchone()[0])
    conn.execute(f"DROP INDEX IF EXISTS {_ITEM_KEY_INDEX}")
    try:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(_BACKFILL_ITEM_KEYS)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {_ITEM_KEY_INDEX} ON listings(item_key)")
    return pending


def summary_snapshot_id(conn: duckdb.DuckDBPyConnection) -> int | None:
    """The snapshot through which ``transaction_summary`` is complete, if known.

    ``None`` (a database that has just been upgraded, or a freshly cleared
    table) means the aggregate cannot be trusted yet and callers must compute
    the slow way until ``cli backfill`` rebuilds it.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = ?", [_SUMMARY_META_KEY]).fetchone()
    return int(row[0]) if row else None


def _set_summary_snapshot(conn: duckdb.DuckDBPyConnection, snapshot_id: int) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [_SUMMARY_META_KEY, str(snapshot_id)])


def _maintain_summary(conn: duckdb.DuckDBPyConnection, snapshot_id: int) -> None:
    """Refresh the aggregate for one snapshot's transactions, inside its write.

    The aggregate stays usable only while it covers every snapshot: the upsert
    below recomputes the transactions *present* in the new snapshot, but a
    database upgraded in place is missing the transactions that vanished
    earlier.  The marker is therefore advanced only when it already covered the
    previous snapshot (or this is the very first snapshot).
    """
    previous = conn.execute("SELECT id FROM snapshots WHERE id < ? ORDER BY id DESC LIMIT 1", [snapshot_id]).fetchone()
    marker = summary_snapshot_id(conn)
    conn.execute(_UPSERT_TRANSACTION_SUMMARY, [snapshot_id])
    if previous is None:
        _set_summary_snapshot(conn, snapshot_id)  # first snapshot ever: now complete
    elif marker is not None and marker == previous[0]:
        _set_summary_snapshot(conn, snapshot_id)


def backfill_transaction_summary(conn: duckdb.DuckDBPyConnection) -> int:
    """Rebuild ``transaction_summary`` from every stored listing.

    Run once as part of ``aoeo_market.cli backfill`` after a database is
    upgraded: once the rebuild finishes the marker points at the latest
    snapshot, and the view reads the aggregate from then on.  Returns the
    number of transactions summarized.
    """
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute("DELETE FROM transaction_summary")
        conn.execute(_REBUILD_TRANSACTION_SUMMARY)
        total = int(conn.execute("SELECT COUNT(*) FROM transaction_summary").fetchone()[0])
        latest = conn.execute("SELECT id FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if latest is not None:
            _set_summary_snapshot(conn, latest[0])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return total


def open_store(path: str | os.PathLike, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the snapshot database at *path* (creating it when writing).

    ``read_only`` connections are meant for the web server: DuckDB allows
    many processes to read the same file concurrently, while only one process
    may hold the read-write connection.  Both modes briefly retry when the
    other side holds the file lock.
    """
    path = str(path)
    if read_only and not Path(path).exists():
        raise FileNotFoundError(f"database {path!r} does not exist yet; run `fetch --store` to create it")
    last: Exception | None = None
    for _ in range(_LOCK_ATTEMPTS):
        try:
            conn = duckdb.connect(path, read_only=read_only)
            conn.execute(_UTC_TIMEZONE)
            if not read_only:
                for stmt in _SCHEMA_STATEMENTS:
                    conn.execute(stmt)
            return conn
        except duckdb.IOException as exc:
            if "lock" not in str(exc).lower():
                raise
            last = exc
            time.sleep(_LOCK_DELAY)
    raise last  # type: ignore[misc]  # retried _LOCK_ATTEMPTS times


def open_memory() -> duckdb.DuckDBPyConnection:
    """Open an in-memory database with the full schema (no data).

    Used by the web server when the snapshot file does not exist yet, so the
    dashboard renders its empty state instead of erroring.
    """
    conn = duckdb.connect(":memory:")
    conn.execute(_UTC_TIMEZONE)
    for stmt in _SCHEMA_STATEMENTS:
        conn.execute(stmt)
    return conn


def _rows(conn: duckdb.DuckDBPyConnection, sql: str, params: Sequence = ()) -> list[dict]:
    """Execute *sql* and return the rows as dicts keyed by column name."""
    cur = conn.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _row(conn: duckdb.DuckDBPyConnection, sql: str, params: Sequence = ()) -> dict | None:
    rows = _rows(conn, sql, params)
    return rows[0] if rows else None


def _scalar(conn: duckdb.DuckDBPyConnection, sql: str, params: Sequence = ()) -> int | float:
    cur = conn.execute(sql, list(params))
    row = cur.fetchone()
    return row[0] if row else 0


def record_snapshot(
    conn: duckdb.DuckDBPyConnection,
    listings: Iterable[Listing],
    captured_at: float | None = None,
) -> int:
    """Append one snapshot of *listings* and return its snapshot id.

    Each listing stores its absolute UTC expiry, computed here as
    ``captured_at + seconds_till_expiry`` so the snapshot preserves *when* the
    listing expires rather than only the countdown the wire happened to carry.
    Each row also stores ``item_key`` (lowercased id) and the per-transaction
    aggregate is refreshed in the same transaction, so the best-sellers view
    never has to scan the history.
    """
    if captured_at is None:
        captured_at = time.time()
    rows = [
        (
            l.transaction_id,
            l.seller_empire_id,
            l.buyer_character_id,
            l.item_id,
            l.item_id.lower(),
            l.item_type,
            l.item_level,
            l.item_count,
            l.item_price,
            l.item_seed,
            l.seconds_till_expiry,
            captured_at + l.seconds_till_expiry,
        )
        for l in listings
    ]
    rows = list(rows)
    # Explicit transaction: DuckDB's connection context manager CLOSES the
    # connection on exit, unlike sqlite3's.
    conn.execute("BEGIN TRANSACTION")
    try:
        snapshot_id = conn.execute("INSERT INTO snapshots(captured_at) VALUES (?) RETURNING id", [captured_at]).fetchone()[0]
        if rows:
            conn.executemany(
                """
                INSERT INTO listings
                    (snapshot_id, transaction_id, seller_empire_id, buyer_character_id, item_id, item_key,
                     item_type, item_level, item_count, item_price, item_seed, seconds_till_expiry, expires_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?, to_timestamp(?) AT TIME ZONE 'UTC')
                """,
                [(snapshot_id, *row) for row in rows],
            )
        _maintain_summary(conn, snapshot_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return snapshot_id


def median(values: Sequence[int]) -> float:
    """Median of an already materialized sequence (small lists, no numpy)."""
    n = len(values)
    if n == 0:
        return 0.0
    s = sorted(values)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


# --- snapshot helpers ------------------------------------------------------


def latest_snapshot(conn: duckdb.DuckDBPyConnection) -> dict | None:
    return _row(conn, "SELECT id, captured_at FROM snapshots ORDER BY id DESC LIMIT 1")


def previous_snapshot(conn: duckdb.DuckDBPyConnection, snapshot_id: int) -> dict | None:
    return _row(conn, "SELECT id, captured_at FROM snapshots WHERE id < ? ORDER BY id DESC LIMIT 1", [snapshot_id])


def snapshot_count(conn: duckdb.DuckDBPyConnection) -> int:
    return int(_scalar(conn, "SELECT COUNT(*) FROM snapshots"))


# --- listing views ---------------------------------------------------------


def _listing_dict(row: dict) -> dict:
    d = dict(row)
    rar = rarity_of(d["item_id"])
    d["rarity"] = rar[1] if rar else None
    d["rarity_rank"] = rar[0] if rar else 0
    # ItemPrice is the total for the whole stack; the price per unit is what
    # makes listings of different stack sizes comparable.
    d["unit_price"] = round(d["item_price"] / max(d["item_count"], 1), 2)
    # Curated display name, kind, icon, … from the item catalog.
    d.update(catalog_fields(d["item_id"]))
    return d


_SORT_COLUMNS = {
    "price": "item_price",
    "level": "item_level",
    "count": "item_count",
    "expiry": "seconds_till_expiry",
    "item": "item_id",
    "type": "item_type",
    "seller": "seller_empire_id",
}


def active_listings(
    conn: duckdb.DuckDBPyConnection,
    snapshot_id: int | None = None,
    *,
    item_type: str | None = None,
    q: str | None = None,
    sort: str = "price",
    direction: str = "asc",
) -> list[dict]:
    """Listings of one snapshot (the latest by default), filtered and sorted.

    ``sort`` must be a key of :data:`_SORT_COLUMNS`; ``direction`` ``asc`` or
    ``desc``.  ``q`` is a case-insensitive substring filter on the item id
    **and** its catalog display name (so "xerxes" and "the Great" both match).
    """
    if snapshot_id is None:
        latest = latest_snapshot(conn)
        snapshot_id = latest["id"] if latest else -1
    where = "snapshot_id = ?"
    params: list = [snapshot_id]
    if item_type:
        where += " AND item_type = ?"
        params.append(item_type)
    col = _SORT_COLUMNS.get(sort, "item_price")
    if direction not in ("asc", "desc"):
        direction = "asc"
    rows = _rows(conn, f"SELECT {_listing_columns()} FROM listings WHERE {where} ORDER BY {col} {direction.upper()}, item_id", params)
    out = [_listing_dict(r) for r in rows]
    if q:
        # Applied after enrichment so the display name is searchable too; the
        # SQL sort order is preserved.
        needle = q.lower()
        out = [d for d in out if needle in d["item_id"].lower() or (d.get("name") and needle in d["name"].lower())]
    return out


# --- overview --------------------------------------------------------------

# Price histogram bins (log-ish): inclusive lower bound -> label.
_PRICE_BINS: tuple[tuple[int, str], ...] = (
    (0, "<100"),
    (100, "100–299"),
    (300, "300–999"),
    (1000, "1k–2.9k"),
    (3000, "3k–9.9k"),
    (10000, "10k–29.9k"),
    (30000, "30k–99.9k"),
    (100000, "100k–299k"),
    (300000, "300k–999k"),
    (1000000, "1M+"),
)


def market_overview(conn: duckdb.DuckDBPyConnection, top_movers: int = 15) -> dict:
    """Aggregate stats for the dashboard's overview tab."""
    latest = latest_snapshot(conn)
    if latest is None:
        return {
            "latest": None,
            "snapshot_count": 0,
            "active_listings": 0,
            "distinct_items": 0,
            "supply_history": [],
            "type_breakdown": [],
            "rarity_breakdown": [],
            "price_distribution": [],
            "top_movers": [],
        }
    sid = latest["id"]

    active = int(_scalar(conn, "SELECT COUNT(*) FROM listings WHERE snapshot_id = ?", [sid]))
    distinct = int(_scalar(conn, "SELECT COUNT(DISTINCT item_id) FROM listings WHERE snapshot_id = ?", [sid]))
    types = _rows(
        conn,
        "SELECT item_type AS name, COUNT(*) AS count FROM listings WHERE snapshot_id = ? GROUP BY item_type ORDER BY count DESC",
        [sid],
    )
    prices = [r["item_price"] / max(r["item_count"], 1) for r in _rows(conn, "SELECT item_price, item_count FROM listings WHERE snapshot_id = ?", [sid])]
    supply = _rows(
        conn,
        """
        SELECT s.captured_at AS t, COUNT(l.transaction_id) AS count
        FROM snapshots s LEFT JOIN listings l ON l.snapshot_id = s.id
        GROUP BY s.id, s.captured_at ORDER BY s.id
        """,
    )

    # Rarity histogram: authoritative rarity from the catalog, falling back to
    # the item-id suffix heuristic (see catalog.rarity_of).
    rarity_bins: dict[str, int] = {}
    for r in _rows(conn, "SELECT item_id FROM listings WHERE snapshot_id = ?", [sid]):
        name = (rarity_of(r["item_id"]) or (0, None))[1] or "unknown"
        rarity_bins[name] = rarity_bins.get(name, 0) + 1

    # Median price per item in the latest and previous snapshots -> movers.
    prev = previous_snapshot(conn, sid)
    movers = _price_movers(conn, sid, prev["id"] if prev else None, top_movers)

    return {
        "latest": latest,
        "snapshot_count": snapshot_count(conn),
        "active_listings": active,
        "distinct_items": distinct,
        "supply_history": supply,
        "type_breakdown": types,
        "rarity_breakdown": [{"name": k, "count": v} for k, v in sorted(rarity_bins.items())],
        "price_distribution": _price_histogram(prices),
        "top_movers": movers,
    }


def _price_histogram(prices: Sequence[int]) -> list[dict]:
    counts = [0] * len(_PRICE_BINS)
    for p in prices:
        idx = 0
        for i, (lo, _) in enumerate(_PRICE_BINS):
            if p >= lo:
                idx = i
        counts[idx] += 1
    return [{"label": label, "count": counts[i]} for i, (_, label) in enumerate(_PRICE_BINS)]


def _log_price_bins(prices: Sequence[float], bins: int = 10) -> list[dict]:
    """Histogram whose bin edges come from the data rather than a fixed ladder.

    Prices are multiplicative and right-skewed, so one static ladder cannot fit
    every item: a fixed 0..1M ramp put the median item's observations into two
    of its ten bins, leaving 86% of them in a single bar. Spacing the edges
    evenly in log space across the item's *own* range keeps the whole chart on
    the prices that actually exist, whatever the item's scale.

    The bin count is capped by the number of distinct prices so a low-variety
    item does not render a row of empty bars. Bins carry numeric bounds only —
    turning them into labels is the dashboard's job. Returns ``[]`` for no
    data, and a single bin when every observation shares one price (or the
    range cannot be log-spaced, e.g. a zero price).
    """
    values = sorted(prices)
    if not values:
        return []
    lo, hi = values[0], values[-1]
    if lo <= 0 or hi <= lo:
        return [{"bin_start": lo, "bin_end": hi, "count": len(values)}]

    count = max(1, min(bins, len(set(values))))
    a, b = math.log10(lo), math.log10(hi)
    # round() strips the float noise of the 10**log10() round trip; the outer
    # edges are pinned back to the real data bounds.
    edges = [round(10 ** (a + (b - a) * i / count), 6) for i in range(count + 1)]
    edges[0], edges[count] = lo, hi

    counts = [0] * count
    for p in values:
        idx = 0
        for i in range(count):
            if p >= edges[i]:
                idx = i
        counts[idx] += 1
    return [{"bin_start": edges[i], "bin_end": edges[i + 1], "count": counts[i]} for i in range(count)]


def _price_movers(conn: duckdb.DuckDBPyConnection, sid: int, prev_sid: int | None, top: int) -> list[dict]:
    """Items whose median price moved most between two snapshots (percent)."""
    if prev_sid is None:
        return []
    now = _median_prices_by_item(conn, sid)
    before = _median_prices_by_item(conn, prev_sid)
    movers = []
    for item_id, med_now in now.items():
        med_before = before.get(item_id)
        if not med_before:
            continue
        pct = (med_now - med_before) / med_before * 100.0
        rar = rarity_of(item_id)
        movers.append(
            {
                "item_id": item_id,
                "name": name_of(item_id),
                **icon_fields(item_id),
                "rarity": rar[1] if rar else None,
                "rarity_rank": rar[0] if rar else 0,
                "median_before": round(med_before),
                "median_now": round(med_now),
                "change_pct": round(pct, 1),
            }
        )
    movers.sort(key=lambda m: -abs(m["change_pct"]))
    return movers[:top]


def _median_prices_by_item(conn: duckdb.DuckDBPyConnection, snapshot_id: int) -> dict[str, float]:
    rows = _rows(
        conn,
        "SELECT item_id, item_price, item_count FROM listings WHERE snapshot_id = ? ORDER BY item_id, item_price",
        [snapshot_id],
    )
    out: dict[str, list[int]] = {}
    for r in rows:
        out.setdefault(r["item_id"], []).append(r["item_price"] / max(r["item_count"], 1))
    return {k: median(v) for k, v in out.items()}


# --- per-item history ------------------------------------------------------


def _material_prices(conn: duckdb.DuckDBPyConnection, item_ids: Sequence[str]) -> dict[str, float]:
    """Per-unit price per item id: current median when listed now, else historical.

    Same per-unit normalisation as every other view, so a stack's total price
    never leaks into an estimate.
    """
    ids = sorted({i.lower() for i in item_ids if i})
    if not ids:
        return {}
    latest = latest_snapshot(conn)
    latest_id = latest["id"] if latest else None
    placeholders = ",".join("?" for _ in ids)
    every: dict[str, list[float]] = {}
    active: dict[str, list[float]] = {}
    for r in _rows(
        conn,
        f"SELECT item_id, item_price, item_count, snapshot_id FROM listings WHERE item_key IN ({placeholders})",
        ids,
    ):
        unit = r["item_price"] / max(r["item_count"], 1)
        key = r["item_id"].lower()
        every.setdefault(key, []).append(unit)
        if r["snapshot_id"] == latest_id:
            active.setdefault(key, []).append(unit)
    return {k: median(active.get(k) or ps) for k, ps in every.items()}


def _recipe_payload(conn: duckdb.DuckDBPyConnection, item_id: str) -> dict | None:
    """The crafting recipe with per-material prices and a total cost estimate."""
    recipe = recipe_of(item_id)
    if not recipe:
        return None
    prices = _material_prices(conn, [(m.get("id") or "") for m in recipe.get("materials", [])])
    materials: list[dict] = []
    total = 0.0
    priced = 0
    for m in recipe.get("materials", []):
        mid = m.get("id") or ""
        qty = m.get("quantity")
        price = prices.get(mid.lower())
        row = {"item_id": mid, "quantity": qty, "unit_price": round(price, 2) if price is not None else None}
        row.update(catalog_fields(mid))
        if price is not None and qty:
            total += price * qty
            priced += 1
        materials.append(row)
    return {
        "school": recipe.get("school"),
        "level": recipe.get("level"),
        "materials": materials,
        # A partial estimate is still useful, but callers can tell it is partial.
        "cost": round(total, 2) if priced else None,
        "materials_priced": priced,
    }


def _dismantle_payload(item_id: str) -> dict | None:
    """What the Gear Dismantler yields, enriched with catalog names/icons."""
    info = dismantle_of(item_id)
    if not info:
        return None
    materials = []
    for mid in info.get("materials", []):
        if not mid:
            continue
        row = {"item_id": mid}
        row.update(catalog_fields(mid))
        materials.append(row)
    return {
        "type": info.get("type"),
        "school": info.get("school"),
        "rarity": info.get("rarity"),
        "materials": materials,
    }


def price_history(conn: duckdb.DuckDBPyConnection, item_id: str, max_points: int = 2000) -> dict | None:
    """Current and previous listings plus the price series of one item.

    Prices are per unit (item_price / item_count) so listings of different
    stack sizes stay comparable.  Returns ``None`` when the item was never
    observed.  ``current`` is the item's active listings; ``previous`` lists
    the vanished ones as full listing rows (all wire fields plus first/last
    seen, vanished-at, and the EXPIRED vs REMOVED classification), newest
    first.  The raw scatter points are downsampled evenly to *max_points* so
    long histories stay chartable.  ``histogram`` is the per-unit price
    distribution of the item's distinct listings, binned across the observed
    price range.
    """
    rows = _rows(
        conn,
        f"""
        SELECT {_listing_columns("l")}, s.captured_at AS t
        FROM listings l JOIN snapshots s ON s.id = l.snapshot_id
        WHERE l.item_key = ?
        ORDER BY s.id, l.item_price
        """,
        [item_id.lower()],
    )
    if not rows:
        return None
    # Wire item ids are case-insensitive (the catalog and the Dismantler map key
    # everything lowercased while listings keep the server's spelling), so adopt
    # the stored one and every downstream lookup and the returned id agree.
    item_id = rows[0]["item_id"]

    latest = latest_snapshot(conn)
    active_txs: set[int] = set()
    if latest:
        active_txs = {
            r["transaction_id"] for r in _rows(conn, "SELECT transaction_id FROM listings WHERE snapshot_id = ? AND item_id = ?", [latest["id"], item_id])
        }

    snaps = {r["id"]: r["captured_at"] for r in _rows(conn, "SELECT id, captured_at FROM snapshots ORDER BY id")}
    snap_ids = sorted(snaps)
    next_sid = {snap_ids[i]: snap_ids[i + 1] for i in range(len(snap_ids) - 1)}

    series: dict[int, dict] = {}
    points: list[dict] = []
    txs: dict[int, dict] = {}
    for r in rows:
        sid = r["snapshot_id"]
        unit = r["item_price"] / max(r["item_count"], 1)
        series.setdefault(
            sid,
            {"t": r["t"], "prices": [], "count": 0, "item_type": r["item_type"], "item_level": r["item_level"]},
        )
        series[sid]["prices"].append(unit)
        series[sid]["count"] += 1
        points.append({"t": r["t"], "price": round(unit, 2)})

        tx = txs.get(r["transaction_id"])
        if tx is None:
            tx = txs[r["transaction_id"]] = {"first_seen": r["t"], "row": r}
        else:
            tx["row"] = r  # rows are ordered by snapshot id, so the last wins

    def summarize(s: dict) -> dict:
        p = s["prices"]
        return {
            "t": s["t"],
            "count": s["count"],
            "min": min(p),
            "max": max(p),
            "median": median(p),
        }

    ordered = [summarize(series[sid]) for sid in sorted(series)]
    current = active_listings(conn, latest["id"], q=item_id) if latest else []
    current = [c for c in current if c["item_id"] == item_id]

    previous: list[dict] = []
    for tx_id, t in txs.items():
        if tx_id in active_txs:
            continue
        row = t["row"]
        remaining = row["seconds_till_expiry"]
        reason = "EXPIRED" if remaining < EXPIRY_WINDOW_SECONDS else "REMOVED"
        nxt = next_sid.get(row["snapshot_id"])
        vanished_at = snaps[nxt] if nxt is not None else (latest["captured_at"] if latest else None)
        # Every vanished listing carries its full Listing fields (the wire
        # record of aoeo_market.market.Listing) plus the observation span, so
        # the row reuses the Listing schema of the OpenAPI contract.
        previous.append(
            {
                "transaction_id": tx_id,
                "seller_empire_id": row["seller_empire_id"],
                "buyer_character_id": row["buyer_character_id"],
                "item_id": row["item_id"],
                "item_type": row["item_type"],
                "item_level": row["item_level"],
                "item_count": row["item_count"],
                "item_price": row["item_price"],
                "item_seed": row["item_seed"],
                "seconds_till_expiry": row["seconds_till_expiry"],
                "expires_at": row["expires_at"],
                "unit_price": round(row["item_price"] / max(row["item_count"], 1), 2),
                "first_seen": t["first_seen"],
                "last_seen": row["t"],
                "vanished_at": vanished_at,
                "reason": reason,
            }
        )
    previous.sort(key=lambda d: d["vanished_at"] or 0.0, reverse=True)

    if len(points) > max_points:
        step = len(points) / max_points
        points = [points[int(i * step)] for i in range(max_points)]

    # Count each listing once: a listing keeps its unit price for its whole
    # life, so binning the per-snapshot points would weight it by how long it
    # lingered rather than by how it was priced.
    listing_prices = [round(t["row"]["item_price"] / max(t["row"]["item_count"], 1), 2) for t in txs.values()]
    histogram = _log_price_bins(listing_prices)

    meta = series[max(series)]
    rar = rarity_of(item_id)
    extra = catalog_fields(item_id)
    name = extra.pop("name", None)
    recipe = _recipe_payload(conn, item_id)
    dismantle = _dismantle_payload(item_id)
    out = {
        "item_id": item_id,
        "name": name,
        "item_type": meta["item_type"],
        "item_level": meta["item_level"],
        "rarity": rar[1] if rar else None,
        "rarity_rank": rar[0] if rar else 0,
        **extra,
        "current": current,
        "previous": previous,
        "series": ordered,
        "points": points,
        "histogram": histogram,
    }
    # Omitted rather than null: the spec composes them from $refs, and OpenAPI
    # 3.0's nullable cannot wrap an allOf/$ref the validator accepts.
    if recipe:
        out["recipe"] = recipe
    if dismantle:
        out["dismantle"] = dismantle
    return out


# --- not-on-sale / recently-removed ---------------------------------------


_NOT_SALE_SORTS = {
    "median_unit_price": "median_unit_price",
    "rarity": "rarity_rank",
    "item": "item_id",
    "type": "item_type",
    "level": "item_level",
    "last_seen": "last_seen",
    "times_listed": "times_listed",
    "max_unit_price": "max_unit_price",
    "min_unit_price": "min_unit_price",
}


def items_not_on_sale(
    conn: duckdb.DuckDBPyConnection,
    *,
    order: str = "median_unit_price",
    direction: str = "desc",
) -> list[dict]:
    """Items seen historically that have **no active listing right now**.

    Each row carries the item's historical price stats (per unit, so stack
    sizes stay comparable) so traders can see what is currently unavailable
    and what it traded for.  ``order`` is one of :data:`_NOT_SALE_SORTS`.
    """
    latest = latest_snapshot(conn)
    if latest is None:
        return []
    active_ids = {r["item_id"] for r in _rows(conn, "SELECT DISTINCT item_id FROM listings WHERE snapshot_id = ?", [latest["id"]])}

    out: list[dict] = []
    for r in _rows(
        conn,
        """
        SELECT item_id, item_type, item_level,
               COUNT(*) AS times_listed, MIN(item_price * 1.0 / item_count) AS min_price, MAX(item_price * 1.0 / item_count) AS max_price
        FROM listings
        GROUP BY item_id, item_type, item_level
        """,
    ):
        if r["item_id"] in active_ids:
            continue
        prices = [
            p["item_price"] / max(p["item_count"], 1)
            for p in _rows(conn, "SELECT item_price, item_count FROM listings WHERE item_id = ? ORDER BY item_price", [r["item_id"]])
        ]
        last = _row(
            conn,
            """
            SELECT s.captured_at AS last_seen, l.item_type AS t, l.item_level AS lvl
            FROM listings l JOIN snapshots s ON s.id = l.snapshot_id
            WHERE l.item_id = ? ORDER BY s.id DESC LIMIT 1
            """,
            [r["item_id"]],
        )
        rar = rarity_of(r["item_id"])
        out.append(
            {
                "item_id": r["item_id"],
                "name": name_of(r["item_id"]),
                **icon_fields(r["item_id"]),
                "item_type": last["t"] if last else r["item_type"],
                "item_level": last["lvl"] if last else r["item_level"],
                "rarity": rar[1] if rar else None,
                "rarity_rank": rar[0] if rar else 0,
                "median_unit_price": median(prices),
                "min_unit_price": r["min_price"],
                "max_unit_price": r["max_price"],
                "times_listed": r["times_listed"],
                "last_seen": last["last_seen"] if last else None,
            }
        )

    col = _NOT_SALE_SORTS.get(order, "median_unit_price")
    if col in ("item_id", "item_type", "last_seen"):
        key = lambda d: d[col]
    else:
        key = lambda d: (d[col] is not None, d[col] or 0)
    out.sort(key=key, reverse=direction == "desc")
    return out


def recently_removed(conn: duckdb.DuckDBPyConnection, *, window: timedelta | None = None) -> list[dict]:
    """Listings that vanished, relative to the latest snapshot.

    With ``window=None`` (the default) this is the delta between the two most
    recent snapshots — the view's original behaviour.  With a ``window``
    :class:`datetime.timedelta` it instead returns every listing that vanished
    within that span of the latest snapshot's ``captured_at``: a transaction
    vanishes at the first snapshot where it is absent after having been
    present, and that snapshot's time is the returned ``vanished_at``.

    Classified like the live observer: EXPIRED when the listing timed out with
    less than a day left on its countdown, REMOVED (sold or withdrawn —
    indistinguishable) otherwise.
    """
    latest = latest_snapshot(conn)
    if latest is None:
        return []
    if window is None:
        prev = previous_snapshot(conn, latest["id"])
        if prev is None:
            return []
        gone = _rows(
            conn,
            f"""
            SELECT {_listing_columns("l")}, s.captured_at AS last_seen
            FROM listings l JOIN snapshots s ON s.id = l.snapshot_id
            WHERE l.snapshot_id = ? AND l.transaction_id NOT IN (
                SELECT transaction_id FROM listings WHERE snapshot_id = ?
            )
            """,
            [prev["id"], latest["id"]],
        )
        for g in gone:
            g["vanished_at"] = latest["captured_at"]
    else:
        window_start = latest["captured_at"] - window.total_seconds()
        gone = _rows(
            conn,
            f"""
            WITH vanished AS (
                SELECT transaction_id, MAX(snapshot_id) AS last_sid
                FROM listings
                WHERE transaction_id NOT IN (
                    SELECT transaction_id FROM listings WHERE snapshot_id = ?
                )
                GROUP BY transaction_id
            ),
            vtimes AS (
                SELECT transaction_id, last_sid,
                       (SELECT MIN(id) FROM snapshots WHERE id > last_sid) AS vanish_sid
                FROM vanished
            )
            SELECT {_listing_columns("l")}, s.captured_at AS last_seen, vs.captured_at AS vanished_at
            FROM vtimes v
            JOIN listings l ON l.snapshot_id = v.last_sid AND l.transaction_id = v.transaction_id
            JOIN snapshots s ON s.id = v.last_sid
            JOIN snapshots vs ON vs.id = v.vanish_sid
            WHERE vs.captured_at >= ?
            """,
            [latest["id"], window_start],
        )
    out = []
    for g in gone:
        remaining = g["seconds_till_expiry"]
        reason = "EXPIRED" if remaining < EXPIRY_WINDOW_SECONDS else "REMOVED"
        rar = rarity_of(g["item_id"])
        # Each vanished row carries its full Listing fields plus the curated
        # item summary and the observation span, so the row reuses the
        # Listing + ItemSummary schemas of the OpenAPI contract.
        out.append(
            {
                "transaction_id": g["transaction_id"],
                "seller_empire_id": g["seller_empire_id"],
                "buyer_character_id": g["buyer_character_id"],
                "item_id": g["item_id"],
                "name": name_of(g["item_id"]),
                **icon_fields(g["item_id"]),
                "item_type": g["item_type"],
                "item_level": g["item_level"],
                "item_count": g["item_count"],
                "item_price": g["item_price"],
                "item_seed": g["item_seed"],
                "seconds_till_expiry": g["seconds_till_expiry"],
                "expires_at": g["expires_at"],
                "rarity": rar[1] if rar else None,
                "rarity_rank": rar[0] if rar else 0,
                "unit_price": round(g["item_price"] / max(g["item_count"], 1), 2),
                "reason": reason,
                "vanished_at": g["vanished_at"],
            }
        )
    out.sort(key=lambda d: d["item_price"], reverse=True)
    return out


# --- best sellers ----------------------------------------------------------


_BEST_SELLER_SORTS = {
    "median_time": "median_time",
    "sales": "sales",
    "item": "item_id",
    "rarity": "rarity_rank",
    "type": "item_type",
    "level": "item_level",
    "active_count": "active_count",
    "current_median_unit_price": "current_median_unit_price",
    "min_time": "min_time",
    "max_time": "max_time",
    "expired": "expired",
    "last_seen": "last_seen",
}

# One row per listing transaction: the snapshot it first and last appeared in,
# its latest unit price and countdown, and the item it is for.  Normally these
# come from the aggregate the writer maintains (``transaction_summary``), which
# is a scan of one row per transaction instead of every listing of every
# snapshot.  A database migrated in place has no aggregate yet, so the marker is
# absent and the view falls back to computing exactly the same rows from the
# history — correct, just slower, until ``cli backfill`` rebuilds it.
_BEST_SELLER_SUMMARY_SQL = """
    SELECT transaction_id, item_id, item_type, item_level,
           first_snapshot_id AS first_sid, last_snapshot_id AS last_sid,
           unit_price, seconds_till_expiry AS expiry
    FROM transaction_summary
    ORDER BY transaction_id
"""
_BEST_SELLER_FALLBACK_SQL = """
    SELECT transaction_id,
           min(snapshot_id) AS first_sid,
           max(snapshot_id) AS last_sid,
           arg_min(item_id, snapshot_id) AS item_id,
           arg_min(item_type, snapshot_id) AS item_type,
           arg_min(item_level, snapshot_id) AS item_level,
           arg_max(item_price / greatest(item_count, 1), snapshot_id) AS unit_price,
           arg_max(seconds_till_expiry, snapshot_id) AS expiry
    FROM listings
    GROUP BY transaction_id
    ORDER BY transaction_id
"""


def best_sellers(
    conn: duckdb.DuckDBPyConnection,
    *,
    order: str = "median_time",
    direction: str = "asc",
    min_sales: int = 1,
) -> list[dict]:
    """Items ranked by how fast their listings sell — time-to-sale.

    For every listing transaction, the observed lifetime is the time from the
    first snapshot it appears in to the first snapshot it is absent from.
    Only listings that vanished with at least a day left on their countdown
    count as sales (sold or withdrawn — indistinguishable, like the live
    observer); EXPIRED listings are tracked separately.  Listings already
    present in the very first snapshot are left-censored — their true listing
    time is unknown — so they count toward ``sales`` but not toward the time
    stats.  With hourly snapshots the lifetime is accurate to within one poll
    interval.

    Only items with at least ``min_sales`` fully observed sales are returned.
    """
    latest = latest_snapshot(conn)
    if latest is None:
        return []
    snaps = _rows(conn, "SELECT id, captured_at FROM snapshots ORDER BY id")
    first_id = snaps[0]["id"]
    snap_ids = [s["id"] for s in snaps]
    snap_times = {s["id"]: s["captured_at"] for s in snaps}
    # The next snapshot after the one a listing was last seen in is when it
    # vanished; a dict keeps the lookup O(1) instead of scanning the list.
    next_sid = {snap_ids[i]: snap_ids[i + 1] for i in range(len(snap_ids) - 1)}
    active_txs = {r["transaction_id"] for r in _rows(conn, "SELECT transaction_id FROM listings WHERE snapshot_id = ?", [latest["id"]])}

    transactions = _rows(conn, _BEST_SELLER_SUMMARY_SQL if summary_snapshot_id(conn) == latest["id"] else _BEST_SELLER_FALLBACK_SQL)

    items: dict[str, dict] = {}
    for r in transactions:
        it = items.setdefault(
            r["item_id"],
            {"item_type": r["item_type"], "item_level": r["item_level"], "sales": 0, "expired": 0, "timed": [], "active_prices": [], "last_seen": 0.0},
        )
        it["last_seen"] = max(it["last_seen"], snap_times[r["last_sid"]])
        if r["transaction_id"] in active_txs:
            it["active_prices"].append(r["unit_price"])
            continue
        if r["expiry"] < EXPIRY_WINDOW_SECONDS:
            it["expired"] += 1
            continue
        it["sales"] += 1
        if r["first_sid"] == first_id:
            continue  # left-censored: true listing time unknown
        following = next_sid.get(r["last_sid"])
        vanished_at = snap_times[following] if following is not None else latest["captured_at"]
        it["timed"].append(vanished_at - snap_times[r["first_sid"]])

    out = []
    for item_id, it in items.items():
        if len(it["timed"]) < min_sales:
            continue
        rar = rarity_of(item_id)
        out.append(
            {
                "item_id": item_id,
                "name": name_of(item_id),
                **icon_fields(item_id),
                "item_type": it["item_type"],
                "item_level": it["item_level"],
                "rarity": rar[1] if rar else None,
                "rarity_rank": rar[0] if rar else 0,
                "sales": it["sales"],
                "timed_sales": len(it["timed"]),
                "expired": it["expired"],
                "median_time": median(it["timed"]) if it["timed"] else None,
                "min_time": min(it["timed"]) if it["timed"] else None,
                "max_time": max(it["timed"]) if it["timed"] else None,
                "active_count": len(it["active_prices"]),
                "current_median_unit_price": median(it["active_prices"]) if it["active_prices"] else None,
                "last_seen": it["last_seen"],
            }
        )

    col = _BEST_SELLER_SORTS.get(order, "median_time")
    if col in ("item_id", "item_type", "last_seen"):
        key = lambda d: d[col]
    else:
        key = lambda d: (d[col] is None, d[col] or 0)
    out.sort(key=key, reverse=direction == "desc")
    return out


# --- best value (crafting) -------------------------------------------------


_CRAFT_VALUE_SORTS = {
    "value_ratio": "value_ratio",
    "item": "item_id",
    "type": "type",
    "rarity": "rarity_rank",
    "craft_cost": "craft_cost",
    "price": "price",
    "listed_now": "listed_now",
}


def crafting_value(
    conn: duckdb.DuckDBPyConnection,
    *,
    order: str = "value_ratio",
    direction: str = "desc",
) -> list[dict]:
    """Craftable items ranked by market price ÷ crafting cost.

    A ratio above 1 means the item sells for more than its ingredients cost, so
    buying the materials and crafting it beats buying the item; below 1 the item
    itself is the cheaper way to get it.  The larger the ratio the better.

    Everything is per unit (a stack's total is divided by its count, as in every
    other view) and an ingredient is priced the way the item page prices it: its
    current median while listed now, else its historical median.  The item's own
    ``price`` is likewise its current median unit price while it is listed now,
    else its historical median — ``listed_now`` and ``price_basis`` say which,
    since an item with no current listing has no live competition.

    Every row is worth acting on: either the item sells above what its
    ingredients cost (craft it), or it is listed right now (buy it).  Only the
    combination that suits nobody — selling below cost with nothing on the
    market — is left out.  An unlisted item is therefore kept when its
    historical median still beats its crafting cost: nothing competes with the
    craft, and the ratio says the market has paid more.

    Items whose ingredients have not all been observed are skipped: a partial
    cost would understate the cost and inflate the ratio.
    """
    latest = latest_snapshot(conn)
    latest_id = latest["id"] if latest else None
    # Only the craftable items and their ingredients are ever priced, so read
    # just those: ``item_key`` is the lowercased id the catalog and the recipes
    # are keyed by, and unlike ``lower(item_id)`` it can be filtered with an
    # index instead of scanning every listing ever stored.
    wanted = {item_id.lower() for item_id in craftable_ids()}
    for item_id in list(wanted):
        for material in (recipe_of(item_id) or {}).get("materials", []):
            if material.get("id"):
                wanted.add(material["id"].lower())
    every: dict[str, list[float]] = {}
    active: dict[str, list[float]] = {}
    spelling: dict[str, str] = {}
    placeholders = ", ".join("?" * len(wanted))
    for r in _rows(
        conn,
        f"SELECT item_id, item_price, item_count, snapshot_id FROM listings WHERE item_key IN ({placeholders})",
        sorted(wanted),
    ):
        key = r["item_id"].lower()
        unit = r["item_price"] / max(r["item_count"], 1)
        every.setdefault(key, []).append(unit)
        spelling.setdefault(key, r["item_id"])
        if r["snapshot_id"] == latest_id:
            active.setdefault(key, []).append(unit)

    def posted_price(key: str) -> float | None:
        cur = active.get(key)
        if cur:
            return median(cur)
        hist = every.get(key)
        return median(hist) if hist else None

    out: list[dict] = []
    for item_id in craftable_ids():
        if item_id not in every:
            continue  # never observed: nothing to compare the cost against
        recipe = recipe_of(item_id) or {}
        materials = recipe.get("materials", [])
        cost = 0.0
        priced = 0
        for m in materials:
            price = posted_price((m.get("id") or "").lower())
            if price is not None and m.get("quantity"):
                cost += price * m["quantity"]
                priced += 1
        if not cost or priced != len(materials):
            continue
        current_median = median(active[item_id]) if active.get(item_id) else None
        historical = median(every[item_id])
        price = current_median if current_median is not None else historical
        ratio = price / cost
        if ratio < 1 and current_median is None:
            continue  # below cost and nothing listed: neither crafting nor buying pays
        row = {
            "item_id": spelling.get(item_id, item_id),
            "type": type_of(item_id),
            "school": recipe.get("school"),
            "craft_cost": round(cost, 2),
            "materials_priced": priced,
            "materials_total": len(materials),
            "price": round(price, 2),
            "price_basis": "current" if current_median is not None else "historical",
            "listed_now": current_median is not None,
            "current_median_unit_price": round(current_median) if current_median is not None else None,
            "median_unit_price": round(historical),
            "active_count": len(active.get(item_id, [])),
            "value_ratio": round(ratio, 2),
        }
        row.update(catalog_fields(item_id))
        rar = rarity_of(item_id)
        row["rarity"] = rar[1] if rar else None
        row["rarity_rank"] = rar[0] if rar else 0
        out.append(row)

    col = _CRAFT_VALUE_SORTS.get(order, "value_ratio")
    if col in ("item_id", "type"):
        key = lambda d: d.get(col) or ""
    else:
        key = lambda d: (d.get(col) is None, d.get(col) or 0)
    out.sort(key=key, reverse=direction == "desc")
    return out


# --- item search -----------------------------------------------------------

SEARCH_LIMIT = 25
MAX_SEARCH_LIMIT = 100


def search_items(
    conn: duckdb.DuckDBPyConnection,
    query: str,
    *,
    limit: int = SEARCH_LIMIT,
) -> list[dict]:
    """Catalog items matching ``query``, each with its market summary.

    The match is catalogue-only (:func:`aoeo_market.catalog.search`), so an item
    that has never been observed is still findable — it simply reports
    ``listed_now: false`` with null prices.  Otherwise the row carries the
    current median unit price while the item is listed and the historical median
    either way, the same numbers the item page and the best-value view use.
    """
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    matches = catalog_search(query, limit)
    if not matches:
        return []
    latest = latest_snapshot(conn)
    latest_id = latest["id"] if latest else None
    keys = [row["item_id"].lower() for row in matches]
    every: dict[str, list[float]] = {}
    active: dict[str, list[float]] = {}
    spelling: dict[str, str] = {}
    placeholders = ", ".join("?" * len(keys))
    for r in _rows(
        conn,
        f"""
        SELECT item_id, item_price, item_count, snapshot_id
        FROM listings WHERE item_key IN ({placeholders})
        """,
        keys,
    ):
        key = r["item_id"].lower()
        unit = r["item_price"] / max(r["item_count"], 1)
        every.setdefault(key, []).append(unit)
        spelling.setdefault(key, r["item_id"])
        if r["snapshot_id"] == latest_id:
            active.setdefault(key, []).append(unit)

    out: list[dict] = []
    for row in matches:
        key = row["item_id"].lower()
        current, historical = active.get(key), every.get(key)
        row["item_id"] = spelling.get(key, row["item_id"])
        row["listed_now"] = bool(current)
        row["active_count"] = len(current or [])
        row["current_median_unit_price"] = round(median(current)) if current else None
        row["median_unit_price"] = round(median(historical)) if historical else None
        out.append(row)
    return out
