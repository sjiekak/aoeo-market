"""Tests for the curated item catalog and its heuristic rarity fallback."""

import json
from pathlib import Path

from aoeo_market import catalog


def test_catalog_is_present_and_keyed_lowercase():
    path = Path(catalog.__file__).with_name("data") / "catalog.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) > 3000
    assert all(k == k.lower() for k in data)


def test_lookup_is_case_insensitive():
    entry = catalog.lookup("Xerxes_L_IV")
    assert entry is not None
    assert entry["name"] == "Xerxes the Great"
    assert entry["rarity"] == "Legendary"
    assert entry["kind"] == "advisor"
    assert entry["civilization"] == "persian"
    assert catalog.lookup("xerxes_l_iv") == entry
    assert catalog.lookup("does-not-exist") is None


def test_rarity_is_authoritative_and_falls_back_to_heuristic():
    # A material has no rarity suffix letter, so the suffix heuristic alone
    # would return None; the curated catalog supplies the real rarity.
    assert catalog.rarity_of("4PureGoldIngot") == (4, "Epic")
    # Ids the catalog has never seen still fall back to the suffix heuristic.
    assert catalog.rarity_of("Sword_U_III") == (2, "Uncommon")
    assert catalog.rarity_of("UntaggedMaterial") is None


def test_name_of():
    assert catalog.name_of("se_sunshard") == "Sun Shard"
    assert catalog.name_of("unknown-id") is None


def test_fields_omit_absent_extras():
    f = catalog.fields("4PureGoldIngot")
    assert f["name"] == "Pure Gold Ingots"
    assert f["kind"] == "material"
    assert f["icon"]
    assert f["description"]
    # Unknown id: only a null name is merged in.
    assert catalog.fields("nope") == {"name": None}


def test_sprite_index_covers_every_catalog_icon():
    """Every catalog (kind, icon) resolves in the front-end sprite index.

    The dashboard clips icons in the browser from ``/static/sprites.json``;
    this keeps that committed asset in sync with the catalog it enriches.
    """
    catalog_path = Path(catalog.__file__).with_name("data") / "catalog.json"
    sprites_path = Path(__file__).resolve().parents[1] / "aoeo_market" / "web" / "static" / "sprites.json"
    catalog_data = json.loads(catalog_path.read_text(encoding="utf-8"))
    sprites = json.loads(sprites_path.read_text(encoding="utf-8"))

    missing = []
    for wire_id, entry in catalog_data.items():
        kind = entry.get("kind")
        icon = entry.get("icon")
        if kind is None or icon is None:
            continue
        if icon not in sprites.get(kind, {}):
            missing.append(wire_id)
    assert not missing, f"{len(missing)} catalog entries lack a sprite position, e.g. {missing[:5]}"

    # The front end scales each sheet with background-size: cols*100% rows*100%;
    # the grid metadata must be present for every kind that has icons.
    sheets = sprites.get("@sheets", {})
    for kind in sprites:
        if kind == "@sheets":
            continue
        assert kind in sheets, f"missing @sheets grid for {kind!r}"
        assert sheets[kind]["cols"] > 0 and sheets[kind]["rows"] > 0
