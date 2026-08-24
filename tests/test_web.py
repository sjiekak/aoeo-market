"""Tests for the website JSON API (WebApp.handle — the HTTP handler is a thin
wrapper around it, so these exercise the full routing without a socket)."""

from aoeo_market import store
from aoeo_market.web import WebApp

from .test_store import mk  # reuse the synthetic listing factory


def seed(db) -> None:
    conn = store.open_store(db)
    store.record_snapshot(
        conn,
        [mk(1, item_id="Axe_R_I", item_type="Design", price=50), mk(2, item_id="Sword_U_III", item_type="Trait", price=120)],
        captured_at=1000.0,
    )
    store.record_snapshot(conn, [mk(3, item_id="Sword_U_III", item_type="Trait", price=150)], captured_at=2000.0)
    conn.close()


def app_for(tmp_path) -> WebApp:
    db = tmp_path / "m.db"
    seed(db)
    return WebApp(str(db))


def test_index_and_static_files(tmp_path):
    app = app_for(tmp_path)
    status, ctype, body = app.handle("/healthz")
    assert status == 200 and body == b'{"status": "ok"}'
    status, ctype, body = app.handle("/")
    assert status == 200
    assert "text/html" in ctype
    assert b"<!doctype html>" in body
    assert b"chart.js" in body
    status, ctype, body = app.handle("/static/app.js")
    assert status == 200 and b"api(" in body
    status, _, _ = app.handle("/static/../secret.py")
    assert status == 404
    status, _, _ = app.handle("/no-such-route")
    assert status == 404


def test_readyz_with_database(tmp_path):
    import json

    app = app_for(tmp_path)  # seed() records two snapshots
    status, _, body = app.handle("/readyz")
    assert status == 200
    payload = json.loads(body)
    assert payload == {"status": "ready", "database": "ok", "snapshots": 2}


def test_readyz_missing_database_until_init(tmp_path):
    import json

    from aoeo_market.cli import main

    db = tmp_path / "missing.db"
    app = WebApp(str(db))
    status, _, body = app.handle("/readyz")
    assert status == 503
    assert "not initialized" in json.loads(body)["database"]

    # once the init container (init-db) has run, the same app becomes ready
    assert main(["init-db", "--db", str(db)]) == 0
    status, _, body = app.handle("/readyz")
    assert status == 200
    assert json.loads(body)["snapshots"] == 0


def test_openapi_spec_is_served_and_in_sync(tmp_path):
    import json

    from aoeo_market.web import openapi

    app = app_for(tmp_path)
    status, ctype, body = app.handle("/openapi.json")
    assert status == 200
    assert "application/json" in ctype
    spec = json.loads(body)
    assert spec["openapi"].startswith("3.")
    for path in (
        "/healthz",
        "/readyz",
        "/api/overview",
        "/api/listings",
        "/api/item/{item_id}",
        "/api/not-on-sale",
        "/api/best-sellers",
        "/api/best-value",
        "/api/recently-removed",
    ):
        assert path in spec["paths"], path
    # the public spec must not advertise how data is ingested
    assert "/api/snapshot" not in spec["paths"]
    # parameter enums come from the live sort whitelists
    assert spec["paths"]["/api/listings"]["get"]["parameters"][2]["schema"]["enum"] == ["price", "level", "count", "expiry", "item", "type", "seller"]

    # the internal spec keeps the ingestion contract in sync with the code
    full = openapi.build_spec(include_ingestion=True)
    assert "post" in full["paths"]["/api/snapshot"]
    # the Listing schema must mirror the payload contract exactly
    assert set(full["components"]["schemas"]["Listing"]["properties"]) == set(mk(1).to_dict())


def test_overview_endpoint(tmp_path):
    status, _, body = app_for(tmp_path).handle("/api/overview")
    assert status == 200
    import json

    o = json.loads(body)
    assert o["snapshot_count"] == 2
    assert o["active_listings"] == 1
    assert o["distinct_items"] == 1


def test_listings_endpoint_params(tmp_path):
    app = app_for(tmp_path)
    _, _, body = app.handle("/api/listings", {"sort": ["price"], "dir": ["desc"]})
    assert body.decode().startswith('[{"snapshot_id"')
    # the latest snapshot holds only Sword_U_III (Axe_R_I was in the first)
    _, _, body = app.handle("/api/listings", {"type": ["Trait"], "q": ["sword"]})
    assert "Sword_U_III" in body.decode() and "Axe" not in body.decode()
    _, _, body = app.handle("/api/listings", {"type": ["Design"]})
    assert body == b"[]"


def test_item_endpoint_and_404(tmp_path):
    app = app_for(tmp_path)
    status, _, body = app.handle("/api/item/Sword_U_III")
    assert status == 200
    assert '"item_id": "Sword_U_III"' in body.decode()
    assert '"series"' in body.decode()
    status, _, body = app.handle("/api/item/unknown")
    assert status == 404
    assert "never observed" in body.decode()


def test_not_on_sale_endpoint(tmp_path):
    _, _, body = app_for(tmp_path).handle("/api/not-on-sale", {"order": ["median_unit_price"], "dir": ["desc"]})
    assert "Axe_R_I" in body.decode()
    assert "Sword" not in body.decode()


