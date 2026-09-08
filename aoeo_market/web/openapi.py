"""OpenAPI 3.0 specification for the market website's JSON API.

Built programmatically from the routing metadata in
:mod:`aoeo_market.web.server` (the sort/order whitelists live in
:mod:`aoeo_market.store`) and the wire contract of
:class:`aoeo_market.market.Listing`, so the spec cannot drift from the
implementation.  Served by the web server at ``GET /openapi.json``.

Every input and output is typed: array ``items`` are always a ``$ref`` to a
named component schema, never an inline ``{"type": "object"}``.  The component
schemas compose the two shared models with per-view metrics:

* ``StockItem``    the item part of a listing: id, type, level, stack count,
  total price, seed.
* ``Listing``      a stock item listed by a seller: ``StockItem`` + transaction
  id, seller/buyer ids, expiry countdown.  Also the snapshot-ingestion row.
* ``ItemSummary``  the curated identity attached to every row that names an
  item: display name, rarity/rank, and the optional catalog fields.
* ``ListingRow``   one enriched listing of the latest snapshot: ``Listing`` +
  ``ItemSummary`` + unit price and snapshot id.

The **public** document describes the read API and the probes only — how
market data is ingested is an internal detail and is deliberately omitted.
:func:`build_spec` can include the ingestion endpoint
(``include_ingestion=True``) for operator documentation and tests.
"""

from __future__ import annotations

import json

from .. import store
from ..observer import RemovalReason

VERSION = "0.1.0"


def _ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def _object(properties: dict, required: list[str], *, strict: bool = True) -> dict:
    """One object schema.

    ``strict`` forbids undeclared properties.  It must be ``False`` for the
    parts of an ``allOf`` composition: in OpenAPI 3.0 ``additionalProperties``
    does not see properties declared by sibling schemas, so a strict part
    would reject the other parts' fields.
    """
    schema: dict = {"type": "object", "required": required, "properties": properties}
    if strict:
        schema["additionalProperties"] = False
    return schema


def _composed(*parts: dict) -> dict:
    """Compose one schema from ``allOf`` of $refs and/or inline parts."""
    return {"allOf": list(parts)}


def _query_param(name: str, description: str, *, enum: list[str] | None = None, default: int | str | None = None, schema_type: str = "string") -> dict:
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


def _array_of(name: str) -> dict:
    return {"type": "array", "items": _ref(name)}


# --- component schemas ------------------------------------------------------


def _stock_item_schema() -> dict:
    """The sold item: the wire item fields every listing row carries.

    Non-strict on purpose: it is composed into ``Listing`` via ``allOf``, and
    OpenAPI 3.0's ``additionalProperties`` cannot see a sibling schema's
    properties, so a strict part would reject the listing fields.
    """
    return _object(
        {
            "item_id": {
                "type": "string",
                "description": "Marketplace item id; resolved against the curated item catalog for the display name and authoritative rarity.",
            },
            "item_type": {"type": "string", "description": "Advisor, Design, Material, Trait, ..."},
            "item_level": {"type": "integer"},
            "item_count": {"type": "integer", "minimum": 1, "description": "Stack size; item_price is the total for the whole stack."},
            "item_price": {"type": "integer", "description": "Total price for the stack; the per-unit price is item_price / item_count."},
            "item_seed": {"type": "integer"},
        },
        ["item_id", "item_type", "item_level", "item_count", "item_price", "item_seed"],
        strict=False,
    )


def _listing_schema() -> dict:
    """A stock item listed by a seller — the wire record and ingestion row."""
    return _composed(
        _ref("StockItem"),
        _object(
            {
                "transaction_id": {"type": "integer", "format": "int64", "description": "Unique listing id; stable across snapshots."},
                "seller_empire_id": {"type": "integer", "format": "int64"},
                "buyer_character_id": {"type": "integer", "format": "int64", "description": "Sentinel -1 while the listing is active."},
                "seconds_till_expiry": {
                    "type": "integer",
                    "description": "Listing countdown in seconds (drives the EXPIRED vs REMOVED classification).",
                },
            },
            ["transaction_id", "seller_empire_id", "buyer_character_id", "seconds_till_expiry"],
            strict=False,
        ),
    )


