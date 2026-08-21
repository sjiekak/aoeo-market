"""Unit tests for the snapshot store — no network or captures required."""

from aoeo_market import store
from aoeo_market.market import Listing


def mk(
    tx: int,
    item_id: str = "Sword_U_III",
    item_type: str = "Trait",
    level: int = 1,
    count: int = 1,
    price: int = 100,
    expiry: int = 90_000,
    seller: int = 7,
) -> Listing:
    return Listing(
        transaction_id=tx,
        seller_empire_id=seller,
        buyer_character_id=-1,
        item_id=item_id,
        item_type=item_type,
        item_level=level,
        item_count=count,
        item_price=price,
        item_seed=0,
        seconds_till_expiry=expiry,
    )


def test_record_and_read_snapshots(tmp_path):
    conn = store.open_store(tmp_path / "m.db")
    s1 = store.record_snapshot(conn, [mk(1, price=100), mk(2, price=300)], captured_at=1000.0)
    s2 = store.record_snapshot(conn, [mk(1, price=120)], captured_at=2000.0)

    assert store.snapshot_count(conn) == 2
    assert store.latest_snapshot(conn)["id"] == s2
    assert store.previous_snapshot(conn, s2)["id"] == s1
    assert store.previous_snapshot(conn, s1) is None

    rows = store.active_listings(conn)
    assert len(rows) == 1
    assert rows[0]["item_price"] == 120
    rows = store.active_listings(conn, s1)
    assert [r["transaction_id"] for r in rows] == [1, 2]
    conn.close()


def test_active_listings_filter_and_sort(tmp_path):
    conn = store.open_store(tmp_path / "m.db")
    store.record_snapshot(
        conn,
        [
            mk(1, item_id="Axe_R_I", item_type="Design", price=50),
            mk(2, item_id="Sword_U_III", item_type="Trait", price=120),
            mk(3, item_id="Shield_E_II", item_type="Trait", price=400),
        ],
        captured_at=1000.0,
    )
    assert [r["item_price"] for r in store.active_listings(conn, sort="price", direction="desc")] == [400, 120, 50]
    assert [r["item_id"] for r in store.active_listings(conn, item_type="Design")] == ["Axe_R_I"]
    assert [r["item_id"] for r in store.active_listings(conn, q="sword")] == ["Sword_U_III"]
    assert store.active_listings(conn, sort="price")[0]["rarity"] == "Rare"
    conn.close()


def test_price_history_series_and_points(tmp_path):
    conn = store.open_store(tmp_path / "m.db")
    store.record_snapshot(conn, [mk(1, price=100), mk(2, price=120), mk(3, price=140)], captured_at=1000.0)
    store.record_snapshot(conn, [mk(4, price=150), mk(5, price=250)], captured_at=2000.0)

    hist = store.price_history(conn, "Sword_U_III")
    assert hist["item_type"] == "Trait"
    assert [s["median"] for s in hist["series"]] == [120.0, 200.0]
    assert [s["count"] for s in hist["series"]] == [3, 2]
    assert len(hist["points"]) == 5
    assert len(hist["current"]) == 2

    assert store.price_history(conn, "never-seen") is None
    conn.close()


def test_items_not_on_sale(tmp_path):
    conn = store.open_store(tmp_path / "m.db")
    store.record_snapshot(conn, [mk(1, item_id="Gone_L_IV", price=100), mk(2, item_id="Still_U_II", price=500)], captured_at=1000.0)
    store.record_snapshot(conn, [mk(3, item_id="Still_U_II", price=600)], captured_at=2000.0)

    rows = store.items_not_on_sale(conn)
    assert [r["item_id"] for r in rows] == ["Gone_L_IV"]
    gone = rows[0]
    assert gone["median_price"] == 100
    assert gone["times_listed"] == 1
    assert gone["last_seen"] == 1000.0
    assert gone["rarity"] == "Legendary"
    conn.close()


def test_items_not_on_sale_orders_by_rarity(tmp_path):
    conn = store.open_store(tmp_path / "m.db")
    store.record_snapshot(
        conn,
        [
            mk(1, item_id="CommonOne_C_I", price=10),
            mk(2, item_id="EpicOne_E_I", price=10),
            mk(3, item_id="Kept_U_I", price=10),
        ],
        captured_at=1000.0,
    )
    store.record_snapshot(conn, [mk(4, item_id="Kept_U_I", price=10)], captured_at=2000.0)

    rows = store.items_not_on_sale(conn, order="rarity", direction="desc")
    assert [r["item_id"] for r in rows] == ["EpicOne_E_I", "CommonOne_C_I"]
    rows = store.items_not_on_sale(conn, order="item", direction="asc")
    assert [r["item_id"] for r in rows] == ["CommonOne_C_I", "EpicOne_E_I"]
    conn.close()


