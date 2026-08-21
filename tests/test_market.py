"""Tests for the best-effort rarity heuristic read from item-id suffixes."""

import pytest

from aoeo_market.market import rarity_of


@pytest.mark.parametrize(
    ("item_id", "expected"),
    [
        ("Achichorius_E_IV", (4, "Epic")),
        ("Singh_C_IV", (1, "Common")),
        ("Makru_U_IV", (2, "Uncommon")),
        ("Solon_R_IV", (3, "Rare")),
        ("Alexander_L_IV", (5, "Legendary")),
        ("Arrows_IceKing_LEG", (5, "Legendary")),
        ("ArmorClth_R016_LEG", (5, "Legendary")),
        ("CreateMetalWorkingWarpaint_R", (3, "Rare")),
        ("CreateHorseBreedingSpear2H_U", (2, "Uncommon")),
        # no known rarity suffix
        ("SunShard", None),
        ("KingCobraVenom", None),
        ("CreateArmorBldg_L005", None),
        ("CreateArmorBldg_Winter2021", None),
    ],
)
def test_rarity_of(item_id, expected):
    assert rarity_of(item_id) == expected
