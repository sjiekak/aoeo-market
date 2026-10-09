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


def http(url: str, *, data: bytes | None = None, method: str | None = None) -> tuple[int, bytes]:
    """One HTTP request against a loopback test listener -> ``(status, body)``."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def resolve_schema(schema: dict, schemas: dict) -> tuple[dict, list[str]]:
    """Flatten an ``allOf``-composed schema into its effective properties.

    Returns ``(properties, required)`` with every ``$ref`` part merged in, so
    tests can compare a composed schema against a concrete payload shape.
    """
    props: dict = {}
    required: list[str] = []
    for part in schema.get("allOf", []):
        if "$ref" in part:
            name = part["$ref"].rsplit("/", 1)[-1]
            part = schemas[name]
        p, r = resolve_schema(part, schemas)
        props.update(p)
        required.extend(r)
    props.update(schema.get("properties", {}))
    required.extend(schema.get("required", []))
    return props, required


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


def test_dashboard_shell_is_mobile_ready(tmp_path):
    """The dashboard must not push the page sideways on a phone.

    Every table is deliberately ``nowrap`` and several are wider than a phone
    viewport, so each one is wrapped in the ``.table-wrap`` scroll container;
    a table added without it would stretch the whole page again (which used to
    clip the sticky header and every card at the viewport edge).  Two pieces of
    front-end wiring go with it, so the assertions below pin all three:

    * the ``.table-top`` twin scroll bar above each table, mirrored by app.js,
      which is what lets a long table be panned without scrolling to its end;
    * the ``data-label`` on every cell and the ``≤640px`` card rules that stack
      a row into labeled lines on a phone, with ``table.sortable`` keeping the
      header buttons (the only way to sort there).
    """
    app = app_for(tmp_path)
    _, _, html = app.handle("/")
    _, _, css = app.handle("/static/style.css")
    _, _, js = app.handle("/static/app.js")

    tables = html.count(b"<table")
    assert tables >= 1
    assert html.count(b'<div class="table-wrap">') == tables  # none left unwrapped
    assert b'name="viewport"' in html

    assert b".table-wrap {" in css and b"overflow-x: auto" in css
    assert b"@media (max-width: 640px)" in css  # the phone breakpoint
    assert b"flex-wrap: nowrap" in css  # the tab row scrolls instead of stacking

    # a wide table scrolls from above as well as from below
    assert b".table-top {" in css and b".table-top[hidden]" in css
    assert b"table-top-spacer" in css and b"table-top" in js

    # on a phone the cells stack into cards, captioned from their column header
    assert b"attr(data-label)" in css and b"dataset.label" in js
    assert b"table.sortable thead" in css and b"sortable" in js
    assert b"MutationObserver" in js  # rows rebuilt by a render are re-captioned


def test_item_pages_serve_the_dashboard_shell(tmp_path):
    """Every item has its own page URL: /item/<item_id> answers with the shell
    and the client opens the item view from the path."""
    app = app_for(tmp_path)
    status, ctype, body = app.handle("/item/Xerxes_L_IV")
    assert status == 200
    assert "text/html" in ctype
    assert b"<!doctype html>" in body
    assert b"/static/app.js" in body  # the shell bootstraps the item view
    # the id is not resolved server-side, so any item (or unknown one) gets the
    # shell; the client's API call renders the item or its not-found state
    assert app.handle("/item/4PureGoldIngot")[0] == 200
    assert app.handle("/item/never-seen")[0] == 200
    # the bare prefix (and the prefix without a slash) is not a page
    assert app.handle("/item/")[0] == 404
    assert app.handle("/item")[0] == 404


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
        "/api/search",
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
    # the Listing schema (StockItem + marketplace fields via allOf) must
    # mirror the wire payload contract exactly
    schemas = full["components"]["schemas"]
    listing_props, listing_required = resolve_schema(schemas["Listing"], schemas)
    assert set(listing_props) == set(mk(1).to_dict())
    assert set(listing_required) == set(mk(1).to_dict())
    # the stock item part carries exactly the item fields of the record
    stock_props, _ = resolve_schema(schemas["StockItem"], schemas)
    assert stock_props == schemas["StockItem"]["properties"]
    # every listing-shaped row reuses the shared Listing schema
    wire = set(mk(1).to_dict())
    for name in ("ListingRow", "PreviousListing", "RemovedListing"):
        row_props, row_required = resolve_schema(schemas[name], schemas)
        assert wire <= set(row_props), f"{name} must reuse every Listing field"
        assert wire <= set(row_required), f"{name} must require every Listing field"
    # the item detail rows are the same listing models, not ad-hoc objects
    detail_props, _ = resolve_schema(schemas["ItemDetail"], schemas)
    assert detail_props["current"]["items"] == {"$ref": "#/components/schemas/ListingRow"}
    assert detail_props["previous"]["items"] == {"$ref": "#/components/schemas/PreviousListing"}
    # the removal classification mirrors the observer's enum
    from aoeo_market.observer import RemovalReason

    assert schemas["RemovalReason"]["enum"] == [r.value for r in RemovalReason]


def test_every_array_items_is_a_named_schema():
    """Every array in the spec (requests and responses) types its items with
    a ``$ref`` to a component schema — inline ``{"type": "object"}`` items
    were the old loose contract."""
    from aoeo_market.web import openapi

    spec = openapi.build_spec(include_ingestion=True)
    arrays: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "array":
                arrays.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(spec)
    assert arrays, "the spec should contain typed arrays"
    schemas = spec["components"]["schemas"]
    for node in arrays:
        items = node["items"]
        assert isinstance(items, dict) and "$ref" in items, f"array items must be a named $ref, got {items!r}"
        name = items["$ref"].rsplit("/", 1)[-1]
        assert name in schemas, name
        assert node.get("type") != "object"


def test_composed_schemas_do_not_claim_additional_properties():
    """OpenAPI 3.0 evaluates ``additionalProperties`` per schema object, so a
    strict part of an ``allOf`` would reject its siblings' fields.  Composed
    schemas — and every schema they reference — must leave it unset."""
    from aoeo_market.web import openapi

    schemas = openapi.build_spec(include_ingestion=True)["components"]["schemas"]
    for name, schema in schemas.items():
        if "allOf" not in schema:
            continue
        assert "additionalProperties" not in schema, name
        for part in schema["allOf"]:
            target = schemas[part["$ref"].rsplit("/", 1)[-1]] if "$ref" in part else part
            assert "additionalProperties" not in target, f"{name} composes {part} which claims additionalProperties"


def test_openapi_specs_are_structurally_valid():
    """Both documents must pass an OpenAPI 3.0 schema validator.

    The home-grown guards above cover this repo's conventions (named $ref
    items, allOf/additionalProperties); this is the independent check that
    the document itself is well-formed OpenAPI — parameter schemas, path
    keys, response codes, nullable/enum tricks included.  ``validate`` raises
    ``OpenAPIValidationError`` on the first structural problem.
    """
    from openapi_spec_validator import validate

    from aoeo_market.web import openapi

    validate(openapi.build_spec())  # the public document
    validate(openapi.build_spec(include_ingestion=True))  # the internal contract too


def test_endpoint_payloads_validate_against_the_spec(tmp_path):
    """The payloads the server actually returns conform to the schemas the
    spec declares for them — the contract holds in the instance direction
    too, not just structurally (openapi-schema-validator)."""
    import json

    from openapi_schema_validator import OAS30Validator
    from openapi_schema_validator import validate as validate_instance

    from aoeo_market.web import openapi

    app = app_for(tmp_path)
    spec = openapi.build_spec(include_ingestion=True)
    schemas = spec["components"]["schemas"]

    def deref(node):
        """Expand every $ref against the component schemas.

        The components are acyclic (rows compose StockItem/Listing/
        ItemSummary), so a plain recursive expansion yields a self-contained
        schema for the validator.
        """
        if isinstance(node, dict):
            if len(node) == 1 and "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                return deref(schemas[name])
            return {k: deref(v) for k, v in node.items()}
        if isinstance(node, list):
            return [deref(v) for v in node]
        return node

    def check(path, spec_path, query=None, status=200):
        code, _, body = app.handle(path, query or {})
        assert code == status, path
        schema = spec["paths"][spec_path]["get"]["responses"][str(status)]["content"]["application/json"]["schema"]
        validate_instance(json.loads(body), deref(schema), OAS30Validator)

    # one payload per read endpoint, with the interesting variants
    check("/healthz", "/healthz")
    check("/readyz", "/readyz")
    check("/api/overview", "/api/overview")
    check("/api/listings", "/api/listings")
    check("/api/listings", "/api/listings", {"type": ["Trait"], "q": ["sword"], "sort": ["price"], "dir": ["desc"]})
    check("/api/item/Sword_U_III", "/api/item/{item_id}")
    check("/api/item/Axe_R_I", "/api/item/{item_id}")  # exercises previous[] with a vanished listing
    check("/api/not-on-sale", "/api/not-on-sale")
    check("/api/search", "/api/search", {"q": ["axe"]})
    check("/api/search", "/api/search", {"q": ["axe"], "limit": ["1"]})
    check("/api/search", "/api/search", {"q": [""]})  # nothing to match
    check("/api/best-sellers", "/api/best-sellers", {"min_sales": ["0"]})
    check("/api/best-value", "/api/best-value")
    check("/api/recently-removed", "/api/recently-removed")
    check("/api/item/nope", "/api/item/{item_id}", status=404)  # the Error schema

    # the ingestion request body and its ack
    payload = {"listings": [mk(1).to_dict(), mk(2).to_dict()], "captured_at": 1000.0}
    request_schema = spec["paths"]["/api/snapshot"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    validate_instance(payload, deref(request_schema), OAS30Validator)
    status, _, body = app.handle_post("/api/snapshot", json.dumps(payload).encode())
    assert status == 201
    ack_schema = spec["paths"]["/api/snapshot"]["post"]["responses"]["201"]["content"]["application/json"]["schema"]
    validate_instance(json.loads(body), deref(ack_schema), OAS30Validator)

    # the empty-database payloads (latest: null, empty arrays)
    empty = WebApp(str(tmp_path / "empty.db"))
    for path, spec_path in (("/api/overview", "/api/overview"), ("/api/listings", "/api/listings")):
        status, _, body = empty.handle(path)
        assert status == 200, path
        schema = spec["paths"][spec_path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        validate_instance(json.loads(body), deref(schema), OAS30Validator)


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


def test_listings_expose_absolute_utc_expiry(tmp_path):
    """The read API returns the stored expiry as an ISO-8601 UTC instant next
    to the wire countdown; the server computes it from captured_at."""
    import json

    _, _, body = app_for(tmp_path).handle("/api/listings")
    row = json.loads(body)[0]
    assert row["seconds_till_expiry"] == 90_000
    # the latest seed snapshot was captured at 2000.0 -> 2000 + 90000
    assert row["expires_at"] == "1970-01-02T01:33:20Z"
    assert row["expires_at"].endswith("Z")  # always UTC


def test_read_api_never_returns_the_real_seller_id(tmp_path):
    """Seller empire ids identify a player, so the read API overwrites the
    field with a sentinel on the way out even though the store keeps the real
    value for its joins."""
    import json

    from aoeo_market.web.server import REDACTED_SELLER_ID

    seller = 987654321
    db = tmp_path / "m.db"
    conn = store.open_store(db)
    store.record_snapshot(conn, [mk(1, item_id="Axe_R_I", item_type="Design", seller=seller)], captured_at=1000.0)
    store.record_snapshot(conn, [mk(2, item_id="Sword_U_III", item_type="Trait", seller=seller)], captured_at=2000.0)
    conn.close()
    app = WebApp(str(db))

    def sellers(payload) -> list:
        """Every seller_empire_id in a nested API document."""
        if isinstance(payload, dict):
            return [v for k, v in payload.items() if k == "seller_empire_id"] + [s for v in payload.values() for s in sellers(v)]
        if isinstance(payload, list):
            return [s for v in payload for s in sellers(v)]
        return []

    # One route per read shape that carries listings: a flat list, a nested
    # item history, and the recently-removed rows.
    for route in ("/api/listings", "/api/item/Axe_R_I", "/api/recently-removed"):
        status, _, body = app.handle(route)
        assert status == 200, route
        observed = sellers(json.loads(body))
        assert observed, f"{route} no longer carries the redacted field"
        assert set(observed) == {REDACTED_SELLER_ID}, route
        assert str(seller).encode() not in body, route

    # Only the API response is redacted: the store still carries the real id,
    # so its joins and snapshot bookkeeping keep working.
    conn = store.open_store(db)
    try:
        assert {row["seller_empire_id"] for row in store.active_listings(conn)} == {seller}
    finally:
        conn.close()


def test_item_endpoint_and_404(tmp_path):
    app = app_for(tmp_path)
    status, _, body = app.handle("/api/item/Sword_U_III")
    assert status == 200
    assert '"item_id": "Sword_U_III"' in body.decode()
    assert '"series"' in body.decode()
    status, _, body = app.handle("/api/item/unknown")
    assert status == 404
    assert "never observed" in body.decode()


def test_static_files_served_by_extension(tmp_path, monkeypatch):
    from aoeo_market.web import server as web_server

    app = app_for(tmp_path)
    # Point the static route at a scratch dir so the test does not depend on
    # the (gitignored) sprite-sheet binaries.
    monkeypatch.setattr(web_server, "STATIC_DIR", tmp_path)
    (tmp_path / "sprites").mkdir()
    (tmp_path / "sprites" / "materials.webp").write_bytes(b"WEBP")
    (tmp_path / "index.json").write_bytes(b'{"material": {}}')

    assert web_server._STATIC_TYPES[".webp"] == "image/webp"
    status, ctype, body = app.handle("/static/index.json")
    assert (status, ctype, body) == (200, "application/json; charset=utf-8", b'{"material": {}}')
    status, ctype, body = app.handle("/static/sprites/materials.webp")
    assert (status, ctype, body) == (200, "image/webp", b"WEBP")

    # Unknown extension and path traversal are rejected.
    assert app.handle("/static/nope.exe")[0] == 404
    assert app.handle("/static/../secret.py")[0] == 404
    assert app.handle("/static/sprites/missing.webp")[0] == 404


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


def test_best_sellers_floor_is_the_dashboards_request(tmp_path):
    """`min_sales=5` is the panel's request, not an API default.

    One fully observed sale is enough for a row to come back with no query
    params, and the dashboard is what asks for the five-sale floor.
    """
    import json

    db = tmp_path / "m.db"
    conn = store.open_store(db)
    # the warm-up snapshot keeps the thin item out of the left-censored first one
    store.record_snapshot(conn, [mk(100, item_id="Warm_U_I", price=10, expiry=200_000)], captured_at=0.0)
    store.record_snapshot(conn, [mk(1, item_id="Thin_U_I", price=10, expiry=200_000)], captured_at=2000.0)
    store.record_snapshot(conn, [], captured_at=3000.0)  # one fully observed sale
    conn.close()

    app = WebApp(str(db))
    _, _, body = app.handle("/api/best-sellers")
    assert [r["item_id"] for r in json.loads(body)] == ["Thin_U_I"]  # the API default is 1
    _, _, body = app.handle("/api/best-sellers", {"min_sales": ["5"]})
    assert json.loads(body) == []  # …and the dashboard's floor hides it
    _, _, js = app.handle("/static/app.js")
    assert b"/api/best-sellers?min_sales=5" in js  # the panel sends exactly that


def test_search_endpoint(tmp_path):
    import json

    # Search matches the curated catalog, so the seeded ids have to be real ones
    db = tmp_path / "s.db"
    conn = store.open_store(db)
    store.record_snapshot(conn, [mk(1, item_id="4ArcticFoxFur", item_type="Material", price=100)], captured_at=1000.0)
    conn.close()
    app = WebApp(str(db))

    _, _, body = app.handle("/api/search", {"q": ["arcticfox"]})
    fox = next(r for r in json.loads(body) if r["item_id"] == "4ArcticFoxFur")
    assert fox["rarity"]  # the curated identity rides along
    assert fox["listed_now"] is True
    assert fox["active_count"] == 1
    assert fox["current_median_unit_price"] == 100
    assert fox["median_unit_price"] == 100

    # an item the market has never seen is still findable, with no prices
    _, _, body = app.handle("/api/search", {"q": ["scepter2h_l001"]})
    rows = json.loads(body)
    assert rows[0]["item_id"] == "scepter2h_l001"
    assert rows[0]["listed_now"] is False
    assert rows[0]["current_median_unit_price"] is None
    assert rows[0]["median_unit_price"] is None

    _, _, body = app.handle("/api/search", {"q": ["arrow"], "limit": ["1"]})
    assert len(json.loads(body)) == 1

    status, _, _ = app.handle("/api/search", {"limit": ["abc"]})
    assert status == 400


def test_best_value_endpoint(tmp_path):
    import json

    db = tmp_path / "v.db"
    materials = [
        mk(1, item_id="4ArcticFoxFur", item_type="Material", price=100),
        mk(2, item_id="4IlluminatedCodex", item_type="Material", price=50),
        mk(3, item_id="4PhilosopherStone", item_type="Material", price=25),
    ]
    conn = store.open_store(db)
    # E004 sells below cost, and the latest snapshot has no listing for it
    store.record_snapshot(conn, [*materials, mk(4, item_id="FishingNet1H_E004", item_type="Trait", price=1000)], captured_at=1000.0)
    store.record_snapshot(
        conn,
        [
            *materials,
            mk(5, item_id="FireThrower2H_E006", item_type="Trait", price=5000),
            # same recipe, listed below what its ingredients cost: a buying deal
            mk(6, item_id="FireThrower2H_E101", item_type="Trait", price=1000),
        ],
        captured_at=2000.0,
    )
    conn.close()
    app = WebApp(str(db))

    status, _, body = app.handle("/api/best-value")
    assert status == 200
    rows = json.loads(body)
    assert [r["item_id"] for r in rows] == ["FireThrower2H_E006", "FireThrower2H_E101"]
    assert rows[0]["craft_cost"] == 2300  # 18*100 + 8*50 + 4*25
    assert rows[0]["value_ratio"] == round(5000 / 2300, 2)
    assert rows[0]["listed_now"] is True
    assert rows[1]["listed_now"] is True  # below cost, but worth buying
    assert rows[1]["value_ratio"] == round(1000 / 2300, 2)

    # ascending is the same metric read from the other end
    _, _, body = app.handle("/api/best-value", {"dir": ["asc"]})
    assert [r["item_id"] for r in json.loads(body)] == ["FireThrower2H_E101", "FireThrower2H_E006"]


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


def test_read_and_write_ports_are_separate(tmp_path):
    """The dashboard port is read-only; POST /api/snapshot answers on --write-port."""
    import json
    import threading

    from aoeo_market.web import server as web_server

    app = WebApp(str(tmp_path / "split.db"))
    read, write = web_server.create_servers(app, "127.0.0.1", 0, "127.0.0.1", 0)
    read_port = read.server_address[1]
    write_port = write.server_address[1]
    assert read_port != write_port
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (read, write)]
    for thread in threads:
        thread.start()
    try:
        # the dashboard port answers reads...
        assert http(f"http://127.0.0.1:{read_port}/healthz") == (200, b'{"status": "ok"}')
        # ...but not the write endpoint
        status, body = http(f"http://127.0.0.1:{read_port}/api/snapshot", data=b'{"listings": []}', method="POST")
        assert status == 404
        assert b"write port" in body

        # the write endpoint answers on the write port...
        payload = json.dumps({"listings": [mk(1, item_id="Sword_U_III", price=120).to_dict()]}).encode()
        status, body = http(f"http://127.0.0.1:{write_port}/api/snapshot", data=payload, method="POST")
        assert status == 201
        assert json.loads(body) == {"snapshot_id": 1, "listings": 1}
        # ...which serves no reads at all
        status, body = http(f"http://127.0.0.1:{write_port}/api/overview")
        assert status == 404
        assert b"only POST" in body

        # both listeners share the one database the process owns
        status, body = http(f"http://127.0.0.1:{read_port}/api/overview")
        assert status == 200
        assert json.loads(body)["snapshot_count"] == 1
    finally:
        for server in (read, write):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


def test_cli_refuses_the_same_read_and_write_port(tmp_path):
    import pytest

    from aoeo_market.web import server as web_server

    argv = ["--db", str(tmp_path / "m.db"), "--host", "127.0.0.1", "--port", "8000", "--write-port", "8000"]
    with pytest.raises(SystemExit) as excinfo:
        web_server.main(argv)
    assert excinfo.value.code == 2


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


# --- pagination, connection reuse, and HTTP response plumbing ---------------


def test_listings_limit_and_offset(tmp_path):
    import json

    app = app_for(tmp_path)
    _, _, body = app.handle("/api/listings", {"sort": ["item"], "dir": ["asc"]})
    all_rows = json.loads(body)

    status, _, body = app.handle("/api/listings", {"sort": ["item"], "dir": ["asc"], "limit": ["1"]})
    assert status == 200
    assert json.loads(body) == all_rows[:1]

    _, _, body = app.handle("/api/listings", {"sort": ["item"], "dir": ["asc"], "limit": ["1"], "offset": ["1"]})
    assert json.loads(body) == all_rows[1:2]

    # an offset past the end is an empty page, not an error
    _, _, body = app.handle("/api/listings", {"offset": ["999"]})
    assert json.loads(body) == []

    # malformed paging is rejected
    for bad in ({"limit": ["0"]}, {"limit": ["-2"]}, {"limit": ["x"]}, {"offset": ["-1"]}, {"offset": ["x"]}):
        status, _, _ = app.handle("/api/listings", bad)
        assert status == 400, bad


def test_listings_pagination_is_documented(tmp_path):
    import json

    app = WebApp(str(tmp_path / "empty.db"))
    spec = json.loads(app.handle("/openapi.json")[2])
    params = spec["paths"]["/api/listings"]["get"]["parameters"]
    assert [p["name"] for p in params] == ["type", "q", "sort", "dir", "limit", "offset"]
    assert params[4]["schema"] == {"type": "integer"}


def test_connection_is_reused_across_requests(tmp_path, monkeypatch):
    """The server opens the database once, not once per request."""
    from aoeo_market.web import server as web_server

    db = str(tmp_path / "m.db")
    seed(db)

    opens: list[str] = []
    real = web_server.store.open_store

    def counting_open(path, **kwargs):
        opens.append(str(path))
        return real(path, **kwargs)

    monkeypatch.setattr(web_server.store, "open_store", counting_open)

    app = WebApp(db)
    for _ in range(5):
        assert app.handle("/api/overview")[0] == 200
    assert opens == [db]  # one connection served all five requests
    app.close()


def _request(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict, bytes]:
    """One HTTP request returning ``(status, headers, body)`` (304 included)."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_data_validator_names_the_latest_snapshot(tmp_path):
    empty = WebApp(str(tmp_path / "empty.db"))
    assert empty.data_validator() == '"snap:none"'
    empty.close()

    app = app_for(tmp_path)  # seed() records two snapshots
    assert app.data_validator() == '"snap:2"'
    app.close()


