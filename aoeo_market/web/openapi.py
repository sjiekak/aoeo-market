"""OpenAPI 3.0 specification for the market website's JSON API.

Built programmatically from the routing metadata in
:mod:`aoeo_market.web.server` (the sort/order whitelists live in
:mod:`aoeo_market.store`) and the wire contract of
:class:`aoeo_market.market.Listing`, so the spec cannot drift from the
implementation.  Served by the web server at ``GET /openapi.json``.

The **public** document describes the read API and the probes only — how
market data is ingested is an internal detail and is deliberately omitted.
:func:`build_spec` can include the ingestion endpoint
(``include_ingestion=True``) for operator documentation and tests.
"""

from __future__ import annotations

import json

from .. import store

VERSION = "0.1.0"


def _query_param(name: str, description: str, *, enum: list[str] | None = None, default: str | None = None, schema_type: str = "string") -> dict:
    param: dict = {"name": name, "in": "query", "required": False, "description": description, "schema": {"type": schema_type}}
    if enum:
        param["schema"]["enum"] = enum
    if default is not None:
        param["schema"]["default"] = default
    return param


def _json_response(description: str, schema: dict | None = None) -> dict:
    content: dict = {}
    if schema is not None:
        content["application/json"] = {"schema": schema}
    return {"description": description, "content": content}


def _listing_schema() -> dict:
    props = {
        "transaction_id": {"type": "integer", "format": "int64", "description": "Unique listing id; stable across snapshots."},
        "seller_empire_id": {"type": "integer", "format": "int64"},
        "buyer_character_id": {"type": "integer", "format": "int64", "description": "Sentinel -1 while the listing is active."},
        "item_id": {
            "type": "string",
            "description": "Marketplace item id; resolved against the curated item catalog for the display name and authoritative rarity.",
        },
        "item_type": {"type": "string", "description": "Advisor, Design, Material, Trait, ..."},
        "item_level": {"type": "integer"},
        "item_count": {"type": "integer", "minimum": 1, "description": "Stack size; item_price is the total for the whole stack."},
        "item_price": {"type": "integer", "description": "Total price for the stack; the per-unit price is item_price / item_count."},
        "item_seed": {"type": "integer"},
        "seconds_till_expiry": {"type": "integer", "description": "Listing countdown in seconds (drives the EXPIRED vs REMOVED classification)."},
    }
    return {"type": "object", "required": list(props), "properties": props, "additionalProperties": False}


def _ingestion_path(listing: dict, error: dict) -> dict:
    """The snapshot write endpoint — internal contract, not served publicly.

    The public ``/openapi.json`` deliberately omits it: the website does not
    advertise how market data is ingested.  It stays in the generated spec
    for operator documentation and for the sync test.
    """
    return {
        "/api/snapshot": {
            "post": {
                "summary": "Append one market snapshot",
                "description": "The only write endpoint: the fetcher posts here, so the web server is the single owner of the database. Unauthenticated — keep the service cluster-internal.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["listings"],
                                "properties": {
                                    "listings": {"type": "array", "items": listing, "description": "All active listings of the snapshot."},
                                    "captured_at": {"type": "number", "description": "Unix seconds (UTC) the snapshot was taken; defaults to now."},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "201": _json_response(
                        "snapshot stored",
                        {
                            "type": "object",
                            "required": ["snapshot_id", "listings"],
                            "properties": {"snapshot_id": {"type": "integer"}, "listings": {"type": "integer"}},
                        },
                    ),
                    "400": _json_response("malformed payload or invalid listing fields", error),
                    "500": _json_response("database error", error),
                },
            }
        }
    }


