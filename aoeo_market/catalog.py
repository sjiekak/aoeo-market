"""Curated item catalog from the Project Celeste search database.

The search web app (``ProjectCeleste/celeste-search``, search.projectceleste.com)
ships a hand-curated item database that maps every *marketplace* listing id —
the ``ItemID`` seen on the wire — to human-readable metadata: a display name,
the authoritative rarity, the entity kind (advisor/blueprint/consumable/design/
item/material), an icon id, and, where the source records it, the description,
civilization, advisor age, seasonal event, and whether a crafting design
produces the item (``craftable``).

``scripts/build_catalog.py`` flattens that database into the committed
``aoeo_market/data/catalog.json`` (keyed by the lowercased wire id), so the
runtime path does **not** depend on the gitignored ``ProjectCeleste`` tree.
This module loads that file lazily and exposes lookups used to enrich the
snapshot-store views (see :mod:`aoeo_market.store`).

Rarity here is authoritative — it comes from the curated database, not from
the suffix-letter heuristic in :mod:`aoeo_market.market` (which stays as the
fallback for ids the catalog has never seen, e.g. brand-new event items).
"""

from __future__ import annotations

import json
from pathlib import Path

from .market import rarity_of as _heuristic_rarity

# Display rarity name -> numeric rank (higher = rarer).  "junk" (rank 0) sits
# below common; it is the source database's lowest crafting tier.
RARITY_RANK = {
    "Junk": 0,
    "Common": 1,
    "Uncommon": 2,
    "Rare": 3,
    "Epic": 4,
    "Legendary": 5,
}

_CATALOG_PATH = Path(__file__).with_name("data") / "catalog.json"
# Gear Dismantler output, keyed by gear type and item rarity (see
# scripts/build_catalog.py's sibling, tmp/gamefiles/build_dismantle_map.py).
_DISMANTLE_PATH = Path(__file__).with_name("data") / "dismantle_map.json"

_catalog: dict[str, dict] | None = None
_dismantle: dict | None = None


def _load() -> dict[str, dict]:
    global _catalog
    if _catalog is None:
        _catalog = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    return _catalog


def lookup(item_id: str) -> dict | None:
    """Return the catalog entry for a wire ``ItemID`` (case-insensitive)."""
    if not item_id:
        return None
    return _load().get(item_id.lower())


def rarity_of(item_id: str) -> tuple[int, str] | None:
    """Authoritative ``(rank, name)`` rarity, falling back to the heuristic.

    The curated database wins when it knows the item; otherwise the
    suffix-letter heuristic in :mod:`aoeo_market.market` still returns a
    best-effort rarity for ids the catalog has never seen.
    """
    entry = lookup(item_id)
    if entry and entry.get("rarity"):
        rank = RARITY_RANK.get(entry["rarity"])
        if rank is not None:
            return (rank, entry["rarity"])
    return _heuristic_rarity(item_id)


def name_of(item_id: str) -> str | None:
    """Human-readable display name for a wire ``ItemID``, if known."""
    entry = lookup(item_id)
    return entry.get("name") if entry else None


def is_craftable(item_id: str) -> bool:
    """Whether a crafting design produces this wire ``ItemID``.

    True when the curated database holds a design whose ``outputId`` is the
    item's canonical id — i.e. the item can be crafted.  Ids the catalog has
    never seen, and items no design produces, are ``False``.
    """
    entry = lookup(item_id)
    return bool(entry and entry.get("craftable"))


def type_of(item_id: str) -> str | None:
    """The gear type — the Dismantler guide's row, e.g. ``"Fire Pot"``."""
    entry = lookup(item_id)
    return entry.get("type") if entry else None


def recipe_of(item_id: str) -> dict | None:
    """Crafting recipe ``{school, level, materials:[{id, quantity}]}``, if any.

    Present only for items a crafting design produces (``craftable``).
    """
    entry = lookup(item_id)
    return entry.get("recipe") if entry else None


def _load_dismantle() -> dict:
    global _dismantle
    if _dismantle is None:
        _dismantle = json.loads(_DISMANTLE_PATH.read_text(encoding="utf-8"))
    return _dismantle


def is_dismantlable(entry: dict | None) -> bool:
    """Whether the Gear Dismantler accepts the item behind a catalog *entry*.

    In-game, *store-bought, Event and Questline completion reward equipment
    cannot be dismantled*.  Only the event class is reliably detectable from
    the curated catalog today (the ``event`` field, itself celeste-search's
    ``isEventReward``), so that is the rule applied for now; the Dismantler
    guide describes what a dismantlable item yields, not which items qualify.
    """
    return bool(entry and not entry.get("event"))


def dismantle_of(item_id: str) -> dict | None:
    """What the Gear Dismantler produces for this item, when it can and we know.

    The guide is keyed by gear type and item rarity, so an item needs both a
    known ``type`` and a rarity to resolve.  ``materials`` holds the raw
    material ids in guide order; an entry is ``None`` where the guide cell
    could not be read.  Returns ``None`` for items the guide does not cover and
    for items the Dismantler will not accept at all (see
    :func:`is_dismantlable`).
    """
    entry = lookup(item_id)
    if not entry or not entry.get("type") or not is_dismantlable(entry):
        return None
    tier = _load_dismantle().get("types", {}).get(entry["type"])
    if not tier:
        return None
    rar = rarity_of(item_id)
    rarity = rar[1] if rar else None
    mats = tier.get("dismantle", {}).get(rarity) if rarity else None
    if not mats:
        return None
    return {"type": entry["type"], "school": tier.get("school"), "rarity": rarity, "materials": mats}


def icon_fields(item_id: str) -> dict:
    """The compact catalog extras merged into analytical row dicts.

    ``kind`` and ``icon`` are what the front end needs to clip the item's
    sprite icon; ``craftable`` rides along so every row that names an item
    reports whether a design produces it.  Returns an empty dict when the
    catalog does not know the item, so callers can merge it into a row without
    a lookup round-trip elsewhere.
    """
    entry = lookup(item_id)
    if not entry:
        return {}
    out: dict = {}
    if entry.get("kind") is not None:
        out["kind"] = entry["kind"]
    if entry.get("icon") is not None:
        out["icon"] = entry["icon"]
    if entry.get("craftable"):
        out["craftable"] = True
    return out


def fields(item_id: str) -> dict:
    """The extra catalog-derived fields to merge into a listing/row dict.

    ``name`` is always present (``None`` when unknown, so callers can fall
    back to the raw id); every other field is omitted when the catalog has no
    value for it, keeping the JSON API payloads lean.  ``craftable`` is only
    emitted for items a design produces.
    """
    entry = lookup(item_id)
    out: dict = {"name": entry.get("name") if entry else None}
    if entry:
        for key in ("kind", "icon", "description", "civilization", "age", "event"):
            if entry.get(key) is not None:
                out[key] = entry[key]
        if entry.get("craftable"):
            out["craftable"] = True
    return out
