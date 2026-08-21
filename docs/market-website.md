# Market website — trading intelligence dashboard

A read-only website over the Project Celeste marketplace: every hour a cron job
fetches the live market and posts an immutable snapshot to the website's API,
and the website presents that history as interactive charts and tables.

## Architecture

```
cron ── hourly ──> fetch --store <url> ── POST /api/snapshot ──> aoeo_market.web
                                                                      │
                                                                      ▼
                                                              market.db (DuckDB, append-only)
                                                                      │
                                                                      ▼
                                                            browser (Chart.js dashboard)
```

The **web server is the single owner of the database**: the fetcher never
opens the file, it only POSTs JSON. That keeps DuckDB's one-writer model
trivially satisfied and maps directly onto Kubernetes — the web app runs as a
**StatefulSet** pod owning the database volume, and `fetch --store <url>`
runs as a **CronJob** that only needs network access to the service.

- `aoeo_market/store.py` — the DuckDB schema and the snapshot/analytics API.
  Two tables: `snapshots(id, captured_at)` and
  `listings(snapshot_id, transaction_id, …, seconds_till_expiry)`.
  DuckDB is a single-file, in-process OLAP engine (no server to deploy): the
  columnar engine keeps the dashboards fast as the history grows, and its SQL
  surface (medians, percentiles, ILIKE) matches the analytics queries.
- `aoeo_market/web.py` — a stdlib HTTP server (`ThreadingHTTPServer`) that
  serves the dashboard page and a JSON API, including the
  `POST /api/snapshot` write endpoint (validated, serialized by a write
  lock).  The only third-party dependency of the whole site is `duckdb`.
- `aoeo_market/cli.py` — `fetch --store` accepts either the web URL (POST)
  or a local DuckDB file path (development fallback; the file form is not
  meant for production, where only the web pod owns the volume).
- `aoeo_market/static/` — the single-page dashboard (vanilla JS + Chart.js
  loaded from the jsDelivr CDN).

## Setup

```bash
# 1. serve the dashboard (sole owner of market.db)
uv run python -m aoeo_market.web --db market.db --port 8000
# -> http://127.0.0.1:8000

# 2. snapshot the market once through its API (add --local-ip <ip> if needed)
uv run python -m aoeo_market.cli fetch --local-ip <ip> --store http://127.0.0.1:8000 --quiet
```

### Cron (hourly snapshots)

Edit your crontab (`crontab -e`) and add one line, substituting the real paths:

```
0 * * * * cd /home/you/aoeo-market && /usr/bin/env AOEO_EMAIL=you@example.com AOEO_PASSWORD=secret .venv/bin/python -m aoeo_market.cli fetch --local-ip <ip> --store http://127.0.0.1:8000 --quiet >> fetch.log 2>&1
```

- Runs at minute 0 of every hour; `--quiet` keeps the log to one line per run
  (`stored N listings -> http://… (snapshot id …)`).
- The fetcher exits non-zero when the POST fails, so cron reports problems.
- Credentials come from `AOEO_EMAIL` / `AOEO_PASSWORD` (or `--email` /
  `--password`, or an interactive prompt — not available under cron).
- `--store <path>` (no `http`) still writes a local DuckDB file directly —
  handy for development, but it must not run against the web server's file.
- Prefer a cron user account dedicated to the poller: the backend may allow
  only one live session per account (see `docs/live-client.md`), so don't
  reuse the account you play on.

A systemd timer is an alternative (systemd ≥ 2.5x runs `%u` user units);
`OnCalendar=hourly` with `ExecStart=/home/you/aoeo-market/.venv/bin/python -m
aoeo_market.cli fetch --local-ip <ip> --store http://127.0.0.1:8000 --quiet`
in a user service is equivalent.

### Kubernetes (same namespace)

The intended deployment puts both components in one namespace:

- **Web app** — a StatefulSet with **exactly one replica** (DuckDB allows one
  writer per file), `--host 0.0.0.0`, the `market.db` file on a PersistentVolume,
  and `GET /healthz` as the liveness/readiness probe (it does not touch the
  database).  An **init container** runs
  `python -m aoeo_market.cli init-db --db /data/market.db` first, so the pod
  always starts with a ready, schema-complete database on the volume
  (idempotent — safe on every restart).
- **Fetcher** — a CronJob (`schedule: "0 * * * *"`) running
  `python -m aoeo_market.cli fetch --store http://<service>:8000 --quiet`
  with the credentials in a Secret (`AOEO_EMAIL` / `AOEO_PASSWORD`).  Same
  namespace means the short Service DNS name works.
- **Trust boundary** — the `POST /api/snapshot` endpoint is unauthenticated
  and served on the same port as the dashboard, so the namespace is the
  security boundary: keep the Service cluster-internal (view the dashboard
  via `kubectl port-forward` or a VPN), or put basic auth in front of it at
  the ingress.  A NetworkPolicy restricting the web pod's ingress to the
  fetcher's pods is the cheap extra hardening.

## Views

| Tab | What it shows |
|---|---|
| **Overview** | KPI cards (active listings, distinct items, snapshot count, last snapshot), market supply over time, current price distribution (log-scale bins), listings by type and by rarity, and the biggest median-price movers between the last two snapshots. |
| **Listings** | Every active listing of the latest snapshot, with client-side search, type filter and sortable columns. Click an item to open its detail view. |
| **Best sellers** | Items ranked by **time-to-sale** (fastest first): how quickly their listings sell. Orderable by median/min/max time, sales count, rarity, current price and more; a bar chart shows the ten fastest. |
| **Best value** | Items ranked by **value for their rarity**: how cheap an item trades relative to the typical price of its rarity tier (a 2× ratio means half the typical price). Orderable by ratio, price, "cheaper than %" percentile and more; a bar chart shows the ten best. |
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
| `GET /api/best-value?order=&dir=&include_unrated=` | items ranked by value for their rarity (cheapest relative to their tier first) |
| `GET /api/recently-removed` | listings that vanished between the last two snapshots |
| `POST /api/snapshot` | append one snapshot — body `{"listings": [<Listing.to_dict()>…], "captured_at": <unix seconds, optional>}` → `{"snapshot_id": id, "listings": n}` |

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
- **Value for rarity** (best value) compares each item against its own rarity
  tier: the tier's reference price is the median of its items' historical
  median prices (each item counts once), and the value ratio is
  reference ÷ price (current median while the item is on sale, historical
  median otherwise), so 2× means half the typical price of that rarity. The
  "cheaper than" column is the item's price percentile within its tier.
  Untagged items (materials and most consumables) are excluded by default —
  the `include_unrated` API flag adds them as their own tier.
- **Price per unit**: the listing's `ItemPrice` is the *total for the whole
  stack* (`ItemCount`), and bulk discounts exist, so every analytics view
  (price distributions, item history, best value, not-on-sale stats, movers)
  normalizes to `ItemPrice / ItemCount`. The listings and item tables show
  both the unit price and, where relevant, the stack total.
- The database only grows: `fetch --store` never deletes. To start over,
  stop the cron job and the web server, move `market.db` (and its
  `market.db.wal` sidecar, if present) aside, and run `fetch --store` again.
