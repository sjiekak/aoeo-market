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

_RECORD_RE = re.compile(
    rb'<MarketPlaceItemInfo\s+SellerEmpireId="(?P<seller>-?\d+)"\s*>(?P<body>.*?)</MarketPlaceItemInfo>',
    re.DOTALL,
)


def _field(body: bytes, tag: str) -> str | None:
    m = re.search(rb"<%b>(.*?)</%b>" % (tag.encode(), tag.encode()), body)
    return m.group(1).decode("utf-8", "replace") if m else None


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
    (or from a concatenation of several)."""
    out: list[Listing] = []
    for m in _RECORD_RE.finditer(data):
        body = m.group("body")

        def g(tag: str, default: int | str = 0) -> int | str:
            v = _field(body, tag)
            return v if v is not None else default

        try:
            out.append(
                Listing(
                    transaction_id=int(_field(body, "TransactionId")),
                    seller_empire_id=int(m.group("seller")),
                    buyer_character_id=int(g("BuyerCharacterId", -1)),
                    item_id=str(g("ItemID", "")),
                    item_type=str(g("ItemType", "")),
                    item_level=int(g("ItemLevel", 0)),
                    item_count=int(g("ItemCount", 1)),
                    item_price=int(g("ItemPrice", 0)),
                    item_seed=int(g("ItemSeed", 0)),
                    seconds_till_expiry=int(g("SecondsTillExpiry", 0)),
                )
            )
        except (TypeError, ValueError):
            # Skip records missing the primary key; keep parsing the rest.
            continue
    return out