def test_best_sellers_endpoint(tmp_path):
    app = app_for(tmp_path)
    # the seed's only sale is left-censored (present in the first snapshot)
    _, _, body = app.handle("/api/best-sellers")
    assert body == b"[]"
    _, _, body = app.handle("/api/best-sellers", {"min_sales": ["0"]})
    assert "Axe_R_I" in body.decode()
    assert '"median_time": null' in body.decode()
    status, _, _ = app.handle("/api/best-sellers", {"min_sales": ["abc"]})
    assert status == 400


def test_best_value_endpoint(tmp_path):
    app = app_for(tmp_path)
    # the seed's items are both rarity-tagged: Axe_R_I (gone) and Sword_U_III
    _, _, body = app.handle("/api/best-value")
    assert "Sword_U_III" in body.decode()
    assert "Axe_R_I" in body.decode()
    assert '"value_ratio"' in body.decode()
    status, _, _ = app.handle("/api/best-value", {"include_unrated": ["1"]})
    assert status == 200
    status, _, _ = app.handle("/api/best-value", {"include_unrated": ["abc"]})
    assert status == 400


def test_best_value_include_unrated(tmp_path):
    db = tmp_path / "u.db"
    conn = store.open_store(db)
    store.record_snapshot(conn, [mk(1, item_id="Sword_U_III", price=120), mk(2, item_id="PlainMat", price=10)], captured_at=1000.0)
    conn.close()
    app = WebApp(str(db))
    _, _, body = app.handle("/api/best-value")
    assert "PlainMat" not in body.decode()
    _, _, body = app.handle("/api/best-value", {"include_unrated": ["1"]})
    assert "PlainMat" in body.decode()
    assert '"rarity": null' in body.decode()


def test_post_snapshot_and_read_back(tmp_path):
    import json

    app = WebApp(str(tmp_path / "fresh.db"))  # file does not exist yet
    payload = json.dumps({"listings": [mk(1, item_id="Sword_U_III", price=120).to_dict(), mk(2, item_id="Axe_R_I", price=50).to_dict()]}).encode()
    status, _, body = app.handle_post("/api/snapshot", payload)
    assert status == 201
    assert json.loads(body) == {"snapshot_id": 1, "listings": 2}

    status, _, body = app.handle("/api/overview")
    overview = json.loads(body)
    assert overview["snapshot_count"] == 1
    assert overview["active_listings"] == 2
    status, _, body = app.handle("/api/listings", {"sort": ["price"]})
    assert [r["item_id"] for r in json.loads(body)] == ["Axe_R_I", "Sword_U_III"]


def test_post_snapshot_captured_at(tmp_path):
    import json

    app = WebApp(str(tmp_path / "fresh.db"))
    payload = json.dumps({"listings": [mk(1).to_dict()], "captured_at": 1234.5}).encode()
    status, _, body = app.handle_post("/api/snapshot", payload)
    assert status == 201
    status, _, body = app.handle("/api/overview")
    assert json.loads(body)["latest"]["captured_at"] == 1234.5


def test_post_snapshot_validation(tmp_path):
    import json

    app = app_for(tmp_path)
    assert app.handle_post("/api/snapshot", b"not json")[0] == 400
    assert app.handle_post("/api/snapshot", json.dumps({"nope": 1}).encode())[0] == 400
    assert app.handle_post("/api/snapshot", json.dumps({"listings": [{}]}).encode())[0] == 400
    bad = mk(1).to_dict()
    bad["item_price"] = "lots"
    assert app.handle_post("/api/snapshot", json.dumps({"listings": [bad]}).encode())[0] == 400
    assert app.handle_post("/api/snapshot", json.dumps({"listings": [mk(1).to_dict()], "captured_at": "x"}).encode())[0] == 400
    assert app.handle_post("/api/nope", b"{}")[0] == 404


def test_recently_removed_endpoint(tmp_path):
    _, _, body = app_for(tmp_path).handle("/api/recently-removed")
    assert "Axe_R_I" in body.decode()
    assert '"reason"' in body.decode()


def test_recently_removed_window_param(tmp_path):
    app = app_for(tmp_path)
    # A valid number of seconds still returns the listing that vanished in the seed data.
    status, _, body = app.handle("/api/recently-removed", {"window": ["86400"]})
    assert status == 200
    assert "Axe_R_I" in body.decode()
    # A non-numeric or non-positive window is rejected with a 400.
    status, _, body = app.handle("/api/recently-removed", {"window": ["abc"]})
    assert status == 400
    assert "number of seconds" in body.decode()
    status, _, body = app.handle("/api/recently-removed", {"window": ["0"]})
    assert status == 400
    assert "positive" in body.decode()


def test_empty_database_responses(tmp_path):
    app = WebApp(str(tmp_path / "empty.db"))
    status, _, body = app.handle("/api/overview")
    assert status == 200
    assert '"snapshot_count": 0' in body.decode()
    _, _, body = app.handle("/api/listings")
    assert body == b"[]"
    _, _, body = app.handle("/api/not-on-sale")
    assert body == b"[]"
    _, _, body = app.handle("/api/recently-removed")
    assert body == b"[]"
