# Observation model — expired vs. removed

The server only ever transmits **active** listings; it never broadcasts a sale,
a withdrawal, or the buyer's identity. At any given time the only input is the
current list of market items, and users can withdraw their sales at any moment.
So from outside:

- **Listed** — a new `TransactionId` appears. Directly observable.
- **Removed** — a `TransactionId` disappears. Sold, withdrawn, and expired all
  look identical: the listing simply stops appearing. The expiry countdown is
  the one signal we have, so we classify with a one-day rule:
  - **EXPIRED** — it vanished with **less than a day remaining** on its
    countdown (within `MarketObserver.expiry_window_seconds` of its expected
    expiry). With so little time left, timing out unsold is the overwhelmingly
    likely cause.
  - **REMOVED** — it vanished with at least a day to spare. That is a sale or a
    withdrawal; we do not claim which.

This is the ceiling for *any* external observer, including the existing "Global
Marketplace" Discord bot. Set `MarketObserver.expiry_window_seconds` at least as
large as your poll interval so a listing that times out between polls is still
read as expired.
