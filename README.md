# japan-trips

Scans Google Flights (via [fast-flights](https://github.com/AWeirdDev/fast-flights)) for
cheap trips from Budapest/Vienna to Tokyo/Osaka and renders the best deals to a static
HTML page. Built for a 12–16 day trip between 2027-03-25 and 2027-05-31, but every knob
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
./venv/bin/python flight_search.py             # full scan (~1600 queries, ~1h at 2 workers)
./venv/bin/python flight_search.py --limit 50  # partial run (first 50 uncached queries)
./venv/bin/python flight_search.py --rank-only # rebuild ranking + results.html from cache
./venv/bin/python flight_search.py --force     # ignore cache TTL, refetch everything
```

Useful flags: `--workers N`, `--step N` (scan every Nth day), `--top N`, `--out FILE`,
`--config FILE`.

Run it a few times a day; results.html and the price-history charts accumulate across
runs, with ▲/▼ deltas vs the previous run.

## Output (`results.html`)

- Summary cards: cheapest overall / round trip / open jaw.
- Top-40 table, sortable by any column (▲/▼ indicator), EUR/HUF toggle.
- Legs show route (hover airport codes for full names), local departure/arrival times,
  `(+n)` = arrival n days after departure, total duration in hours incl. layovers, stops,
  airlines, and Google Flights links to verify/book.
- Δ column shows price movement vs the previous run.
- Two Chart.js charts: per-itinerary price history and cheapest-overall-per-run trend.

## Caching & history (`flights.db`, SQLite)

Every query result is cached with a TTL (default 4 h, see `[cache]`) and every price is
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

## Files

- `flight_search.py` — search, ranking, HTML generation (single file).
- `config.toml` — all settings.
- `flights.db` — SQLite cache + price history (created at first run, gitignored).
- `results.html` — generated report.

## GitHub Pages

The report is published at https://la55u.github.io/japan-trips/ (served from the
`main` branch root; `index.html` redirects to `results.html`). To update the live
page after a run:

```bash
git add results.html && git commit -m "update results" && git push
```