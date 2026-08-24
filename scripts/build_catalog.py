"""Build the market item catalog from the Project Celeste search database.

The Project Celeste "search" web app (``ProjectCeleste/celeste-search``, served
at search.projectceleste.com) ships a curated item database under
``src/assets/db/*.json``.  Each entity carries a ``marketplace`` array that
maps its canonical id to the **actual** marketplace listing ids (the
``ItemID`` seen on the wire), plus the authoritative rarity for each listing.
That is a strictly richer, hand-curated source of truth than the suffix-letter
heuristic in :mod:`aoeo_market.market`.

This script flattens that database into one compact, stable JSON file keyed by
the lowercased marketplace/wire item id::

    {
      "xerxes_l_iv": {
        "name": "Xerxes the Great",
        "rarity": "Legendary",
        "kind": "advisor",
        "icon": "CxsWN9Z6",
        "description": "…",
        "civilization": "persian",
        "age": 4
      },
      …
    }

The generated file is committed (``aoeo_market/data/catalog.json``) so the
market observer does **not** depend on the gitignored ``ProjectCeleste`` tree
at runtime.  Re-run this script whenever the source database changes::

    uv run python scripts/build_catalog.py

The source database is not part of this repository (``ProjectCeleste*`` is
gitignored); the script is a maintenance tool, not part of the runtime path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Canonical rarity names (lowercase in the source db) -> display names used
# across the aoeo-market project (see aoeo_market.catalog.RARITY_RANK).
RARITY_TITLE = {
    "legendary": "Legendary",
    "epic": "Epic",
    "rare": "Rare",
    "uncommon": "Uncommon",
    "common": "Common",
    "junk": "Junk",
}

# The kinds mirror the source entity files; "material" covers both
# materials.json and the shared.json material dictionary.
_SOURCE_FILES = (
    ("advisors.json", "advisor"),
    ("blueprints.json", "blueprint"),
    ("consumables.json", "consumable"),
    ("designs.json", "design"),
    ("items.json", "item"),
    ("materials.json", "material"),
)


def _title_rarity(value: str | None) -> str | None:
    if not value:
        return None
    return RARITY_TITLE.get(value.strip().lower())


def _rarity_slot(entity: dict, query_rarity: str | None) -> str | None:
    """Resolve the canonical lowercase rarity key for an entity.

    Marketplace queries carry ``rarity``; blueprint/design/item/material
    entities carry a top-level ``rarity``; advisors/consumables carry a
    ``rarities`` map keyed by rarity name.  Returns the *lowercase* rarity
    name (a key into ``RARITY_TITLE`` and the entity's ``rarities`` map).
    """
    rar = (query_rarity or entity.get("rarity") or "").strip().lower()
    if rar:
        return rar
    rarities = entity.get("rarities")
    if rarities:
        return next(iter(rarities))
    return None


def _icon(entity: dict, rarity_key: str | None) -> str | None:
    if entity.get("icon"):
        return entity["icon"]
    rarities = entity.get("rarities")
    if rarities and rarity_key in rarities:
        return rarities[rarity_key].get("icon")
    return None


def _description(entity: dict, rarity_key: str | None) -> str | None:
    if entity.get("description"):
        return entity["description"]
    rarities = entity.get("rarities")
    if rarities and rarity_key in rarities:
        return rarities[rarity_key].get("description")
    return None


def _set_entry(catalog: dict, key: str, entry: dict) -> None:
    """Insert *entry* keyed by *key*; first writer wins (ids are unique)."""
    key = key.lower()
    if key and key not in catalog:
        catalog[key] = entry


def build(src_dir: Path) -> dict:
    """Flatten the search database into ``{lowercased id: entry}``."""
    catalog: dict[str, dict] = {}

    for filename, kind in _SOURCE_FILES:
        data = json.loads((src_dir / filename).read_text(encoding="utf-8"))
        for entity in data:
            marketplace = entity.get("marketplace") or []
            # Materials carry no `marketplace` array — their id *is* the wire
            # item id (see materials.json and the Material interface).
            if kind == "material":
                marketplace = [{"id": entity.get("id")}]
            for query in marketplace:
                qid = query.get("id")
                if not qid:
                    continue
                rarity_key = _rarity_slot(entity, query.get("rarity"))
                name = entity.get("name") or entity.get("outputName")
                entry = {
                    "name": name,
                    "rarity": _title_rarity(rarity_key),
                    "kind": kind,
                }
                icon = _icon(entity, rarity_key)
                if icon:
                    entry["icon"] = icon
                description = _description(entity, rarity_key)
                if description:
                    entry["description"] = description
                if entity.get("civilization"):
                    entry["civilization"] = entity["civilization"]
                if entity.get("age") is not None:
                    entry["age"] = entity["age"]
                if entity.get("event"):
                    entry["event"] = entity["event"]
                _set_entry(catalog, qid, entry)

    # shared.json is a single object whose `materials` dict mirrors
    # materials.json entries (id -> {name, icon, rarity}).
    shared = json.loads((src_dir / "shared.json").read_text(encoding="utf-8"))
    for mat_id, mat in shared.get("materials", {}).items():
        entry = {
            "name": mat.get("name"),
            "rarity": _title_rarity(mat.get("rarity")),
            "kind": "material",
        }
        if mat.get("icon"):
            entry["icon"] = mat["icon"]
        _set_entry(catalog, mat_id, entry)

    return catalog


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build the market item catalog from the celeste-search database.")
    p.add_argument("--src", default="ProjectCeleste/celeste-search/src/assets/db", help="path to the celeste-search db directory")
    p.add_argument("--out", default="aoeo_market/data/catalog.json", help="output JSON path")
    args = p.parse_args(argv)

    src = Path(args.src)
    if not (src / "items.json").exists():
        print(f"error: no celeste-search database at {src}", file=sys.stderr)
        return 2

    catalog = build(src)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Sorted keys -> deterministic, diff-friendly output; compact separators
    # keep the committed file small (it is read into memory at import time).
    out.write_text(json.dumps(catalog, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(catalog)} items)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