def _item_summary_schema() -> dict:
    """The curated identity attached to every row that names an item.

    ``item_id``, ``name`` (null when the catalog has never seen the id),
    ``rarity`` (null when unknown) and ``rarity_rank`` (0 when unknown) are
    always present; the remaining catalog fields appear only when the curated
    database records them.
    """
    return _object(
        {
            "item_id": {"type": "string", "description": "Marketplace item id."},
            "name": {"type": "string", "nullable": True, "description": "Curated display name; null when the catalog has never seen the id."},
            "rarity": {
                "type": "string",
                "nullable": True,
                "description": "Authoritative rarity name (catalog first, id-suffix heuristic as fallback); null when unknown.",
            },
            "rarity_rank": {"type": "integer", "description": "Numeric rarity rank, higher = rarer; 0 when unknown."},
            "kind": {"type": "string", "description": "Entity kind (advisor / blueprint / consumable / design / item / material)."},
            "icon": {"type": "string", "description": "Sprite icon id."},
            "description": {"type": "string"},
            "civilization": {"type": "string"},
            "age": {"type": "string"},
            "event": {"type": "string", "description": "Seasonal event the item belongs to."},
        },
        ["item_id", "name", "rarity", "rarity_rank"],
        strict=False,
    )


def _listing_row_schema() -> dict:
    """One enriched listing row of ``GET /api/listings`` (and item details)."""
    return _composed(
        _ref("Listing"),
        _ref("ItemSummary"),
        _object(
            {
                "snapshot_id": {"type": "integer", "format": "int64", "description": "Snapshot the row was observed in."},
                "unit_price": {"type": "number", "description": "item_price / item_count, rounded to cents — the price per unit."},
            },
            ["snapshot_id", "unit_price"],
            strict=False,
        ),
    )


def _price_mover_schema() -> dict:
    """One median-price mover between the two most recent snapshots."""
    return _composed(
        _ref("ItemSummary"),
        _object(
            {
                "median_before": {"type": "integer", "description": "Median unit price in the previous snapshot."},
                "median_now": {"type": "integer", "description": "Median unit price in the latest snapshot."},
                "change_pct": {"type": "number", "description": "Percent change between the two medians."},
            },
            ["median_before", "median_now", "change_pct"],
            strict=False,
        ),
    )


def _not_on_sale_row_schema() -> dict:
    """One row of ``GET /api/not-on-sale``: a historical item with no active listing."""
    return _composed(
        _ref("ItemSummary"),
        _object(
            {
                "item_type": {"type": "string"},
                "item_level": {"type": "integer"},
                "median_unit_price": {"type": "number"},
                "min_unit_price": {"type": "number"},
                "max_unit_price": {"type": "number"},
                "times_listed": {"type": "integer"},
                "last_seen": {"type": "number", "nullable": True, "description": "Unix seconds of the most recent observation."},
            },
            ["item_type", "item_level", "median_unit_price", "min_unit_price", "max_unit_price", "times_listed", "last_seen"],
            strict=False,
        ),
    )


def _best_seller_row_schema() -> dict:
    """One row of ``GET /api/best-sellers``: an item ranked by time-to-sale."""
    return _composed(
        _ref("ItemSummary"),
        _object(
            {
                "item_type": {"type": "string"},
                "item_level": {"type": "integer"},
                "sales": {"type": "integer"},
                "timed_sales": {"type": "integer", "description": "Fully observed sales counted toward the time stats."},
                "expired": {"type": "integer"},
                "median_time": {"type": "number", "nullable": True, "description": "Seconds."},
                "min_time": {"type": "number", "nullable": True, "description": "Seconds."},
                "max_time": {"type": "number", "nullable": True, "description": "Seconds."},
                "active_count": {"type": "integer"},
                "current_median_unit_price": {"type": "number", "nullable": True},
                "last_seen": {"type": "number"},
            },
            [
                "item_type",
                "item_level",
                "sales",
                "timed_sales",
                "expired",
                "median_time",
                "min_time",
                "max_time",
                "active_count",
                "current_median_unit_price",
                "last_seen",
            ],
            strict=False,
        ),
    )