def test_etag_revalidation_skips_the_view(tmp_path, monkeypatch):
    """A 304 is decided from the snapshot id, before the view is computed."""
    import threading

    from aoeo_market.web import server as web_server

    app = app_for(tmp_path)
    validator = app.data_validator()

    def explode(*args, **kwargs):
        raise AssertionError("a revalidation must not compute the view")

    monkeypatch.setattr(web_server.store, "active_listings", explode)
    read, write = web_server.create_servers(app, "127.0.0.1", 0, "127.0.0.1", 0)
    thread = threading.Thread(target=read.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{read.server_address[1]}/api/listings"
        status, headers, body = _request(url, {"If-None-Match": validator})
        assert status == 304
        assert body == b""
        assert headers["ETag"] == validator
    finally:
        read.shutdown()
        read.server_close()
        write.server_close()
        thread.join()
        app.close()


def test_http_gzip_and_etag_revalidation(tmp_path):
    """A big payload is compressed on request and revalidated with a 304."""
    import gzip
    import threading

    from aoeo_market.web import server as web_server

    # enough listings that the payload clears the compression threshold
    db = tmp_path / "many.db"
    conn = store.open_store(db)
    store.record_snapshot(conn, [mk(i, item_id=f"Item{i:03d}_U_I", price=100 + i) for i in range(1, 41)], captured_at=1000.0)
    conn.close()

    app = WebApp(str(db))
    read, write = web_server.create_servers(app, "127.0.0.1", 0, "127.0.0.1", 0)
    thread = threading.Thread(target=read.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{read.server_address[1]}/api/listings"
    try:
        # the identity body, for comparison
        status, headers, plain = _request(url)
        assert status == 200
        assert headers.get("Content-Encoding", "") != "gzip"
        assert headers["ETag"].startswith('"') and headers["Cache-Control"] == "no-cache"

        # the same resource, gzipped when the client offers it
        status, headers, body = _request(url, {"Accept-Encoding": "gzip"})
        assert status == 200
        assert headers["Content-Encoding"] == "gzip"
        assert headers["Vary"] == "Accept-Encoding"
        assert gzip.decompress(body) == plain

        # revalidating with the ETag skips the body entirely
        status, headers, body = _request(url, {"If-None-Match": headers["ETag"]})
        assert status == 304
        assert body == b""
    finally:
        read.shutdown()
        read.server_close()
        write.server_close()
        thread.join()
        app.close()


# --- the read window (`--since` / `--start-date`) ---------------------------


def windowed_app(tmp_path, since):
    """Three hourly snapshots for the start-date tests.

    t=1000: Axe (tx1) + Arctic Fox (tx2)   <- hidden by a 1500 cutoff
    t=2000: Arctic Fox (tx2)
    t=3000: Sword (tx3)
    """
    db = tmp_path / "window.db"
    conn = store.open_store(db)
    store.record_snapshot(
        conn,
        [
            mk(1, item_id="Axe_R_I", item_type="Design", price=100, expiry=200_000),
            mk(2, item_id="4ArcticFoxFur", item_type="Material", price=1000, expiry=200_000),
        ],
        captured_at=1000.0,
    )
    store.record_snapshot(conn, [mk(2, item_id="4ArcticFoxFur", item_type="Material", price=1000, expiry=200_000)], captured_at=2000.0)
    store.record_snapshot(conn, [mk(3, item_id="Sword_U_III", item_type="Trait", price=500, expiry=200_000)], captured_at=3000.0)
    conn.close()
    return WebApp(str(db), since=since)


def test_start_cutoff_restricts_every_read_endpoint(tmp_path):
    """`--since` hides a snapshot from the whole read API, not just one view."""
    import json

    app = windowed_app(tmp_path, since=1500.0)

    assert app.data_validator() == '"snap:3"'
    assert json.loads(app.handle("/readyz")[2])["snapshots"] == 2

    overview = json.loads(app.handle("/api/overview")[2])
    assert overview["snapshot_count"] == 2
    assert overview["active_listings"] == 1
    assert [s["t"] for s in overview["supply_history"]] == [2000.0, 3000.0]

    assert [r["item_id"] for r in json.loads(app.handle("/api/listings")[2])] == ["Sword_U_III"]
    assert app.handle("/api/item/Axe_R_I")[0] == 404  # only seen before the cutoff
    history = json.loads(app.handle("/api/item/4ArcticFoxFur")[2])
    assert [s["t"] for s in history["series"]] == [2000.0]
    assert [p["transaction_id"] for p in history["previous"]] == [2]
    assert [r["item_id"] for r in json.loads(app.handle("/api/not-on-sale")[2])] == ["4ArcticFoxFur"]
    assert [r["item_id"] for r in json.loads(app.handle("/api/recently-removed")[2])] == ["4ArcticFoxFur"]
    assert "Axe_R_I" not in app.handle("/api/best-sellers", {"min_sales": ["0"]})[2].decode()
    assert json.loads(app.handle("/api/search", {"q": ["arcticfox"]})[2])[0]["median_unit_price"] == 1000

    # The window is a start-up argument, never a request parameter: an API
    # caller cannot widen it (or narrow it) from the query string.
    assert app.handle("/api/listings", {"since": ["0"]})[2] == app.handle("/api/listings")[2]
    assert app.handle("/api/item/Axe_R_I", {"since": ["0"]})[0] == 404
    assert json.loads(app.handle("/api/overview", {"since": ["0"]})[2])["snapshot_count"] == 2


def test_start_cutoff_leaves_ingestion_alone(tmp_path):
    """The cutoff is read-side only: snapshots keep being appended to the file."""
    import json

    app = windowed_app(tmp_path, since=2500.0)  # only t=3000 is inside the window
    assert json.loads(app.handle("/api/overview")[2])["snapshot_count"] == 1

    payload = json.dumps({"listings": [mk(9, item_id="Sword_U_III", price=900, expiry=200_000).to_dict()], "captured_at": 4000.0}).encode()
    assert app.handle_post("/api/snapshot", payload)[0] == 201

    # The new snapshot is inside the window, so every read follows it.
    assert app.data_validator() == '"snap:4"'
    assert [r["item_price"] for r in json.loads(app.handle("/api/listings")[2])] == [900]
    assert json.loads(app.handle("/readyz")[2])["snapshots"] == 2


def test_parse_start_date():
    from datetime import UTC, datetime

    from aoeo_market.web.server import parse_start_date

    midnight = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    assert parse_start_date("2026-01-01") == midnight  # a bare date is midnight UTC
    assert parse_start_date(" 2026-01-01 ") == midnight  # surrounding space is ignored
    assert parse_start_date("2026-01-01T06:30:00Z") == midnight + 6.5 * 3600
    # a value without an offset is read as UTC, like every stored timestamp
    assert parse_start_date("2026-01-01T06:30:00") == parse_start_date("2026-01-01T06:30:00Z")

    import pytest

    for bad in ("", "yesterday", "2026-13-01", "01/02/2026", "2026"):
        with pytest.raises(ValueError):
            parse_start_date(bad)


def test_start_date_argument_reaches_the_app(tmp_path, monkeypatch, capsys):
    """`--since` is parsed by the CLI and handed to the one WebApp it serves."""
    from aoeo_market.web import server as web_server

    served = {}

    class FakeServer:
        def __init__(self, *, blocking):
            self._blocking = blocking

        def serve_forever(self):
            if self._blocking:  # the read listener: hand control back to main()
                raise KeyboardInterrupt

        def shutdown(self):
            pass

        def server_close(self):
            pass

    def fake_create_servers(app, host, port, write_host, write_port):
        served["app"] = app
        return FakeServer(blocking=True), FakeServer(blocking=False)

    monkeypatch.setattr(web_server, "create_servers", fake_create_servers)
    db = tmp_path / "m.db"
    seed(db)

    assert web_server.main(["--db", str(db), "--since", "2026-01-01"]) == 0
    assert served["app"].since == web_server.parse_start_date("2026-01-01")
    assert "snapshots since 2026-01-01" in capsys.readouterr().out

    # the long spelling is an alias for the same argument
    assert web_server.main(["--db", str(db), "--start-date", "2026-01-01T06:30:00Z"]) == 0
    assert served["app"].since == web_server.parse_start_date("2026-01-01T06:30:00Z")

    # a value that is not a date is a usage error, before any database is opened
    import pytest

    with pytest.raises(SystemExit) as excinfo:
        web_server.main(["--db", str(db), "--since", "the day before yesterday"])
    assert excinfo.value.code == 2


def test_start_cutoff_can_hide_a_crafting_cost(tmp_path):
    """Best value prices ingredients inside the window: one the cutoff hides
    counts as never observed, so the recipe can no longer be costed."""
    import json

    db = tmp_path / "value-window.db"
    conn = store.open_store(db)
    materials = [
        mk(1, item_id="4ArcticFoxFur", item_type="Material", price=100),
        mk(2, item_id="4IlluminatedCodex", item_type="Material", price=50),
        mk(3, item_id="4PhilosopherStone", item_type="Material", price=25),
    ]
    store.record_snapshot(conn, [*materials, mk(4, item_id="FireThrower2H_E006", item_type="Trait", price=5000)], captured_at=1000.0)
    # the ingredients are only ever listed in the snapshot before the cutoff
    store.record_snapshot(conn, [mk(5, item_id="FireThrower2H_E006", item_type="Trait", price=5000)], captured_at=2000.0)
    conn.close()

    rows = json.loads(WebApp(str(db)).handle("/api/best-value")[2])
    assert [r["item_id"] for r in rows] == ["FireThrower2H_E006"]
    assert rows[0]["craft_cost"] == 2300  # 18*100 + 8*50 + 4*25
    assert json.loads(WebApp(str(db), since=2500.0).handle("/api/best-value")[2]) == []  # nothing to cost it with
