# Age of empire online market — read-only observer for the AoEO / Project Celeste Global Marketplace

A headless client that watches the Age of Empires Online (Project Celeste)
marketplace and emits events when items are **listed** and **removed**, with a
best-effort **sold vs. expired** classification.

## Quick start (offline, works today)

```bash
uv run pytest                                              # offline test suite
uv run python -m aoeo_market.cli dump   capture_*.pcapng.gz   # list active listings
uv run python -m aoeo_market.cli replay A.pcapng B.pcapng     # diff two snapshots -> events
```

> All Python in this project runs through `uv` and the project-local `.venv`.

## Documentation

- [Marketplace protocol](docs/protocol.md) — TCP 1510 framing, opcodes,
  zlib-compressed XML messages, and the listing record.
- [Authentication](docs/authentication.md) — the TCP 4564 "Celeste Network"
  login and the two account systems.
- [Observation model](docs/observation.md) — how listed / removed events are
  classified as sold vs. expired.
- [Live client](docs/live-client.md) — what remains to finish against a live
  login.

## Layout

The offline pipeline (codec, parser, observer) is implemented and tested against
real packet captures, and the live authentication path is implemented in
`aoeo_market/auth.py` (unit-tested against the captured login bytes; the actual
network round-trip still needs a live run). Run `uv run pytest` to validate
everything that does not need a live server.


```
aoeo_market/
  constants.py    shared Celeste server host/port and client defaults
  protocol.py     frame codec, zlib member scanner, message builders
  market.py       MarketPlaceItemInfo XML -> Listing
  observer.py     snapshot diff -> LISTED / REMOVED(sold|expired) events
  pcap_source.py  read listings from a .pcapng (offline data source)
  auth.py         TCP 4564 "Celeste Network" login (email+password -> session)
  client.py       live TCP 1510 client
  cli.py          `dump`, `replay`, and `probe` commands
  live_probe.py   live connection probe
tests/            end-to-end tests over the real captures
```

## Disclaimer

aoeo_market is a hobbyst project
aoeo_market is not endorsed by or affiliated with Microsoft.
aoeo_market is not endorsed by or affiliated with Project Celeste.
