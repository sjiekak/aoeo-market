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
    _, _, body = app_for(tmp_path).handle("/api/not-on-sale", {"order": ["median_price"], "dir": ["desc"]})
    assert "Axe_R_I" in body.decode()
    assert "Sword" not in body.decode()


def test_recently_removed_endpoint(tmp_path):
    _, _, body = app_for(tmp_path).handle("/api/recently-removed")
    assert "Axe_R_I" in body.decode()
    assert '"reason"' in body.decode()


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
