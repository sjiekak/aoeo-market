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
    check("/api/best-sellers", "/api/best-sellers", {"min_sales": ["0"]})
    check("/api/best-value", "/api/best-value")
    check("/api/best-value", "/api/best-value", {"include_unrated": ["1"]})
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
