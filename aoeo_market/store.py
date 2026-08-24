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
    listings(snapshot_id, transaction_id, ...)         -- active listings per snapshot

All times are Unix timestamps (UTC seconds).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

import duckdb

from .catalog import fields as catalog_fields
from .catalog import name_of, rarity_of
from .market import Listing

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
        item_type VARCHAR NOT NULL,
        item_level BIGINT NOT NULL,
        item_count BIGINT NOT NULL,
        item_price BIGINT NOT NULL,
        item_seed BIGINT NOT NULL,
        seconds_till_expiry BIGINT NOT NULL,
        PRIMARY KEY (snapshot_id, transaction_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_listings_item_price ON listings(item_id, item_price)",
    "CREATE INDEX IF NOT EXISTS idx_listings_snapshot ON listings(snapshot_id)",
    "CREATE INDEX IF NOT EXISTS idx_listings_item_type ON listings(item_type)",
)

# One day: the observer's expiry window — a listing that vanishes with less
# than this left on its countdown is read as EXPIRED, otherwise sold/withdrawn.
EXPIRY_WINDOW_SECONDS = 86400.0

_LOCK_ATTEMPTS = 30
_LOCK_DELAY = 0.1  # seconds; the writer's exclusive lock is held for milliseconds


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
    """Append one snapshot of *listings* and return its snapshot id."""
    if captured_at is None:
        captured_at = time.time()
    rows = [
        (
            l.transaction_id,
            l.seller_empire_id,
            l.buyer_character_id,
            l.item_id,
            l.item_type,
            l.item_level,
            l.item_count,
            l.item_price,
            l.item_seed,
            l.seconds_till_expiry,
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
                "INSERT INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(snapshot_id, *row) for row in rows],
            )
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
    rows = _rows(conn, f"SELECT * FROM listings WHERE {where} ORDER BY {col} {direction.upper()}, item_id", params)
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
        movers.append(
            {
                "item_id": item_id,
                "name": name_of(item_id),
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


def price_history(conn: duckdb.DuckDBPyConnection, item_id: str, max_points: int = 2000) -> dict | None:
    """Current listings plus the price series of one item across all snapshots.

    Prices are per unit (item_price / item_count) so listings of different
    stack sizes stay comparable.  Returns ``None`` when the item was never
    observed.  The raw scatter points are downsampled evenly to *max_points*
    so long histories stay chartable.
    """
    rows = _rows(
        conn,
        """
        SELECT s.id AS sid, s.captured_at AS t, l.item_price AS price, l.item_count AS count,
               l.item_type AS item_type, l.item_level AS item_level
        FROM listings l JOIN snapshots s ON s.id = l.snapshot_id
        WHERE l.item_id = ?
        ORDER BY s.id, l.item_price
        """,
        [item_id],
    )
    if not rows:
        return None

    latest = latest_snapshot(conn)
    series: dict[int, dict] = {}
    points: list[dict] = []
    for r in rows:
        sid = r["sid"]
        unit = r["price"] / max(r["count"], 1)
        series.setdefault(
            sid,
            {"t": r["t"], "prices": [], "count": 0, "item_type": r["item_type"], "item_level": r["item_level"]},
        )
        series[sid]["prices"].append(unit)
        series[sid]["count"] += 1
        points.append({"t": r["t"], "price": round(unit, 2)})

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

    if len(points) > max_points:
        step = len(points) / max_points
        points = [points[int(i * step)] for i in range(max_points)]

    meta = series[max(series)]
    rar = rarity_of(item_id)
    extra = catalog_fields(item_id)
    name = extra.pop("name", None)
    return {
        "item_id": item_id,
        "name": name,
        "item_type": meta["item_type"],
        "item_level": meta["item_level"],
        "rarity": rar[1] if rar else None,
        "rarity_rank": rar[0] if rar else 0,
        **extra,
        "current": current,
        "series": ordered,
        "points": points,
    }


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


def recently_removed(conn: duckdb.DuckDBPyConnection) -> list[dict]:
    """Listings that vanished between the two most recent snapshots.

    Classified like the live observer: EXPIRED when the listing timed out with
    less than a day left on its countdown, REMOVED (sold or withdrawn —
    indistinguishable) otherwise.
    """
    latest = latest_snapshot(conn)
    if latest is None:
        return []
    prev = previous_snapshot(conn, latest["id"])
    if prev is None:
        return []
    gone = _rows(
        conn,
        """
        SELECT l.*, s.captured_at AS last_seen
        FROM listings l JOIN snapshots s ON s.id = l.snapshot_id
        WHERE l.snapshot_id = ? AND l.transaction_id NOT IN (
            SELECT transaction_id FROM listings WHERE snapshot_id = ?
        )
        """,
        [prev["id"], latest["id"]],
    )
    out = []
    for g in gone:
        remaining = g["seconds_till_expiry"]
        reason = "EXPIRED" if remaining < EXPIRY_WINDOW_SECONDS else "REMOVED"
        rar = rarity_of(g["item_id"])
        out.append(
            {
                "transaction_id": g["transaction_id"],
                "item_id": g["item_id"],
                "name": name_of(g["item_id"]),
                "item_type": g["item_type"],
                "item_level": g["item_level"],
                "rarity": rar[1] if rar else None,
                "rarity_rank": rar[0] if rar else 0,
                "item_price": g["item_price"],
                "seller_empire_id": g["seller_empire_id"],
                "reason": reason,
                "vanished_at": latest["captured_at"],
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
    snap_times = {s["id"]: s["captured_at"] for s in snaps}
    snap_ids = [s["id"] for s in snaps]
    active_txs = {r["transaction_id"] for r in _rows(conn, "SELECT transaction_id FROM listings WHERE snapshot_id = ?", [latest["id"]])}

    txs: dict[int, dict] = {}
    items: dict[str, dict] = {}
    for r in _rows(
        conn,
        "SELECT transaction_id, snapshot_id, item_id, item_type, item_level, item_price, item_count, seconds_till_expiry "
        "FROM listings ORDER BY transaction_id, snapshot_id",
    ):
        t = txs.get(r["transaction_id"])
        if t is None:
            t = txs[r["transaction_id"]] = {
                "first_sid": r["snapshot_id"],
                "last_sid": r["snapshot_id"],
                "item_id": r["item_id"],
                "unit_price": r["item_price"] / max(r["item_count"], 1),
                "expiry": r["seconds_till_expiry"],
            }
        else:
            t["last_sid"] = r["snapshot_id"]
            t["unit_price"] = r["item_price"] / max(r["item_count"], 1)
            t["expiry"] = r["seconds_till_expiry"]
        items.setdefault(
            r["item_id"],
            {"item_type": r["item_type"], "item_level": r["item_level"], "sales": 0, "expired": 0, "timed": [], "active_prices": [], "last_seen": 0.0},
        )

    for tx, t in txs.items():
        it = items[t["item_id"]]
        it["last_seen"] = max(it["last_seen"], snap_times[t["last_sid"]])
        if tx in active_txs:
            it["active_prices"].append(t["unit_price"])
            continue
        if t["expiry"] < EXPIRY_WINDOW_SECONDS:
            it["expired"] += 1
            continue
        it["sales"] += 1
        if t["first_sid"] == first_id:
            continue  # left-censored: true listing time unknown
        next_idx = snap_ids.index(t["last_sid"]) + 1
        vanished_at = snap_times[snap_ids[next_idx]] if next_idx < len(snap_ids) else latest["captured_at"]
        it["timed"].append(vanished_at - snap_times[t["first_sid"]])

    out = []
    for item_id, it in items.items():
        if len(it["timed"]) < min_sales:
            continue
        rar = rarity_of(item_id)
        out.append(
            {
                "item_id": item_id,
                "name": name_of(item_id),
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


# --- best value ------------------------------------------------------------


_BEST_VALUE_SORTS = {
    "value_ratio": "value_ratio",
    "rarity": "rarity_rank",
    "item": "item_id",
    "type": "item_type",
    "level": "item_level",
    "median_unit_price": "median_unit_price",
    "current_median_unit_price": "current_median_unit_price",
    "current_min_unit_price": "current_min_unit_price",
    "cheaper_than_pct": "cheaper_than_pct",
    "active_count": "active_count",
    "times_listed": "times_listed",
}


def best_value(
    conn: duckdb.DuckDBPyConnection,
    *,
    order: str = "value_ratio",
    direction: str = "desc",
    include_unrated: bool = False,
) -> list[dict]:
    """Items ranked by how cheap they are for their rarity ("best value").

    Prices are per unit (item_price / item_count) so stack sizes stay
    comparable.  For every rarity tier the reference price is the median of
    the items' historical median unit prices (each item counts once).  An
    item's value ratio is ``tier_reference / effective_price``, where the
    effective price is the item's current median unit price when it is on
    sale now and its historical median otherwise — a ratio of 2 means it
    trades at half the typical price of its rarity.  ``cheaper_than_pct`` is
    the share (0..100) of same-rarity items whose median unit price is at
    least this item's, i.e. how cheap the item ranks within its rarity.
    Items without a rarity suffix are excluded unless ``include_unrated`` is
    set.
    """
    latest = latest_snapshot(conn)
    if latest is None:
        return []

    items: dict[str, dict] = {}
    for r in _rows(
        conn,
        "SELECT item_id, item_type, item_level, item_price, item_count, snapshot_id FROM listings ORDER BY item_id, snapshot_id",
    ):
        it = items.get(r["item_id"])
        if it is None:
            it = items[r["item_id"]] = {
                "item_type": r["item_type"],
                "item_level": r["item_level"],
                "prices": [],
                "active_prices": [],
                "times_listed": 0,
            }
        else:
            it["item_type"] = r["item_type"]
            it["item_level"] = r["item_level"]
        it["prices"].append(r["item_price"] / max(r["item_count"], 1))
        it["times_listed"] += 1
        if r["snapshot_id"] == latest["id"]:
            it["active_prices"].append(r["item_price"] / max(r["item_count"], 1))

    for item_id, it in items.items():
        it["rarity"] = rarity_of(item_id)
        it["median_unit_price"] = median(it["prices"])
        it["min_unit_price"] = min(it["prices"])
        it["max_unit_price"] = max(it["prices"])
        it["current_median_unit_price"] = median(it["active_prices"]) if it["active_prices"] else None
        it["current_min_unit_price"] = min(it["active_prices"]) if it["active_prices"] else None
        it["active_count"] = len(it["active_prices"])

    tiers: dict[int, list[float]] = {}
    for it in items.values():
        rank = it["rarity"][0] if it["rarity"] else 0
        if rank == 0 and not include_unrated:
            continue
        tiers.setdefault(rank, []).append(it["median_unit_price"])
    tier_ref = {rank: median(ps) for rank, ps in tiers.items()}

    out = []
    for item_id, it in items.items():
        rank = it["rarity"][0] if it["rarity"] else 0
        if rank == 0 and not include_unrated:
            continue
        ref = tier_ref[rank]
        effective = it["current_median_unit_price"] if it["current_median_unit_price"] is not None else it["median_unit_price"]
        tier_medians = tiers[rank]
        pct = 100.0 * sum(1 for m in tier_medians if m >= it["median_unit_price"]) / len(tier_medians)
        out.append(
            {
                "item_id": item_id,
                "name": name_of(item_id),
                "item_type": it["item_type"],
                "item_level": it["item_level"],
                "rarity": it["rarity"][1] if it["rarity"] else None,
                "rarity_rank": rank,
                "tier_reference_price": round(ref),
                "median_unit_price": round(it["median_unit_price"]),
                "min_unit_price": it["min_unit_price"],
                "max_unit_price": it["max_unit_price"],
                "current_median_unit_price": round(it["current_median_unit_price"]) if it["current_median_unit_price"] is not None else None,
                "current_min_unit_price": it["current_min_unit_price"],
                "active_count": it["active_count"],
                "times_listed": it["times_listed"],
                "value_ratio": round(ref / effective, 2) if effective else None,
                "cheaper_than_pct": round(pct, 1),
            }
        )

    col = _BEST_VALUE_SORTS.get(order, "value_ratio")
    if col in ("item_id", "item_type"):
        key = lambda d: d[col]
    else:
        key = lambda d: (d[col] is None, d[col] or 0)
    out.sort(key=key, reverse=direction == "desc")
    return out
