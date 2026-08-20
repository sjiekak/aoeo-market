"""Snapshot-diffing market observer.

Feed it successive snapshots of the *active* marketplace listings (each snapshot
is whatever the browse returned, plus the wall-clock time it was taken). It emits
events describing what changed:

* ``LISTED``  - a transaction id not seen before.
* ``REMOVED`` - a transaction id that was present and is now gone. The server
  never sends a removal notice: at any given time you only have the list of
  active items, and users can withdraw their sales at any moment, so a sold,
  withdrawn, or expired listing all look the same — the listing simply stops
  appearing. The expiry countdown is the one signal we have, so removals are
  classified like this:

    - ``EXPIRED`` - it vanished with **less than a day remaining** on its
      countdown (i.e. within ``expiry_window_seconds`` of its expected expiry).
      With so little time left, timing out unsold is the overwhelmingly likely
      cause.
    - ``REMOVED`` - it vanished with at least a day to spare. That is a sale or
      a withdrawal — indistinguishable from the outside, so no stronger claim
      is made.

Why this is the best an external observer can do: the server only ever transmits
active listings (``BuyerCharacterId == -1``); it never broadcasts a sale or who
bought. See the project README.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum

from .market import Listing


class RemovalReason(str, Enum):
    EXPIRED = "EXPIRED"  # vanished with < 1 day left -> timed out unsold
    REMOVED = "REMOVED"  # vanished with time to spare -> sold or withdrawn


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

    # A removal whose expected expiry is within this window counts as EXPIRED;
    # anything earlier is REMOVED with no cause claimed (sold vs. withdrawn is
    # indistinguishable). One day by default. Set it at least as large as your
    # poll interval so a listing that times out between polls is still read as
    # expired.
    expiry_window_seconds: float = 86400.0
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
            if t.expected_expiry_at - now < self.expiry_window_seconds:
                reason = RemovalReason.EXPIRED
            else:
                reason = RemovalReason.REMOVED
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
