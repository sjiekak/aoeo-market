"""Tests for the best-effort rarity heuristic read from item-id suffixes."""

import pytest

from aoeo_market.market import Listing, rarity_of, summarize


def _mk(tx: int, item_id: str, item_type: str) -> Listing:
    return Listing(
        transaction_id=tx,
        seller_empire_id=7,
        buyer_character_id=-1,
        item_id=item_id,
        item_type=item_type,
        item_level=2,
        item_count=1,
        item_price=4,
        item_seed=5,
        seconds_till_expiry=6,
    )


def test_listing_to_dict_roundtrip():
    listing = _mk(1, "X_E_I", "Trait")
    assert Listing(**listing.to_dict()) == listing


def test_summarize_counts_total_distinct_and_types():
    listings = [
        _mk(1, "A_U_I", "Trait"),
        _mk(2, "A_U_I", "Trait"),  # same item id: not distinct
        _mk(3, "B_E_I", "Advisor"),
    ]
    s = summarize(listings)
    assert s["total"] == 3
    assert s["distinct_items"] == 2
    assert s["by_type"] == {"Trait": 2, "Advisor": 1}

    assert summarize([]) == {"total": 0, "distinct_items": 0, "by_type": {}}


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
