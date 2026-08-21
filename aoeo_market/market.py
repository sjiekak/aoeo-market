"""Parse ``<MarketPlaceItems>`` messages into structured listings.

Record shape observed on the wire::

    <MarketPlaceItemInfo SellerEmpireId="4072340471133720139">
      <TransactionId>1246582926234651764</TransactionId>
      <BuyerCharacterId>-1</BuyerCharacterId>
      <ItemID>ArmorPlt_Halloween2025</ItemID>
      <ItemType>Trait</ItemType>
      <ItemLevel>43</ItemLevel>
      <ItemCount>1</ItemCount>
      <ItemPrice>99000</ItemPrice>
      <ItemSeed>0</ItemSeed>
      <SecondsTillExpiry>2590620</SecondsTillExpiry>
    </MarketPlaceItemInfo>
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The server browse only ever returns active listings, for which BuyerCharacterId
# is this sentinel. A non-sentinel value would indicate a completed sale record.
NO_BUYER = -1

# Item ids encode rarity as a suffix letter — ``AdvisorName_E_IV`` (epic
# advisor, tier IV), ``Design_R`` (rare design), ``Arrows_IceKing_LEG``
# (legendary).  The letters seen in the wild are C/U/R/E/L plus the LEG
# legendary marker.  This is a best-effort heuristic, not a server field.
_RARITY_LETTER_RE = re.compile(r"_([CUREL])(?:_[IVX]+)?$")
_LEGENDARY_RE = re.compile(r"_(?:LEG|[Ll]egendary)(?:_[IVX]+)?$")
RARITY_RANK = {"C": 1, "U": 2, "R": 3, "E": 4, "L": 5}
RARITY_NAME = {1: "Common", 2: "Uncommon", 3: "Rare", 4: "Epic", 5: "Legendary"}


def rarity_of(item_id: str) -> tuple[int, str] | None:
    """Best-effort ``(rank, name)`` rarity read from an item id's suffix.

    Returns ``None`` when no known rarity suffix is present (materials and
    most consumables carry none).  Higher rank means rarer.
    """
    if _LEGENDARY_RE.search(item_id):
        return (5, RARITY_NAME[5])
    m = _RARITY_LETTER_RE.search(item_id)
    if m:
        rank = RARITY_RANK[m.group(1)]
        return (rank, RARITY_NAME[rank])
    return None


_RECORD_RE = re.compile(
    rb'<MarketPlaceItemInfo\s+SellerEmpireId="(?P<seller>-?\d+)"\s*>(?P<body>.*?)</MarketPlaceItemInfo>',
    re.DOTALL,
)


def _field(body: bytes, tag: str) -> str | None:
    m = re.search(rb"<%b>(.*?)</%b>" % (tag.encode(), tag.encode()), body)
    return m.group(1).decode("utf-8", "replace") if m else None


def _get(body: bytes, tag: str, default: int | str = 0) -> int | str:
    v = _field(body, tag)
    return v if v is not None else default


@dataclass(frozen=True)
class Listing:
    transaction_id: int
    seller_empire_id: int
    buyer_character_id: int
    item_id: str
    item_type: str
    item_level: int
    item_count: int
    item_price: int
    item_seed: int
    seconds_till_expiry: int

    @property
    def is_active(self) -> bool:
        return self.buyer_character_id == NO_BUYER


def parse_listings(data: bytes) -> list[Listing]:
    """Extract every ``MarketPlaceItemInfo`` record from a decompressed message
    (or from a concatenation of several).

    Server messages arrive both as UTF-8 XML (the offline captures) and as
    UTF-16-LE XML with a BOM (the live login's ``<Empire><Offers>`` document),
    so the records are matched in the raw bytes and again in a NUL-stripped
    copy of them (which collapses UTF-16-LE ASCII markup to plain ASCII).
    Results are deduplicated by transaction id.
    """
    out: list[Listing] = []
    seen: set[int] = set()
    for corpus in (data, data.replace(b"\x00", b"")):
        for m in _RECORD_RE.finditer(corpus):
            body = m.group("body")

            try:
                tx = int(_field(body, "TransactionId"))
                if tx in seen:
                    continue
                seen.add(tx)
                out.append(
                    Listing(
                        transaction_id=tx,
                        seller_empire_id=int(m.group("seller")),
                        buyer_character_id=int(_get(body, "BuyerCharacterId", -1)),
                        item_id=str(_get(body, "ItemID", "")),
                        item_type=str(_get(body, "ItemType", "")),
                        item_level=int(_get(body, "ItemLevel", 0)),
                        item_count=int(_get(body, "ItemCount", 1)),
                        item_price=int(_get(body, "ItemPrice", 0)),
                        item_seed=int(_get(body, "ItemSeed", 0)),
                        seconds_till_expiry=int(_get(body, "SecondsTillExpiry", 0)),
                    )
                )
            except (TypeError, ValueError):
                # Skip records missing the primary key; keep parsing the rest.
                continue
    return out
