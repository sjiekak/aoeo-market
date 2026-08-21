"""Persistent market snapshot store (SQLite).

The live ``fetch --store`` command records one immutable snapshot per run —
every active listing plus the wall-clock time it was taken — and the website
reads those snapshots back for the trading-intelligence views.  Snapshots are
append-only: queries never mutate, so the database can be read by the web
server while the cron fetch writes.

Schema::

    snapshots(id INTEGER PK, captured_at REAL)          -- one row per fetch
    listings(snapshot_id FK, transaction_id, ...)       -- active listings per snapshot

All times are Unix timestamps (UTC seconds).
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable, Sequence

from .market import Listing, rarity_of

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS listings (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    transaction_id INTEGER NOT NULL,
    seller_empire_id INTEGER NOT NULL,
    buyer_character_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    item_type TEXT NOT NULL,
    item_level INTEGER NOT NULL,
    item_count INTEGER NOT NULL,
    item_price INTEGER NOT NULL,
    item_seed INTEGER NOT NULL,
    seconds_till_expiry INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, transaction_id)
);
CREATE INDEX IF NOT EXISTS idx_listings_item_price ON listings(item_id, item_price);
CREATE INDEX IF NOT EXISTS idx_listings_snapshot ON listings(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_listings_item_type ON listings(item_type);
"""

# One day: the observer's expiry window — a listing that vanishes with less
# than this left on its countdown is read as EXPIRED, otherwise sold/withdrawn.
EXPIRY_WINDOW_SECONDS = 86400.0


