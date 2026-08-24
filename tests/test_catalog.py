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