def _best_value_row_schema() -> dict:
    """One row of ``GET /api/best-value``: an item ranked by value for its rarity."""
    return _composed(
        _ref("ItemSummary"),
        _object(
            {
                "item_type": {"type": "string"},
                "item_level": {"type": "integer"},
                "tier_reference_price": {"type": "integer", "description": "Median historical price of the item's rarity tier."},
                "median_unit_price": {"type": "integer"},
                "min_unit_price": {"type": "number"},
                "max_unit_price": {"type": "number"},
                "current_median_unit_price": {"type": "integer", "nullable": True},
                "current_min_unit_price": {"type": "number"},
                "active_count": {"type": "integer"},
                "times_listed": {"type": "integer"},
                "value_ratio": {"type": "number", "nullable": True, "description": "tier_reference_price / effective unit price."},
                "cheaper_than_pct": {"type": "number", "description": "0..100 percentile within the rarity tier."},
            },
            [
                "item_type",
                "item_level",
                "tier_reference_price",
                "median_unit_price",
                "min_unit_price",
                "max_unit_price",
                "current_median_unit_price",
                "current_min_unit_price",
                "active_count",
                "times_listed",
                "value_ratio",
                "cheaper_than_pct",
            ],
            strict=False,
        ),
    )


def _removal_reason_schema() -> dict:
    """Why a listing vanished, generated from the observer's classification.

    ``EXPIRED``: vanished with less than a day left on its countdown (timed
    out unsold).  ``REMOVED``: vanished with time to spare (sold or withdrawn
    — indistinguishable from the outside).
    """
    return {"type": "string", "enum": [r.value for r in RemovalReason]}


def _removed_listing_schema() -> dict:
    """One vanished listing row of ``GET /api/recently-removed``.

    A full ``Listing`` enriched with its curated ``ItemSummary`` plus the
    observation span; the payload carries every listing field even where the
    view only displays a few.
    """
    return _composed(
        _ref("Listing"),
        _ref("ItemSummary"),
        _object(
            {
                "unit_price": {"type": "number", "description": "item_price / item_count, rounded to cents."},
                "reason": _ref("RemovalReason"),
                "vanished_at": {"type": "number", "description": "Unix seconds of the first snapshot where the listing is absent."},
            },
            ["unit_price", "reason", "vanished_at"],
            strict=False,
        ),
    )


def _previous_listing_schema() -> dict:
    """One vanished listing of ``GET /api/item/{item_id}``.

    A full ``Listing`` plus the observation span; the item identity is
    redundant with the enclosing item detail but keeps the row on the shared
    listing model.
    """
    return _composed(
        _ref("Listing"),
        _object(
            {
                "unit_price": {"type": "number", "description": "item_price / item_count, rounded to cents."},
                "first_seen": {"type": "number", "description": "Unix seconds of the first snapshot the listing appears in."},
                "last_seen": {"type": "number", "description": "Unix seconds of the last snapshot the listing appears in."},
                "vanished_at": {"type": "number", "description": "Unix seconds of the first snapshot where the listing is absent."},
                "reason": _ref("RemovalReason"),
            },
            ["unit_price", "first_seen", "last_seen", "vanished_at", "reason"],
            strict=False,
        ),
    )


def _series_point_schema() -> dict:
    """One per-snapshot aggregate of the item's price series."""
    return _object(
        {
            "t": {"type": "number", "description": "Unix seconds of the snapshot."},
            "count": {"type": "integer", "description": "Active listings of the item in that snapshot."},
            "min": {"type": "number"},
            "max": {"type": "number"},
            "median": {"type": "number"},
        },
        ["t", "count", "min", "max", "median"],
    )


def _scatter_point_schema() -> dict:
    """One raw unit-price observation of the item's price series."""
    return _object(
        {
            "t": {"type": "number", "description": "Unix seconds of the snapshot."},
            "price": {"type": "number", "description": "Unit price."},
        },
        ["t", "price"],
    )