def open_store(path: str | os.PathLike) -> sqlite3.Connection:
    """Open (creating if needed) the snapshot database at *path*."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def record_snapshot(
    conn: sqlite3.Connection,
    listings: Iterable[Listing],
    captured_at: float | None = None,
) -> int:
    """Append one snapshot of *listings* and return its snapshot id."""
    if captured_at is None:
        captured_at = time.time()
    cur = conn.execute("INSERT INTO snapshots(captured_at) VALUES (?)", (captured_at,))
    snapshot_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                snapshot_id,
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
        ],
    )
    conn.commit()
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


def latest_snapshot(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT id, captured_at FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return {"id": row["id"], "captured_at": row["captured_at"]} if row else None


def previous_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict | None:
    row = conn.execute("SELECT id, captured_at FROM snapshots WHERE id < ? ORDER BY id DESC LIMIT 1", (snapshot_id,)).fetchone()
    return {"id": row["id"], "captured_at": row["captured_at"]} if row else None


def snapshot_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]


# --- listing views ---------------------------------------------------------


def _listing_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    rar = rarity_of(d["item_id"])
    d["rarity"] = rar[1] if rar else None
    d["rarity_rank"] = rar[0] if rar else 0
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
    conn: sqlite3.Connection,
    snapshot_id: int | None = None,
    *,
    item_type: str | None = None,
    q: str | None = None,
    sort: str = "price",
    direction: str = "asc",
) -> list[dict]:
    """Listings of one snapshot (the latest by default), filtered and sorted.

    ``sort`` must be a key of :data:`_SORT_COLUMNS`; ``direction`` ``asc`` or
    ``desc``.  ``q`` is a case-insensitive substring filter on item id.
    """
    if snapshot_id is None:
        latest = latest_snapshot(conn)
        snapshot_id = latest["id"] if latest else -1
    where = "snapshot_id = ?"
    params: list = [snapshot_id]
    if item_type:
        where += " AND item_type = ?"
        params.append(item_type)
    if q:
        where += " AND item_id LIKE ?"
        params.append(f"%{q}%")
    col = _SORT_COLUMNS.get(sort, "item_price")
    if direction not in ("asc", "desc"):
        direction = "asc"
    rows = conn.execute(
        f"SELECT * FROM listings WHERE {where} ORDER BY {col} {direction.upper()}, item_id",
        params,
    ).fetchall()
    return [_listing_dict(r) for r in rows]


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


def market_overview(conn: sqlite3.Connection, top_movers: int = 15) -> dict:
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

    active = conn.execute("SELECT COUNT(*) FROM listings WHERE snapshot_id = ?", (sid,)).fetchone()[0]
    distinct = conn.execute("SELECT COUNT(DISTINCT item_id) FROM listings WHERE snapshot_id = ?", (sid,)).fetchone()[0]
    types = conn.execute(
        "SELECT item_type AS name, COUNT(*) AS count FROM listings WHERE snapshot_id = ? GROUP BY item_type ORDER BY count DESC",
        (sid,),
    ).fetchall()
    prices = [r["item_price"] for r in conn.execute("SELECT item_price FROM listings WHERE snapshot_id = ?", (sid,))]
    supply = conn.execute(
        """
        SELECT s.captured_at AS t, COUNT(l.transaction_id) AS count
        FROM snapshots s LEFT JOIN listings l ON l.snapshot_id = s.id
        GROUP BY s.id ORDER BY s.id
        """
    ).fetchall()

    # Rarity histogram: rarity letters live in the item id (see market.rarity_of).
    rarity = conn.execute("SELECT item_id FROM listings WHERE snapshot_id = ?", (sid,)).fetchall()
    rarity_bins: dict[str, int] = {}
    for r in rarity:
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
        "supply_history": [dict(r) for r in supply],
        "type_breakdown": [dict(r) for r in types],
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


def _price_movers(conn: sqlite3.Connection, sid: int, prev_sid: int | None, top: int) -> list[dict]:
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
                "median_before": round(med_before),
                "median_now": round(med_now),
                "change_pct": round(pct, 1),
            }
        )
    movers.sort(key=lambda m: -abs(m["change_pct"]))
    return movers[:top]


def _median_prices_by_item(conn: sqlite3.Connection, snapshot_id: int) -> dict[str, float]:
    rows = conn.execute(
        "SELECT item_id, item_price FROM listings WHERE snapshot_id = ? ORDER BY item_id, item_price",
        (snapshot_id,),
    )
    out: dict[str, list[int]] = {}
    for r in rows:
        out.setdefault(r["item_id"], []).append(r["item_price"])
    return {k: median(v) for k, v in out.items()}


# --- per-item history ------------------------------------------------------


def price_history(conn: sqlite3.Connection, item_id: str, max_points: int = 2000) -> dict | None:
    """Current listings plus the price series of one item across all snapshots.

    Returns ``None`` when the item was never observed.  The raw scatter points
    are downsampled evenly to *max_points* so long histories stay chartable.
    """
    rows = conn.execute(
        """
        SELECT s.id AS sid, s.captured_at AS t, l.item_price AS price,
               l.item_type AS item_type, l.item_level AS item_level
        FROM listings l JOIN snapshots s ON s.id = l.snapshot_id
        WHERE l.item_id = ?
        ORDER BY s.id, l.item_price
        """,
        (item_id,),
    ).fetchall()
    if not rows:
        return None

    latest = latest_snapshot(conn)
    series: dict[int, dict] = {}
    points: list[dict] = []
    for r in rows:
        sid = r["sid"]
        series.setdefault(
            sid,
            {"t": r["t"], "prices": [], "count": 0, "item_type": r["item_type"], "item_level": r["item_level"]},
        )
        series[sid]["prices"].append(r["price"])
        series[sid]["count"] += 1
        points.append({"t": r["t"], "price": r["price"]})

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
    return {
        "item_id": item_id,
        "item_type": meta["item_type"],
        "item_level": meta["item_level"],
        "rarity": rar[1] if rar else None,
        "rarity_rank": rar[0] if rar else 0,
        "current": current,
        "series": ordered,
        "points": points,
    }


# --- not-on-sale / recently-removed ---------------------------------------


_NOT_SALE_SORTS = {
    "median_price": "median_price",
    "rarity": "rarity_rank",
    "item": "item_id",
    "type": "item_type",
    "level": "item_level",
    "last_seen": "last_seen",
    "times_listed": "times_listed",
    "max_price": "max_price",
    "min_price": "min_price",
}


def items_not_on_sale(
    conn: sqlite3.Connection,
    *,
    order: str = "median_price",
    direction: str = "desc",
) -> list[dict]:
    """Items seen historically that have **no active listing right now**.

    Each row carries the item's historical price stats so traders can see what
    is currently unavailable and what it traded for.  ``order`` is one of
    :data:`_NOT_SALE_SORTS`.
    """
    latest = latest_snapshot(conn)
    if latest is None:
        return []
    active_ids = {r["item_id"] for r in conn.execute("SELECT DISTINCT item_id FROM listings WHERE snapshot_id = ?", (latest["id"],))}

    out: list[dict] = []
    for r in conn.execute(
        """
        SELECT item_id, item_type, item_level,
               COUNT(*) AS times_listed, MIN(item_price) AS min_price, MAX(item_price) AS max_price
        FROM listings
        GROUP BY item_id
        """
    ):
        if r["item_id"] in active_ids:
            continue
        prices = [p["item_price"] for p in conn.execute("SELECT item_price FROM listings WHERE item_id = ? ORDER BY item_price", (r["item_id"],))]
        last = conn.execute(
            """
            SELECT s.captured_at AS last_seen, l.item_type AS t, l.item_level AS lvl
            FROM listings l JOIN snapshots s ON s.id = l.snapshot_id
            WHERE l.item_id = ? ORDER BY s.id DESC LIMIT 1
            """,
            (r["item_id"],),
        ).fetchone()
        rar = rarity_of(r["item_id"])
        out.append(
            {
                "item_id": r["item_id"],
                "item_type": last["t"] if last else r["item_type"],
                "item_level": last["lvl"] if last else r["item_level"],
                "rarity": rar[1] if rar else None,
                "rarity_rank": rar[0] if rar else 0,
                "median_price": median(prices),
                "min_price": r["min_price"],
                "max_price": r["max_price"],
                "times_listed": r["times_listed"],
                "last_seen": last["last_seen"] if last else None,
            }
        )

    col = _NOT_SALE_SORTS.get(order, "median_price")
    if col in ("item_id", "item_type", "last_seen"):
        key = lambda d: d[col]
    else:
        key = lambda d: (d[col] is not None, d[col] or 0)
    out.sort(key=key, reverse=direction == "desc")
    return out


def recently_removed(conn: sqlite3.Connection) -> list[dict]:
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
    gone = conn.execute(
        """
        SELECT l.*, s.captured_at AS last_seen
        FROM listings l JOIN snapshots s ON s.id = l.snapshot_id
        WHERE l.snapshot_id = ? AND l.transaction_id NOT IN (
            SELECT transaction_id FROM listings WHERE snapshot_id = ?
        )
        """,
        (prev["id"], latest["id"]),
    ).fetchall()
    out = []
    for g in gone:
        remaining = g["seconds_till_expiry"]
        reason = "EXPIRED" if remaining < EXPIRY_WINDOW_SECONDS else "REMOVED"
        rar = rarity_of(g["item_id"])
        out.append(
            {
                "transaction_id": g["transaction_id"],
                "item_id": g["item_id"],
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