def test_recently_removed_classification(tmp_path):
    conn = store.open_store(tmp_path / "m.db")
    store.record_snapshot(
        conn,
        [
            mk(1, item_id="Sold_U_I", price=300, expiry=200_000),  # long countdown
            mk(2, item_id="Expired_U_I", price=50, expiry=500),  # <1 day left
            mk(3, item_id="StillThere_U_I", price=10, expiry=200_000),
        ],
        captured_at=1000.0,
    )
    store.record_snapshot(conn, [mk(4, item_id="StillThere_U_I", price=10, expiry=150_000)], captured_at=2000.0)

    rows = store.recently_removed(conn)
    by_tx = {r["transaction_id"]: r for r in rows}
    assert by_tx[1]["reason"] == "REMOVED"  # long countdown -> sold/withdrawn
    assert by_tx[2]["reason"] == "EXPIRED"  # <1 day left -> timed out
    # tx 3 vanished too (relisted as tx 4 with the same item id): with a long
    # countdown it reads REMOVED, and it must not be confused with tx 4.
    assert by_tx[3]["reason"] == "REMOVED"
    assert by_tx[3]["item_id"] == "StillThere_U_I"
    assert 4 not in by_tx
    conn.close()


def test_overview_empty_database(tmp_path):
    conn = store.open_store(tmp_path / "m.db")
    o = store.market_overview(conn)
    assert o["snapshot_count"] == 0
    assert o["latest"] is None
    assert o["top_movers"] == []
    assert store.items_not_on_sale(conn) == []
    assert store.recently_removed(conn) == []
    conn.close()


def test_overview_counts_histogram_and_movers(tmp_path):
    conn = store.open_store(tmp_path / "m.db")
    store.record_snapshot(
        conn,
        [
            mk(1, item_id="Axe_R_I", item_type="Design", price=50),
            mk(2, item_id="Axe_R_I", item_type="Design", price=80),
            mk(3, item_id="Sword_E_II", item_type="Trait", price=1000),
        ],
        captured_at=1000.0,
    )
    store.record_snapshot(
        conn,
        [
            mk(4, item_id="Axe_R_I", item_type="Design", price=500),
            mk(5, item_id="Axe_R_I", item_type="Design", price=900),
            mk(6, item_id="Sword_E_II", item_type="Trait", price=1000),
        ],
        captured_at=2000.0,
    )

    o = store.market_overview(conn)
    assert o["snapshot_count"] == 2
    assert o["active_listings"] == 3
    assert o["distinct_items"] == 2
    assert {t["name"]: t["count"] for t in o["type_breakdown"]} == {"Design": 2, "Trait": 1}
    assert o["rarity_breakdown"] == [{"name": "Epic", "count": 1}, {"name": "Rare", "count": 2}]
    assert sum(b["count"] for b in o["price_distribution"]) == 3
    assert [s["count"] for s in o["supply_history"]] == [3, 3]

    movers = {m["item_id"]: m for m in o["top_movers"]}
    assert movers["Axe_R_I"]["median_before"] == 65
    assert movers["Axe_R_I"]["median_now"] == 700
    assert movers["Axe_R_I"]["change_pct"] > 900
    conn.close()


def test_best_sellers_ranks_by_time_to_sale(tmp_path):
    conn = store.open_store(tmp_path / "m.db")
    # s1: only a long-lived listing -> its later sale would be left-censored
    store.record_snapshot(conn, [mk(1, item_id="Old_U_I", price=10, expiry=200_000)], captured_at=1000.0)
    # s2: Fast and Slow first appear (fully observed from here on)
    store.record_snapshot(
        conn,
        [
            mk(1, item_id="Old_U_I", price=10, expiry=200_000),
            mk(2, item_id="Fast_E_I", price=900, expiry=200_000),
            mk(3, item_id="Slow_U_I", price=90, expiry=200_000),
        ],
        captured_at=4600.0,
    )
    # s3: Fast sold (vanished with a long countdown left)
    store.record_snapshot(
        conn,
        [
            mk(1, item_id="Old_U_I", price=10, expiry=200_000),
            mk(3, item_id="Slow_U_I", price=90, expiry=200_000),
        ],
        captured_at=8200.0,
    )
    # s4: Slow sold too, and a fresh Fast listing is back on the market
    store.record_snapshot(
        conn,
        [mk(1, item_id="Old_U_I", price=10, expiry=200_000), mk(4, item_id="Fast_E_I", price=800, expiry=200_000)],
        captured_at=11800.0,
    )

    rows = store.best_sellers(conn)
    assert [r["item_id"] for r in rows] == ["Fast_E_I", "Slow_U_I"]  # fastest first
    fast, slow = rows
    assert fast["median_time"] == 3600.0  # 8200 - 4600 (one hourly gap)
    assert fast["min_time"] == fast["max_time"] == 3600.0
    assert fast["timed_sales"] == 1
    assert fast["rarity"] == "Epic"
    assert fast["active_count"] == 1  # relisted in s4
    assert fast["current_median_price"] == 800.0
    assert slow["median_time"] == 7200.0  # 11800 - 4600 (two gaps)
    assert slow["active_count"] == 0
    assert slow["current_median_price"] is None

    rows = store.best_sellers(conn, order="item", direction="asc")
    assert [r["item_id"] for r in rows] == ["Fast_E_I", "Slow_U_I"]
    conn.close()