def _item_detail_schema() -> dict:
    """The payload of ``GET /api/item/{item_id}``: identity + full history."""
    return _composed(
        _ref("ItemSummary"),
        _object(
            {
                "item_type": {"type": "string", "description": "Item type as last observed."},
                "item_level": {"type": "integer", "description": "Item level as last observed."},
                "current": {"type": "array", "items": _ref("ListingRow"), "description": "The item's active listings in the latest snapshot."},
                "previous": {
                    "type": "array",
                    "items": _ref("PreviousListing"),
                    "description": "Vanished listings, newest first, with the EXPIRED vs REMOVED classification.",
                },
                "series": {"type": "array", "items": _ref("SeriesPoint"), "description": "Per-snapshot aggregates of the price series."},
                "points": {"type": "array", "items": _ref("ScatterPoint"), "description": "Downsampled raw unit-price observations."},
            },
            ["item_type", "item_level", "current", "previous", "series", "points"],
            strict=False,
        ),
    )


def _snapshot_info_schema() -> dict:
    return _object(
        {
            "id": {"type": "integer", "format": "int64"},
            "captured_at": {"type": "number", "description": "Unix seconds (UTC) the snapshot was taken."},
        },
        ["id", "captured_at"],
    )


def _supply_point_schema() -> dict:
    """One point of the market supply history."""
    return _object(
        {
            "t": {"type": "number", "description": "Unix seconds of the snapshot."},
            "count": {"type": "integer", "description": "Active listings in that snapshot."},
        },
        ["t", "count"],
    )


def _name_count_schema() -> dict:
    """One bucket of a named breakdown (by type or by rarity)."""
    return _object(
        {
            "name": {"type": "string"},
            "count": {"type": "integer"},
        },
        ["name", "count"],
    )


def _histogram_bin_schema() -> dict:
    """One bin of the price distribution histogram."""
    return _object(
        {
            "label": {"type": "string", "description": "Human-readable bin range, e.g. '1k–2.9k'."},
            "count": {"type": "integer"},
        },
        ["label", "count"],
    )


def _overview_schema() -> dict:
    """The payload of ``GET /api/overview``."""
    return _object(
        {
            "latest": {
                "oneOf": [_ref("SnapshotInfo"), {"enum": [None]}],
                "description": "The most recent snapshot; null before the first one.",
            },
            "snapshot_count": {"type": "integer"},
            "active_listings": {"type": "integer"},
            "distinct_items": {"type": "integer"},
            "supply_history": _array_of("SupplyPoint"),
            "type_breakdown": _array_of("NameCount"),
            "rarity_breakdown": _array_of("NameCount"),
            "price_distribution": _array_of("HistogramBin"),
            "top_movers": _array_of("PriceMover"),
        },
        [
            "latest",
            "snapshot_count",
            "active_listings",
            "distinct_items",
            "supply_history",
            "type_breakdown",
            "rarity_breakdown",
            "price_distribution",
            "top_movers",
        ],
    )


def _status_schema() -> dict:
    """Probe payloads: ``/healthz`` always; ``/readyz`` on success.

    ``database`` and ``snapshots`` appear only in the readyz success body.
    """
    return _object(
        {
            "status": {"type": "string"},
            "database": {"type": "string"},
            "snapshots": {"type": "integer"},
        },
        ["status"],
    )


def _snapshot_ack_schema() -> dict:
    """The ``201`` payload of ``POST /api/snapshot``."""
    return _object(
        {
            "snapshot_id": {"type": "integer", "format": "int64", "description": "Id of the appended snapshot."},
            "listings": {"type": "integer", "description": "Number of listings stored."},
        },
        ["snapshot_id", "listings"],
    )


def _snapshot_payload_schema() -> dict:
    """The ``POST /api/snapshot`` request body.

    Non-strict: the server ignores unknown top-level fields, so the schema
    documents the accepted payload instead of forbidding more than the server
    does.
    """
    return _object(
        {
            "listings": {"type": "array", "items": _ref("Listing"), "description": "All active listings of the snapshot."},
            "captured_at": {"type": "number", "description": "Unix seconds (UTC) the snapshot was taken; defaults to now."},
        },
        ["listings"],
        strict=False,
    )


def _error_schema() -> dict:
    return _object({"error": {"type": "string"}}, ["error"])


# --- paths ------------------------------------------------------------------


def _ingestion_path() -> dict:
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
                    "content": {"application/json": {"schema": _ref("SnapshotPayload")}},
                },
                "responses": {
                    "201": _json_response("snapshot stored", _ref("SnapshotAck")),
                    "400": _json_response("malformed payload or invalid listing fields", _ref("Error")),
                    "500": _json_response("database error", _ref("Error")),
                },
            }
        }
    }