def build_spec(*, include_ingestion: bool = False) -> dict:
    """Return the complete OpenAPI 3.0 document as a dict.

    The public document (``include_ingestion=False``) describes the read API
    and the probes only — how snapshots are ingested is an internal detail.
    """
    listing = {"$ref": "#/components/schemas/Listing"}
    error = {"$ref": "#/components/schemas/Error"}
    loose = {"type": "object", "additionalProperties": True}

    paths: dict[str, dict] = {
        "/healthz": {
            "get": {
                "summary": "Liveness probe",
                "description": "Always 200 while the process is up; never touches the database.",
                "responses": {"200": _json_response("process alive", {"type": "object"})},
            }
        },
        "/readyz": {
            "get": {
                "summary": "Readiness probe",
                "description": "200 once the database file is initialized and openable; 503 before init-db has run or on open errors.",
                "responses": {
                    "200": _json_response("database ready", loose),
                    "503": _json_response("database not initialized or not openable", error),
                },
            }
        },
        "/api/overview": {
            "get": {
                "summary": "Aggregate stats for the overview tab",
                "description": "Current market stats: active listings, distinct items, supply history, type and rarity breakdowns, the current per-unit price histogram, and the biggest median-price movers between the two most recent data points.",
                "responses": {"200": _json_response("overview aggregates", loose)},
            }
        },
        "/api/listings": {
            "get": {
                "summary": "Current active listings",
                "parameters": [
                    _query_param("type", "Filter by item type."),
                    _query_param("q", "Case-insensitive substring filter on item id."),
                    _query_param("sort", "Sort column.", enum=list(store._SORT_COLUMNS), default="price"),
                    _query_param("dir", "Sort direction.", enum=["asc", "desc"], default="asc"),
                ],
                "responses": {
                    "200": _json_response(
                        "listings, each enriched with display name, kind, icon, authoritative rarity, and per-unit price", {"type": "array", "items": listing}
                    )
                },
            }
        },
        "/api/item/{item_id}": {
            "get": {
                "summary": "Current and previous listings plus the full price history of one item",
                "description": "Per-unit price series (median per observation plus downsampled raw points), the item's current listings, and its previous (vanished) listings with the EXPIRED vs REMOVED classification.",
                "parameters": [
                    {
                        "name": "item_id",
                        "in": "path",
                        "required": True,
                        "description": "Item id, URL-encoded.",
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": _json_response("item history", loose),
                    "404": _json_response("item was never observed", error),
                },
            }
        },
        "/api/not-on-sale": {
            "get": {
                "summary": "Items seen historically with no active listing right now",
                "description": "Historical per-unit price stats for items that are currently absent from the market.",
                "parameters": [
                    _query_param("order", "Sort column.", enum=list(store._NOT_SALE_SORTS), default="median_unit_price"),
                    _query_param("dir", "Sort direction.", enum=["asc", "desc"], default="desc"),
                ],
                "responses": {"200": _json_response("not-on-sale rows", {"type": "array", "items": loose})},
            }
        },
        "/api/best-sellers": {
            "get": {
                "summary": "Items ranked by observed time-to-sale (fastest first)",
                "description": "Listing lifetime measured between consecutive observations; only fully observed listings that vanished with >= 1 day left on their countdown count as sales.",
                "parameters": [
                    _query_param("order", "Sort column.", enum=list(store._BEST_SELLER_SORTS), default="median_time"),
                    _query_param("dir", "Sort direction.", enum=["asc", "desc"], default="asc"),
                    _query_param("min_sales", "Only items with at least this many fully observed sales.", default="1"),
                ],
                "responses": {"200": _json_response("best-seller rows", {"type": "array", "items": loose})},
            }
        },
        "/api/best-value": {
            "get": {
                "summary": "Items ranked by value for their rarity (cheapest relative to their tier first)",
                "description": "Value ratio = rarity-tier reference price / effective per-unit price; cheaper_than_pct is the item's price percentile within its tier.",
                "parameters": [
                    _query_param("order", "Sort column.", enum=list(store._BEST_VALUE_SORTS), default="value_ratio"),
                    _query_param("dir", "Sort direction.", enum=["asc", "desc"], default="desc"),
                    _query_param("include_unrated", "Include items without a rarity tag as their own tier.", enum=["0", "1"], default="0"),
                ],
                "responses": {"200": _json_response("best-value rows", {"type": "array", "items": loose})},
            }
        },
        "/api/recently-removed": {
            "get": {
                "summary": "Listings that vanished between the two most recent data points, or within a chosen time window",
                "description": "Classified EXPIRED (< 1 day left on the countdown) or REMOVED (sold or withdrawn — indistinguishable). By default this is the delta between the two most recent snapshots; pass `window` (seconds) to see every listing that vanished within that time window.",
                "parameters": [
                    _query_param(
                        "window",
                        "Time window in seconds back from the latest snapshot (default: the delta between the two most recent snapshots).",
                        schema_type="number",
                    ),
                ],
                "responses": {"200": _json_response("removed listings", {"type": "array", "items": loose})},
            }
        },
    }

    if include_ingestion:
        paths.update(_ingestion_path(listing, error))

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "AoEO Market API",
            "description": (
                "Read-only trading-intelligence API over the recorded history of the Project Celeste marketplace."
                if not include_ingestion
                else "Read-only trading-intelligence API over the recorded history of the Project Celeste marketplace, plus the snapshot ingestion endpoint (internal contract)."
            ),
            "version": VERSION,
        },
        "servers": [{"url": "/"}],
        "paths": paths,
        "components": {
            "schemas": {
                "Listing": _listing_schema(),
                "Error": {"type": "object", "required": ["error"], "properties": {"error": {"type": "string"}}},
            }
        },
    }


def spec_json(*, include_ingestion: bool = False) -> bytes:
    """The serialized OpenAPI document served at ``GET /openapi.json``.

    By default the public document: read endpoints and probes only, so the
    website does not leak how market data is ingested.
    """
    return json.dumps(build_spec(include_ingestion=include_ingestion), indent=2).encode()
