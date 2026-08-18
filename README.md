# Age of empire online market — read-only observer for the AoEO / Project Celeste Global Marketplace

A headless client that watches the Age of Empires Online (Project Celeste)
marketplace and emits events when items are **listed** and **removed**, with a
best-effort **sold vs. expired** classification.

## Quick start (offline, works today)

```bash
uv run pytest                                              # offline test suite
uv run python -m aoeo_market.cli dump   capture_*.pcapng.gz   # list active listings
uv run python -m aoeo_market.cli replay A.pcapng B.pcapng     # diff two snapshots -> events
uv run python -m aoeo_market.live_probe --local-ip <your-ip> --game   # live login probe
```

> All Python in this project runs through `uv` and the project-local `.venv`.

## Documentation

- [Marketplace protocol](docs/protocol.md) — TCP 1510 framing (context /
  channel / length / flags+opcode / counter), the login bundle, zlib-compressed
  XML messages, and the listing record.
- [Authentication](docs/authentication.md) — the TCP 4564 "Celeste Network"
  login, the per-install constants, and the 1510/1500 game-service logins.
- [Observation model](docs/observation.md) — how listed / removed events are
  classified as sold vs. expired.
- [Live client](docs/live-client.md) — status of the live login/polling path
  (validated against the real server on 2026-08-17).

## Layout

The offline pipeline (codec, parser, observer) is implemented and tested against
real packet captures, and the live path (4564 login → 1510 login bundle →
market sweep → observe) has been run successfully against the real servers:
a live poll returns the whole marketplace (~650 listings) and the observer
emits LISTED / REMOVED events across polls. Run `uv run pytest` to validate
everything that does not need a live server.


```
aoeo_market/
  constants.py    shared Celeste server host/port and client defaults
  protocol.py     frame codec (ctx/ch/len/flags+op/counter), zlib scanner,
                  login bundle and market sweep builders
  market.py       MarketPlaceItemInfo XML (UTF-8 and UTF-16) -> Listing
  observer.py     snapshot diff -> LISTED / REMOVED(sold|expired) events
  pcap_source.py  read listings from a .pcapng (offline data source)
  auth.py         TCP 4564 "Celeste Network" login (email+password -> session)
  client.py       live TCP 1510 client
  cli.py          `dump`, `replay`, and `probe` commands
  live_probe.py   live connection probe
tests/            unit tests (no captures needed)
tests/capture/    capture-dependent tests + reference data — local-only,
                  gitignored along with the .pcapng captures
```

## Disclaimer

aoeo_market is a hobbyst project
aoeo_market is not endorsed by or affiliated with Microsoft.
aoeo_market is not endorsed by or affiliated with Project Celeste.
