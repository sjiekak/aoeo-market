# Observation model — sold vs. expired

The server only ever transmits **active** listings; it never broadcasts a sale
or the buyer's identity to third parties. So from outside:

- **Listed** — a new `TransactionId` appears. Directly observable.
- **Removed** — a `TransactionId` disappears. We classify it with the expiry
  countdown:
  - **EXPIRED** — vanished at/after its known expiry time.
  - **SOLD_OR_CANCELLED** — vanished well before expiry. On a fixed-price market
    this is overwhelmingly a sale, but a seller cancelling is indistinguishable
    from the outside. We don't claim more than the data supports.

This is the ceiling for *any* external observer, including the existing "Global
Marketplace" Discord bot. Set `MarketObserver.expiry_grace_seconds` at least as
large as your poll interval so a listing that times out between polls isn't
misread as a sale.
