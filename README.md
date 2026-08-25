# Age of empire online market — read-only observer for the AoEO / Project Celeste Global Marketplace

A headless client that watches the Age of Empires Online (Project Celeste)
marketplace and emits events when items are **listed** and **removed**, with a
best-effort **expired vs. removed** classification: a listing that vanishes
with less than a day left on its countdown is EXPIRED, anything earlier is
REMOVED (sold or withdrawn — indistinguishable from the outside). Hourly
snapshots can be persisted to DuckDB and browsed on the **market website**:
price distributions and history, best-selling items ranked by time-to-sale,
items currently not on sale, recent sold/expired removals, and other trading
intelligence.

## Quick start (offline, works today)

```bash
uv run pytest                                              # offline test suite
uv run python -m aoeo_market.cli dump   capture_*.pcapng.gz   # list active listings
uv run python -m aoeo_market.cli replay A.pcapng B.pcapng     # diff two snapshots -> events
uv run python -m aoeo_market.cli probe   --game                    # live login probe
uv run python -m aoeo_market.cli fetch                             # read the live market
uv run python -m aoeo_market.cli fetch  --watch                    # stream LISTED/REMOVED events
uv run python -m aoeo_market.cli fetch  --store http://127.0.0.1:8000 --quiet  # snapshot via the web API
uv run python -m aoeo_market.web --db market.db                    # serve the dashboard
```

> The live commands detect your local IPv4 address and use it as the default;
> pass `--local-ip <your-ip>` to override it.
>
> All Python in this project runs through `uv` and the project-local `.venv`.

## Documentation

- [Marketplace protocol](docs/protocol.md) — TCP 1510 framing (context /
  channel / length / flags+opcode / counter), the login bundle, zlib-compressed
  XML messages, and the listing record.
- [Authentication](docs/authentication.md) — the TCP 4564 "Celeste Network"
  login, the per-install constants, and the 1510/1500 game-service logins.
- [Observation model](docs/observation.md) — how listed / removed events are
  classified: EXPIRED only when a listing vanishes with less than a day
  remaining, REMOVED otherwise (sold vs. withdrawn is indistinguishable).
- [Live client](docs/live-client.md) — status of the live login/polling path
  (validated against the real server on 2026-08-17).
- [Market website](docs/market-website.md) — the DuckDB snapshot store, the
  hourly cron fetch, the dashboard views, and the JSON API.

## Layout

The offline pipeline (codec, parser, observer) is implemented and tested against
real packet captures, and the live path (4564 login → 1510 login bundle →
market sweep → observe) has been run successfully against the real servers:
a live poll returns the whole marketplace (all six categories — gear, advisors,
consumables, designs, materials, blueprints) and the observer emits LISTED /
REMOVED events across polls. Run `uv run pytest` to validate everything that
does not need a live server.


```
aoeo_market/
  constants.py    shared Celeste server host/port and client defaults
  protocol.py     frame codec (ctx/ch/len/flags+op/counter), zlib scanner,
                  login bundle and market sweep builders
  market.py       MarketPlaceItemInfo XML (UTF-8 and UTF-16) -> Listing
  catalog.py      curated item catalog (display name, authoritative rarity,
                  kind, icon, …) keyed by marketplace id, with the suffix
                  heuristic as fallback; data/catalog.json is its committed data
  observer.py     snapshot diff -> LISTED / REMOVED(expired|removed) events
  pcap_source.py  read listings from a .pcapng (offline data source)
  auth.py         TCP 4564 "Celeste Network" login (email+password -> session)
  client.py       live TCP 1510 client
  cli.py          `dump`, `replay`, `probe`, `fetch`, and `init-db` commands
  live_probe.py   live connection probe
  store.py        DuckDB snapshot store + analytics queries
  web/            market website package (stdlib HTTP server, OpenAPI spec,
                  dashboard page)
scripts/
  build_catalog.py  regenerate aoeo_market/data/catalog.json from the
                  ProjectCeleste/celeste-search item database (maintenance tool)
tests/            unit tests (no captures needed)
tests/capture/    capture-dependent tests + reference data — local-only,
                  gitignored along with the .pcapng captures
```

## Disclaimer

aoeo_market is a hobbyst project
aoeo_market is not endorsed by or affiliated with Microsoft.
aoeo_market is not endorsed by or affiliated with Project Celeste.
