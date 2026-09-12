# japan-trips

Scans Google Flights (via [fast-flights](https://github.com/AWeirdDev/fast-flights)) for
cheap trips from Budapest/Vienna to Tokyo/Osaka and renders the best deals to a static
HTML page. Built for a 12–16 day trip between 2027-03-22 and 2027-05-31, but every knob
is configurable.

## What it does

For every departure day in the window and every trip length (12–16 nights), it prices:

- **Round trips** — BUD or VIE ↔ Tokyo (TYO) or Osaka (OSA), same city both ways.
- **Open jaw** — fly into one Japanese city, fly home from the other (one shinkansen ride
  between cities instead of backtracking). Priced as the sum of two one-ways, with the
  outbound origin and return destination freely mixable (e.g. out of Vienna, home to
  Budapest).

Each itinerary's **total per-person cost** = airfare + estimated transfers:

| Transfer | Cost (config) | When |
|---|---|---|
| Shinkansen Tokyo↔Osaka (reserved seat) | €90 | always, one direction |
| Domestic flight Tokyo↔Osaka (LCC) | €65 | round trips only (fly back to arrival city) |
| FlixBus Budapest↔Vienna | €15/direction | every leg involving Vienna |

Everything lives in `config.toml`. Prices are **per person** (queried as 1 adult,
economy, checked bag included, max 2 stops by default).

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install fast-flights primp selectolax typing_extensions
```

## Usage

```bash
./venv/bin/python flight_search.py              # full scan into flights_local.db + results_local.html
./venv/bin/python flight_search.py --limit 50   # partial run (first 50 stale queries)
./venv/bin/python flight_search.py --rank-only  # rebuild ranking + HTML from local cache
./venv/bin/python flight_search.py --force      # ignore cache TTL, refetch everything
```

Useful flags: `--workers N`, `--step N` (scan every Nth day), `--top N`, `--db FILE`,
`--out FILE`, `--config FILE`, `--verbose` (debug logging).

## Local vs CI data (important)

There are two independent data stores so local runs and scheduled CI scans can never
overwrite each other's price history:

| | DB | Report | Committed to git |
|---|---|---|---|
| **CI (GitHub Actions, 12×/day)** | `flights.db` | `results.html` → GitHub Pages | yes |
| **Local runs (default)** | `flights_local.db` | `results_local.html` | no (gitignored) |

Local runs never touch `flights.db`/`results.html`, and the CI workflow explicitly
passes `--db flights.db --out results.html`, so neither side can clobber the other.
Local history and the site's history diverge — that's by design.

If you deliberately want a local scan to feed the live site, opt in with
`--db flights.db --out results.html`, then `git pull --rebase` immediately after and
push promptly (the DB is a binary file and cannot merge).

## Output (`results.html`)

- Summary cards: cheapest overall / round trip / open jaw.
- Top-40 table, sortable by any column (▲/▼ indicator), EUR/HUF toggle.
- Legs show route (hover airport codes for full names), local departure/arrival times,
  `(+n)` = arrival n days after departure, total duration in hours incl. layovers, stops,
  airlines, and Google Flights links to verify/book.
- Round-trip return legs show a **reference** (the best one-way on the same date, marked
  ≈): Google's API does not expose return-leg details for round-trip searches — verify
  the actual return via the Google Flights link.
- Δ column shows price movement vs the previous run.
- Click/tap any row for a detail card (native HTML dialog): full airport names, times,
  durations, itemized cost breakdown, and Google Flights links.
- Two Chart.js charts: per-itinerary price history and cheapest-overall-per-run trend.

## Caching & history (SQLite)

Every query result is cached with a TTL (see `[cache]`) and every price is
logged to history tables, so repeated runs are fast and deltas/graphs build over time.
Failed or empty queries are retried (3×, with backoff) and refetched on the next run.

## Known limitations

- **Round-trip payloads from Google only contain outbound leg details** (times, route,
  duration). The return leg shows price only; the table marks it explicitly.
- **Open jaw = sum of two one-ways**, which slightly overestimates a real multi-city
  ticket. fast-flights' parser cannot handle Google's multi-city responses. Use the
  Google Flights links on each row to verify the true multi-city price.
- Transfer costs are static estimates (`[costs]`); March–May 2027 shinkansen/domestic
  fares aren't bookable yet.
- Ranking uses the 1-adult fare; a specific fare may not be available for 2 seats.
- Scraping Google Flights is unofficial and may break if the page structure changes.

## Automation (GitHub Actions)

The `.github/workflows/scan.yml` workflow runs every 2 hours (12×/day; GitHub may
delay scheduled runs by some minutes). Each run fetches the next ~350 stale queries
(`--limit 350 --db flights.db`, cache TTL 12 h), so the whole scan window is refreshed twice a day, so data is never staler than ~12 h — the rate per second stays unchanged,
roughly once a day while the page updates every 2 hours. After
scanning, the workflow commits `results.html` and `flights.db` back to `main`, and
GitHub Pages redeploys automatically. `flights.db` is committed so price history and
deltas accumulate across runs; it is written **only by CI** (see "Local vs CI data").
Manual runs: *Actions → scan → Run workflow*.

## Files

- `flight_search.py` — search, ranking, HTML generation (single file).
- `config.toml` — all settings.
- `flights.db` — CI-owned SQLite cache + price history (committed).
- `results.html` — generated report deployed via GitHub Pages.
- `flights_local.db`, `results_local.html` — local-run outputs (gitignored).

## GitHub Pages

The report is published at https://la55u.github.io/japan-trips/ (served from the
`main` branch root; `index.html` redirects to `results.html`). The page is refreshed
automatically by the scheduled workflow; after a deliberate local scan against the
repo DB, push as described in "Local vs CI data":

```bash
git add results.html flights.db && git commit -m "scan: update results" && git push
```