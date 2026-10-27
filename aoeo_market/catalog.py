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
import re
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

# The seasonal events as the curated database names them, and the tokens the
# game and the marketplace ids use for them.
_EVENT_ID_TOKENS = {"se": "summer", "he": "halloween", "we": "winter"}
_EVENT_WORDS = ("halloween", "summer", "winter")
_EVENT_ID_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_EVENT_ACRONYM_RE = re.compile(r"(se|he|we)(\d{4})")

_CATALOG_PATH = Path(__file__).with_name("data") / "catalog.json"
# Gear Dismantler output: a bare gear type -> tier map, each tier carrying the
# guide's per-rarity material lists and the crafting school.
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


def _event_from_id(item_id: str) -> dict | None:
    """Recover ``{name[, year]}`` from a wire id that encodes its event.

    Covers ids such as ``SE2025_…``, ``HE_Blackwood``, ``WE_IceKingsHeart``,
    ``Axe2H_Halloween2020``, ``CreateArmorBldg_Winter2021`` and
    ``…_IceKing_LEG``.
    """
    text = str(item_id or "").lower()
    name: str | None = None
    year: int | None = None
    for token in _EVENT_ID_SPLIT_RE.split(text):
        if not token:
            continue
        if token in _EVENT_ID_TOKENS:
            name = _EVENT_ID_TOKENS[token]
            break
        acronym = _EVENT_ACRONYM_RE.fullmatch(token)
        if acronym:
            name = _EVENT_ID_TOKENS[acronym.group(1)]
            year = int(acronym.group(2))
            break
        for word in _EVENT_WORDS:
            if token.startswith(word):
                name = word
                tail = token[len(word) :]
                if len(tail) == 4 and tail.isdigit():
                    year = int(tail)
                break
        if name:
            break
    if name is None and "iceking" in text:
        name = "winter"
    if name is None:
        return None
    event: dict = {"name": name}
    if year is not None:
        event["year"] = year
    return event


def _vanity_event(entry: dict) -> dict | None:
    """A vanity item's name can name its event when its id does not."""
    if entry.get("kind") != "item" or not str(entry.get("type") or "").startswith("Vanity"):
        return None
    name = str(entry.get("name") or "").lower()
    for word in _EVENT_WORDS:
        if word in name:
            return {"name": word}
    return None


def event_of(item_id: str) -> dict | None:
    """The seasonal event an item belongs to, if any.

    The curated catalog's ``event`` field is authoritative.  For items whose
    catalog entry has no event but whose wire id encodes one (e.g.
    ``SE2025_…`` or ``CreateArmorBldg_Winter2021``), the id is parsed; event
    vanity items named Halloween/Winter/Summer are the last fallback.  Returns
    ``{name[, year]}`` — the shape the catalog stores — or ``None`` for
    ordinary items.
    """
    entry = lookup(item_id)
    if entry:
        raw = entry.get("event") or {}
        if raw.get("name"):
            event = {"name": str(raw["name"])}
            if isinstance(raw.get("year"), int):
                event["year"] = raw["year"]
            return event
    event = _event_from_id(item_id)
    if event is None and entry is not None:
        event = _vanity_event(entry)
    return event


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


def craftable_ids() -> list[str]:
    """Every catalog key (lowercased wire id) that carries a crafting recipe."""
    return [key for key, entry in _load().items() if entry.get("recipe")]


def suggestion_index() -> list[dict]:
    """Every catalog item as a compact row for the dashboard's type-ahead.

    The web app loads this list once from ``/static/suggestions.json`` (see
    ``scripts/build_suggestions.py``) and matches it in the browser, so typing
    never asks the server at all.  Rows carry only the fields a suggestion
    shows, and the ids are sorted so the committed asset is deterministic.
    """
    catalog = _load()
    out: list[dict] = []
    for key in sorted(catalog):
        entry = catalog[key]
        row: dict = {"item_id": key, "name": entry.get("name")}
        for field in ("type", "kind", "icon"):
            if entry.get(field) is not None:
                row[field] = entry[field]
        rarity = rarity_of(key)
        if rarity:
            row["rarity"] = rarity[1]
        if entry.get("craftable"):
            row["craftable"] = True
        out.append(row)
    return out


def search(query: str, limit: int = 25) -> list[dict]:
    """Entries whose wire id or display name contains ``query``.

    Case-insensitive, and ranked so a known id lands first: exact id, then id
    prefix, then name prefix, then any other substring match, id-ordered within
    a rank.  Each row carries the same curated identity the JSON API attaches
    to an item elsewhere (``fields`` plus type and rarity), so a caller only has
    to add whatever market data it has.
    """
    needle = query.strip().lower()
    if not needle:
        return []
    ranked: list[tuple[int, str]] = []
    for key, entry in _load().items():
        name = (entry.get("name") or "").lower()
        if needle == key:
            rank = 0
        elif key.startswith(needle):
            rank = 1
        elif name.startswith(needle):
            rank = 2
        elif needle in key or needle in name:
            rank = 3
        else:
            continue
        ranked.append((rank, key))
    ranked.sort()
    out: list[dict] = []
    for _, key in ranked[: max(limit, 0)]:
        rar = rarity_of(key)
        row: dict = {
            "item_id": key,
            "type": type_of(key),
            "rarity": rar[1] if rar else None,
            "rarity_rank": rar[0] if rar else 0,
        }
        row.update(fields(key))
        out.append(row)
    return out


def _load_dismantle() -> dict[str, dict]:
    """Gear type -> ``{"dismantle": {rarity: [material_id, ...]}, "school": ...}``."""
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
    tier = _load_dismantle().get(entry["type"])
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
    reports whether a design produces it, and ``event`` so the item-list views
    can be filtered by seasonal event in the browser.  An unknown id still gets
    whatever :func:`event_of` can recover from the id itself.
    """
    entry = lookup(item_id)
    out: dict = {}
    if entry:
        if entry.get("kind") is not None:
            out["kind"] = entry["kind"]
        if entry.get("icon") is not None:
            out["icon"] = entry["icon"]
        if entry.get("craftable"):
            out["craftable"] = True
    event = event_of(item_id)
    if event is not None:
        out["event"] = event
    return out


def fields(item_id: str) -> dict:
    """The extra catalog-derived fields to merge into a listing/row dict.

    ``name`` is always present (``None`` when unknown, so callers can fall
    back to the raw id); every other field is omitted when the catalog has no
    value for it, keeping the JSON API payloads lean.  ``craftable`` is only
    emitted for items a design produces, and ``event`` only for seasonal event
    items (curated, or recovered from the id/name by :func:`event_of`).
    """
    entry = lookup(item_id)
    out: dict = {"name": entry.get("name") if entry else None}
    if entry:
        for key in ("kind", "icon", "description", "civilization", "age"):
            if entry.get(key) is not None:
                out[key] = entry[key]
        if entry.get("craftable"):
            out["craftable"] = True
    event = event_of(item_id)
    if event is not None:
        out["event"] = event
    return out
