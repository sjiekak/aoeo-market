"""Snapshot-diffing market observer.

Feed it successive snapshots of the *active* marketplace listings (each snapshot
is whatever the browse returned, plus the wall-clock time it was taken). It emits
events describing what changed:

* ``LISTED``  - a transaction id not seen before.
* ``REMOVED`` - a transaction id that was present and is now gone. Removals are
  classified using each listing's expiry countdown:

    - ``EXPIRED``            - it vanished at (or after) its known expiry time.
    - ``SOLD_OR_CANCELLED``  - it vanished well before expiry. On a fixed-price
      market this is overwhelmingly a sale, but a seller cancelling a listing is
      indistinguishable from the outside, so we do not claim more than we can
      prove.

Why this is the best an external observer can do: the server only ever transmits
active listings (``BuyerCharacterId == -1``); it never broadcasts a sale or who
bought. The expiry countdown is the one signal that lets us separate "left early"
from "timed out". See the project README.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum

from .market import Listing


class RemovalReason(str, Enum):
    EXPIRED = "EXPIRED"
    SOLD_OR_CANCELLED = "SOLD_OR_CANCELLED"


@dataclass(frozen=True)
class ListedEvent:
    at: float
    listing: Listing
    kind: str = "LISTED"


@dataclass(frozen=True)
class RemovedEvent:
    at: float
    listing: Listing
    reason: RemovalReason
    expected_expiry_at: float
    kind: str = "REMOVED"


Event = ListedEvent | RemovedEvent


@dataclass
class _Tracked:
    listing: Listing
    last_seen_at: float
    # Best current estimate of the absolute time this listing will expire,
    # refined on every observation from its live seconds_till_expiry.
    expected_expiry_at: float


@dataclass
class MarketObserver:
    """Stateful diff engine. Not thread-safe; drive it from one loop."""

    # A removal seen at or after (expected_expiry_at - grace) counts as EXPIRED.
    # Set at least as large as your poll interval so a listing that expires
    # between polls is not misread as a sale.
    expiry_grace_seconds: float = 120.0
    clock: Callable[[], float] = time.time

    _tracked: dict[int, _Tracked] = field(default_factory=dict)

    def observe(self, listings: Iterable[Listing], at: float | None = None) -> list[Event]:
        """Apply one snapshot; return the events it produced."""
        now = self.clock() if at is None else at
        events: list[Event] = []
        seen: set[int] = set()

        for lst in listings:
            seen.add(lst.transaction_id)
            expiry_at = now + lst.seconds_till_expiry
            if lst.transaction_id not in self._tracked:
                self._tracked[lst.transaction_id] = _Tracked(lst, now, expiry_at)
                events.append(ListedEvent(at=now, listing=lst))
            else:
                t = self._tracked[lst.transaction_id]
                t.listing = lst
                t.last_seen_at = now
                t.expected_expiry_at = expiry_at

        for tx in list(self._tracked):
            if tx in seen:
                continue
            t = self._tracked.pop(tx)
            if now >= t.expected_expiry_at - self.expiry_grace_seconds:
                reason = RemovalReason.EXPIRED
            else:
                reason = RemovalReason.SOLD_OR_CANCELLED
            events.append(
                RemovedEvent(
                    at=now,
                    listing=t.listing,
                    reason=reason,
                    expected_expiry_at=t.expected_expiry_at,
                )
            )
        return events

    @property
    def active(self) -> dict[int, Listing]:
        return {tx: t.listing for tx, t in self._tracked.items()}
