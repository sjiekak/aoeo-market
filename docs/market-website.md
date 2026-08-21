# Market website — trading intelligence dashboard

A read-only website over the Project Celeste marketplace: every hour a cron job
fetches the live market and appends an immutable snapshot to a SQLite database,
and the website presents that history as interactive charts and tables.

## Architecture

```
cron ── hourly ──> fetch --store ──> market.db (SQLite, append-only)
                                          │  read
                                          ▼
                              aoeo_market.web ──> browser (Chart.js dashboard)
```

- `aoeo_market/store.py` — the SQLite schema and the snapshot/analytics API.
  Two tables: `snapshots(id, captured_at)` and
  `listings(snapshot_id, transaction_id, …, seconds_till_expiry)`.
  Writing is append-only; every read runs on its own WAL connection, so the
  web server and the cron writer can run at the same time.
- `aoeo_market/web.py` — a stdlib-only HTTP server (`ThreadingHTTPServer`) that
  serves the dashboard page and a JSON API. No framework dependencies.
- `aoeo_market/static/` — the single-page dashboard (vanilla JS + Chart.js
  loaded from the jsDelivr CDN).

## Setup

```bash
# 1. snapshot the market once (writes market.db; add --local-ip <ip> if needed)
uv run python -m aoeo_market.cli fetch --local-ip <ip> --store --quiet

# 2. serve the dashboard
uv run python -m aoeo_market.web --db market.db --port 8000
# -> http://127.0.0.1:8000
```

### Cron (hourly snapshots)

Edit your crontab (`crontab -e`) and add one line, substituting the real paths:

```
0 * * * * cd /home/you/aoeo-market && /usr/bin/env AOEO_EMAIL=you@example.com AOEO_PASSWORD=secret .venv/bin/python -m aoeo_market.cli fetch --local-ip <ip> --store --quiet >> fetch.log 2>&1
```

- Runs at minute 0 of every hour; `--quiet` keeps the log to one line per run
  (`stored N listings -> market.db`).
- Credentials come from `AOEO_EMAIL` / `AOEO_PASSWORD` (or `--email` /
  `--password`, or an interactive prompt — not available under cron).
- `--store` without a path writes `market.db` in the working directory, the
  same default the web server reads.
- Prefer a cron user account dedicated to the poller: the backend may allow
  only one live session per account (see `docs/live-client.md`), so don't
  reuse the account you play on.

A systemd timer is an alternative (systemd ≥ 2.5x runs `%u` user units);
`OnCalendar=hourly` with `ExecStart=/home/you/aoeo-market/.venv/bin/python -m
aoeo_market.cli fetch --local-ip <ip> --store --quiet` in a user service is
equivalent.

## Views

| Tab | What it shows |
|---|---|
| **Overview** | KPI cards (active listings, distinct items, snapshot count, last snapshot), market supply over time, current price distribution (log-scale bins), listings by type and by rarity, and the biggest median-price movers between the last two snapshots. |
| **Listings** | Every active listing of the latest snapshot, with client-side search, type filter and sortable columns. Click an item to open its detail view. |
| **Best sellers** | Items ranked by **time-to-sale** (fastest first): how quickly their listings sell. Orderable by median/min/max time, sales count, rarity, current price and more; a bar chart shows the ten fastest. |
| **Not on sale** | Items seen in past snapshots that have **no active listing right now** — what you could list. Orderable by median price, rarity, level, times listed, last seen, min/max price (click the column headers). |
| **Recently removed** | Listings that vanished between the last two snapshots, classified like the observer: `EXPIRED` (timed out with < 1 day left) vs `REMOVED` (sold or withdrawn — indistinguishable). |
| **Item detail** | Full price history of one item: median line per snapshot overlaid with the individual listing price points, a historical price histogram, and the current listings. |

## JSON API

| Endpoint | Returns |
|---|---|
| `GET /api/overview` | snapshot stats, supply history, price histogram, type/rarity breakdown, top movers |
| `GET /api/listings?type=&q=&sort=&dir=` | active listings of the latest snapshot |
| `GET /api/item/<item_id>` | current listings + price history (`series`, `points`) of one item |
| `GET /api/not-on-sale?order=&dir=` | historical items with no active listing right now |
| `GET /api/best-sellers?order=&dir=&min_sales=` | items ranked by observed time-to-sale (fastest first by default) |
| `GET /api/recently-removed` | listings that vanished between the last two snapshots |

The API is the stable surface of the website; the frontend is a consumer of it.

## Data notes

- **Rarity** is a best-effort heuristic: item ids encode it as a suffix
  letter (`_C/_U/_R/_E/_L`, plus the `_LEG` legendary marker), e.g.
  `Achichorius_E_IV` (epic advisor) or `CreateMetalWorkingWarpaint_R`.
  Materials and consumables carry no letter and show as "unknown". See
  `aoeo_market.market.rarity_of`.
- All timestamps are Unix seconds in UTC; the dashboard renders them in the
  browser's local timezone.
- With one snapshot only, the "not on sale" and "recently removed" views are
  empty and the movers table says so — everything fills in from the second
  snapshot onwards.
- **Time-to-sale** (best sellers) is the observed lifetime of a listing: from
  the first snapshot it appears in to the first snapshot it is absent from.
  It counts only listings that vanished with ≥1 day left on their countdown
  (sold or withdrawn — indistinguishable, like everywhere else); EXPIRED
  listings are counted separately.  Listings already present in the very first
  snapshot are left-censored (their true listing time is unknown), so they
  count toward `sales` but not toward the time stats — the view needs a few
  hourly snapshots before it fills in, and times are accurate to within one
  poll interval.
- The database only grows: `fetch --store` never deletes. To start over,
  stop the cron job, move `market.db` (and `-wal`/`-shm` sidecars) aside, and
  run `fetch --store` again.