def build_spec(*, include_ingestion: bool = False) -> dict:
    """Return the complete OpenAPI 3.0 document as a dict.

    The public document (``include_ingestion=False``) describes the read API
    and the probes only — how snapshots are ingested is an internal detail.
    """
    paths: dict[str, dict] = {
        "/healthz": {
            "get": {
                "summary": "Liveness probe",
                "description": "Always 200 while the process is up; never touches the database.",
                "responses": {"200": _json_response("process alive", _ref("Status"))},
            }
        },
        "/readyz": {
            "get": {
                "summary": "Readiness probe",
                "description": "200 once the database file is initialized and openable; 503 before init-db has run or on open errors.",
                "responses": {
                    "200": _json_response("database ready", _ref("Status")),
                    "503": _json_response("database not initialized or not openable", _ref("Error")),
                },
            }
        },
        "/api/overview": {
            "get": {
                "summary": "Aggregate stats for the overview tab",
                "description": "Current market stats: active listings, distinct items, supply history, type and rarity breakdowns, the current per-unit price histogram, and the biggest median-price movers between the two most recent data points.",
                "responses": {"200": _json_response("overview aggregates", _ref("Overview"))},
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
                        "listings, each enriched with display name, kind, icon, authoritative rarity, and per-unit price", _array_of("ListingRow")
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
                    "200": _json_response("item history", _ref("ItemDetail")),
                    "404": _json_response("item was never observed", _ref("Error")),
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
                "responses": {"200": _json_response("not-on-sale rows", _array_of("NotOnSaleRow"))},
            }
        },
        "/api/best-sellers": {
            "get": {
                "summary": "Items ranked by observed time-to-sale (fastest first)",
                "description": "Listing lifetime measured between consecutive observations; only fully observed listings that vanished with >= 1 day left on their countdown count as sales.",
                "parameters": [
                    _query_param("order", "Sort column.", enum=list(store._BEST_SELLER_SORTS), default="median_time"),
                    _query_param("dir", "Sort direction.", enum=["asc", "desc"], default="asc"),
                    _query_param("min_sales", "Only items with at least this many fully observed sales.", default=1, schema_type="integer"),
                ],
                "responses": {"200": _json_response("best-seller rows", _array_of("BestSellerRow"))},
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
                "responses": {"200": _json_response("best-value rows", _array_of("BestValueRow"))},
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
                "responses": {"200": _json_response("removed listings", _array_of("RemovedListing"))},
            }
        },
    }

    if include_ingestion:
        paths.update(_ingestion_path())

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
                "BestSellerRow": _best_seller_row_schema(),
                "BestValueRow": _best_value_row_schema(),
                "Error": _error_schema(),
                "HistogramBin": _histogram_bin_schema(),
                "ItemDetail": _item_detail_schema(),
                "ItemSummary": _item_summary_schema(),
                "Listing": _listing_schema(),
                "ListingRow": _listing_row_schema(),
                "NameCount": _name_count_schema(),
                "NotOnSaleRow": _not_on_sale_row_schema(),
                "Overview": _overview_schema(),
                "PreviousListing": _previous_listing_schema(),
                "PriceMover": _price_mover_schema(),
                "RemovedListing": _removed_listing_schema(),
                "RemovalReason": _removal_reason_schema(),
                "ScatterPoint": _scatter_point_schema(),
                "SeriesPoint": _series_point_schema(),
                "SnapshotAck": _snapshot_ack_schema(),
                "SnapshotInfo": _snapshot_info_schema(),
                "SnapshotPayload": _snapshot_payload_schema(),
                "Status": _status_schema(),
                "StockItem": _stock_item_schema(),
                "SupplyPoint": _supply_point_schema(),
            }
        },
    }


def spec_json(*, include_ingestion: bool = False) -> bytes:
    """The serialized OpenAPI document served at ``GET /openapi.json``.

    By default the public document: read endpoints and probes only, so the
    website does not leak how market data is ingested.
    """
    return json.dumps(build_spec(include_ingestion=include_ingestion), indent=2).encode()
