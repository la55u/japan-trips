# japan-trips

Scans Google Flights (via [fast-flights](https://github.com/AWeirdDev/fast-flights)) for
cheap trips from Budapest/Vienna to Tokyo/Osaka and renders the best deals to a static
HTML page. Built for a 12–16 day trip between 2027-03-22 and 2027-05-31, but every knob
is configurable.

## What it does

For every departure day in the window and every 12–16 day difference between outbound
and return departure dates, it prices:

- **Round trips** — BUD or VIE ↔ Tokyo (TYO) or Osaka (OSA), same city both ways.
- **Open jaw** — fly into one Japanese city, fly home from the other (one shinkansen ride
  between cities instead of backtracking). Google prices it as the sum of two one-ways,
  with the outbound origin and return destination freely mixable (e.g. out of Vienna,
  home to Budapest); Skyscanner is additionally searched for **true multi-city
  open-jaw fares** (both legs as one OTA booking).

Each itinerary's **total per-person cost** = airfare + estimated transfers:

| Transfer | Cost (config) | When |
|---|---|---|
| Shinkansen Tokyo↔Osaka (reserved seat) | €90 | always, one direction |
| Domestic flight Tokyo↔Osaka (LCC) | €65 | round trips only (fly back to arrival city) |
| FlixBus Budapest↔Vienna | €15/direction | every leg involving Vienna |

Everything lives in `config.toml`. Google and Skyscanner are queried for **2 adults**;
party totals are divided to show per-person prices. Google requests economy, one
checked bag, and max 2 stops by default. OTA rows are ranked independently of Google:
the table shows both the raw OTA fare and the fare plus a conservative configured bag
estimate, because Skyscanner does not provide a verified bag-inclusive quote.

Skyscanner is spot-checked through an anti-fingerprint browser at most every 2 hours in
three tiers with per-combination refresh times: **hot** (refresh current winners),
**neighbours** (±1–3 days around stored RT deals that beat Google), and **exploration**
(unseen pairs first, then oldest checked, balanced across routes and trip durations —
separate quotas for round trips and multi-city open-jaw searches, a full RT sweep in
under a week). Open-jaw searches are fast-path only (no SPA deep link); occasional
PerimeterX 403s right after a session bootstrap are handled by one fresh-session
retry. Rows older than a day are labelled *indicative*; the stats strip shows
Skyscanner coverage (pairs checked, oldest check, estimated full-sweep time).
Optionally, a Travelpayouts Data API token (`[travelpayouts]`) enables cached calendar
prices to prioritize which exploration pairs get checked — they are never shown or
ranked.

## Setup

```bash
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python -m camoufox fetch
```

## Usage

```bash
./venv/bin/python flight_search.py              # full scan into flights_local.db + index_local.html
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
| **CI (GitHub Actions, hourly)** | `flights.db` | `index.html` → GitHub Pages | yes |
| **Local runs (default)** | `flights_local.db` | `index_local.html` | no (gitignored) |

Local runs never touch `flights.db`/`index.html`, and the CI workflow explicitly
passes `--db flights.db --out index.html`, so neither side can clobber the other.
Local history and the site's history diverge — that's by design.

If you deliberately want a local scan to feed the live site, opt in with
`--db flights.db --out index.html`, then `git pull --rebase` immediately after and
push promptly (the DB is a binary file and cannot merge).

## Output (`index.html`)

- Files: `index.html` (generated) + `results.css` (static styles, committed) —
  the only HTML file on the site.
- Summary cards (top of page, compact): cheapest overall / round trip / open jaw / OTA
  (Skyscanner).
- Filters sit directly above the table: EUR/HUF toggle, departure-city selector, source
  filter (Google / OTA), max single-leg duration (default 24 h — raise it to surface
  cheap-but-slow OTA itineraries), and minimum/maximum trip-day range. Top-40 rows,
  sortable by any column (▲/▼ indicator); the report retains enough candidates to show
  the best 40 for every configured city/day combination.
- Legs show route (hover airport codes for full names), local departure/arrival times,
  `(+n)` = arrival n days after departure, total duration in hours incl. layovers, stops,
  airlines, and Google Flights links to verify/book.
- Round-trip return legs show the actual selected pairing when Google's selection RPC
  exposes it, otherwise a reference one-way on the same date marked ≈.
- Δ column shows price movement vs the previous run.
- Click/tap any row for a detail card (native HTML dialog): full airport names, times,
  durations, itemized cost breakdown, and Google Flights links.
- Two Chart.js charts: per-itinerary price history and cheapest-overall-per-run trend.

## Caching & history (SQLite)

Every query result is cached with a TTL (see `[cache]`) and successful prices are logged
to bounded history tables. Failed requests are retried three times and update attempt
metadata without deleting the last-good fare. Confirmed-empty results use the normal
TTL; parser, transport, and unrecognized-response failures remain due. Fares beyond
`max_rank_age_hours` are not published.

## Known limitations

- **Round-trip page payloads contain outbound details only.** A second selection RPC is
  required for the paired return and can break if Google's private schema changes.
- **Google open jaw = sum of two one-ways**, which slightly overestimates a real
  multi-city ticket. fast-flights' parser cannot handle Google's multi-city responses.
  Use the Google Flights links on each row to verify the true multi-city price;
  Skyscanner multi-city (Open jaw · OTA) rows price both legs as one OTA booking.
- Transfer costs are static estimates (`[costs]`); March–May 2027 shinkansen/domestic
  fares aren't bookable yet.
- “12–16 days” is a departure-date difference, not guaranteed nights in Japan.
- OTA baggage is an estimate; self-transfer/protection and source freshness are shown,
  but final checkout price and agent reliability still require verification.
- Scraping Google Flights is unofficial and may break if the page structure changes.

## Automation (GitHub Actions)

The `.github/workflows/scan.yml` workflow targets hourly execution, although GitHub may
delay or skip scheduled jobs. Each run fetches the 450 oldest stale queries
(`--limit 450 --db flights.db`, cache TTL 4 h), targeting a full refresh in roughly
four runs. The page reports actual freshness and deferred stale work instead of
claiming a guaranteed four-hour age. Ruff and unit tests run in a separate
`verify` workflow (push/PR); the scan workflow runs unconditionally so a failing
test suite never halts the hourly scans. After
scanning, the workflow commits `index.html` and `flights.db` back to `main`, and
GitHub Pages redeploys automatically. `flights.db` is committed so price history and
deltas accumulate across runs; it is written **only by CI** (see "Local vs CI data").
Manual runs: *Actions → scan → Run workflow*.

### Local watchdog

`watchdog.py` compensates for dropped GitHub cron events. It checks the latest workflow
runs and dispatches `scan.yml` when the latest success is older than 75 minutes. It
does nothing while a run is active and waits 30 minutes after any recent attempt before
retrying, preventing dispatch storms during failures.

It uses the authenticated GitHub CLI and can be checked without dispatching anything:

```bash
gh auth status
./watchdog.py --dry-run
```

The repository includes a systemd user service and timer, so no cron package is needed.
Link and enable them with:

```bash
systemctl --user link "$PWD/systemd/japan-trips-watchdog.service"
systemctl --user link "$PWD/systemd/japan-trips-watchdog.timer"
systemctl --user daemon-reload
systemctl --user enable --now japan-trips-watchdog.timer
```

Enable user lingering so the timer continues after logout:

```bash
loginctl enable-linger "$USER"
```

Inspect it with `systemctl --user list-timers japan-trips-watchdog.timer` and
`journalctl --user -u japan-trips-watchdog.service`. `Persistent=true` runs a missed
check after the user manager starts again, although no local timer can run while the
machine is powered off.

To remove the installed watchdog permanently from this machine:

```bash
systemctl --user disable --now japan-trips-watchdog.timer
rm -f "$HOME/.config/systemd/user/japan-trips-watchdog.timer"
rm -f "$HOME/.config/systemd/user/japan-trips-watchdog.service"
systemctl --user daemon-reload
systemctl --user reset-failed
```

The first command removes the `timers.target.wants` enablement link; the two `rm`
commands remove the unit links created by `systemctl --user link`. If lingering was
enabled only for this watchdog and no other user services need to run after logout,
disable it separately:

```bash
loginctl disable-linger "$USER"
```

Do not disable lingering if another user service depends on it. Confirm removal with:

```bash
systemctl --user is-active japan-trips-watchdog.timer
systemctl --user is-enabled japan-trips-watchdog.timer
```

Both checks should report `inactive`, `disabled`, or `not-found`. These commands remove
only the local installation; they intentionally leave `watchdog.py`, its tests, and the
version-controlled unit files in the repository. Removing the feature from the
repository as well requires deleting those files, removing them from the CI Ruff
command, and deleting this documentation. The watchdog does not create a dedicated log
file: historical output remains in the shared user journal and expires according to
the machine's normal journald retention policy.

Useful overrides are `--max-age-minutes`, `--retry-cooldown-minutes`, `--repo`,
`--workflow`, and `--ref`. A lock file in `/tmp` prevents overlapping watchdog
processes. The watchdog only dispatches GitHub Actions; it never modifies the local
database or report.

## Files

- `flight_search.py` — search, ranking, HTML generation (single file).
- `test_flight_search.py` — ranking, failure-handling, DB, and source regression tests.
- `watchdog.py`, `test_watchdog.py` — local watchdog and its decision tests.
- `systemd/` — user service and 10-minute timer for the watchdog.
- `config.toml` — all settings.
- `flights.db` — CI-owned SQLite cache + price history (committed).
- `index.html` — generated report deployed via GitHub Pages.
- `flights_local.db`, `index_local.html` — local-run outputs (gitignored).

## GitHub Pages

The report is published at https://la55u.github.io/japan-trips/ (index.html is
generated directly by the scan; `results.css` carries the styles and is the only
other web-served file). The page is refreshed
automatically by the scheduled workflow; after a deliberate local scan against the
repo DB, push as described in "Local vs CI data":

```bash
git add index.html flights.db && git commit -m "scan: update results" && git push
```