def test_best_sellers_censoring_and_expired(tmp_path):
    conn = store.open_store(tmp_path / "m.db")
    # s1: Censored already listed (true start unknown), Expiring almost timed out
    store.record_snapshot(
        conn,
        [mk(1, item_id="Censored_U_I", price=10, expiry=200_000), mk(2, item_id="Expiring_U_I", price=10, expiry=500)],
        captured_at=1000.0,
    )
    # s2: both gone
    store.record_snapshot(conn, [], captured_at=4600.0)

    assert store.best_sellers(conn) == []  # no fully observed sale -> filtered out
    rows = store.best_sellers(conn, min_sales=0)
    by = {r["item_id"]: r for r in rows}
    assert by["Censored_U_I"]["sales"] == 1
    assert by["Censored_U_I"]["timed_sales"] == 0
    assert by["Censored_U_I"]["median_time"] is None
    assert by["Expiring_U_I"]["expired"] == 1
    assert by["Expiring_U_I"]["sales"] == 0
    conn.close()


def test_best_value_ranks_cheap_for_rarity(tmp_path):
    conn = store.open_store(tmp_path / "m.db")
    store.record_snapshot(
        conn,
        [
            # Epic tier: CheapEpic 1000 vs PriceyEpic 2000 -> tier ref 1500
            mk(1, item_id="CheapEpic_E_I", price=1000),
            mk(2, item_id="PriceyEpic_E_I", price=2000),
            # Common tier: CheapCommon 100 vs PriceyCommon 200 -> tier ref 150
            mk(3, item_id="CheapCommon_C_I", price=100),
            mk(4, item_id="PriceyCommon_C_I", price=200),
            # untagged material: excluded by default
            mk(5, item_id="UntaggedMat", price=10),
        ],
        captured_at=1000.0,
    )

    rows = store.best_value(conn)
    by = {r["item_id"]: r for r in rows}
    assert set(by) == {"CheapEpic_E_I", "PriceyEpic_E_I", "CheapCommon_C_I", "PriceyCommon_C_I"}
    assert by["CheapEpic_E_I"]["value_ratio"] == 1.5  # 1500 / 1000
    assert by["PriceyEpic_E_I"]["value_ratio"] == 0.75  # 1500 / 2000
    assert by["CheapEpic_E_I"]["tier_reference_price"] == 1500
    assert by["CheapEpic_E_I"]["cheaper_than_pct"] == 100.0  # cheapest in its tier
    assert by["PriceyEpic_E_I"]["cheaper_than_pct"] == 50.0
    # default order: best value first (1.5 beats 0.75)
    assert rows[0]["value_ratio"] >= rows[1]["value_ratio"]

    # a current cheap listing improves the value ratio over the historical
    # median (the tier reference recomputes: epic medians [1000, 1250] -> 1125)
    store.record_snapshot(conn, [mk(6, item_id="PriceyEpic_E_I", price=500)], captured_at=2000.0)
    rows = store.best_value(conn)
    by = {r["item_id"]: r for r in rows}
    assert by["PriceyEpic_E_I"]["current_median_price"] == 500.0
    assert by["PriceyEpic_E_I"]["value_ratio"] == 2.25  # 1125 / 500
    assert by["PriceyEpic_E_I"]["tier_reference_price"] == 1125
    assert rows[0]["item_id"] == "PriceyEpic_E_I"

    # unrated items appear only when asked
    assert "UntaggedMat" not in {r["item_id"] for r in store.best_value(conn)}
    assert "UntaggedMat" in {r["item_id"] for r in store.best_value(conn, include_unrated=True)}
    conn.close()


def test_median():
    assert store.median([]) == 0.0
    assert store.median([7]) == 7.0
    assert store.median([1, 2]) == 1.5
    assert store.median([3, 1, 2]) == 2.0
