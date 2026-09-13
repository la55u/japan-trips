# japan-trips — flight deal scanner (BUD/VIE ⇄ Tokyo/Osaka)

## Goal

Scan Google Flights (and Skyscanner as a secondary source) for cheap round-trip and
open-jaw flights from Budapest (BUD) and Vienna (VIE) to Tokyo (TYO) and Osaka (OSA),
for a 12–16 day trip between 2027-03-22 and 2027-05-31, for 2 people (prices per person,
1-adult queries, economy, 1 checked bag, max 2 stops). Results are ranked by total
per-person cost (airfare + estimated in-Japan transfers + FlixBus to Vienna if used) and
published as a static HTML page via GitHub Pages, refreshed hourly by GitHub Actions.

**AGENTS.md must be kept brief; this file (CONTEXT.md) is the agent knowledge base and
MUST be updated whenever behavior, schema, config, or pipeline changes.**

## Repository layout

- `flight_search.py` — the entire pipeline in one file (~1900 lines). No other modules.
- `config.toml` — all knobs (window, costs, TTL, cadence, skyscanner settings).
- `flights.db` — CI-owned SQLite (committed). Local runs use `flights_local.db`.
- `results.html` — generated report, deployed to GitHub Pages (CI-owned).
- `index.html` — redirect to results.html.
- `.github/workflows/scan.yml` — hourly scan job.
- `requirements.txt` — pinned deps.
- `README.md` — human-facing docs.

## Environment

- Python 3.14 venv at `./venv` (NOTE: venv bin scripts have a stale shebang pointing to
  the pre-rename dir `$HOME/Work/japan`; always invoke via
  `./venv/bin/python -m pip ...` or `./venv/bin/python script.py`).
- Repo dir was renamed `$HOME/Work/japan` → `$HOME/Work/japan-trips`.
- GitHub: repo `la55u/japan-trips`, Pages at
  https://la55u.github.io/japan-trips/ (branch `main`, root, `index.html` redirects).
- Local test DB `flights_local.db` was seeded from `flights.db` once; it diverges.

## Data ownership (critical invariant)

| Store | Writer | Committed |
|---|---|---|
| `flights.db`, `results.html` | CI ONLY (workflow passes `--db flights.db --out results.html`) | yes |
| `flights_local.db`, `results_local.html` | local runs (gitignored defaults) | never |

Never commit local-run artifacts. If scanning against `flights.db` locally: `git pull
--rebase` first, push promptly (SQLite cannot merge).

## Google Flights pipeline (primary source)

Query building uses `fast_flights.create_query` (protobuf builder only — its parser is
NOT used; it crashes on fare-less itineraries). All HTTP via `primp` Client
(`impersonate="chrome_145"`) + hardcoded `SOCS` cookie to bypass the EU consent wall.

Fetch flow per query (`fetch_with_retry`):
1. GET `https://www.google.com/travel/flights` with the tfs params → page HTML.
2. `parse_payload`: extract the `ds:1` script, split `data:` JSON.
   - Normal variant: `payload[3][0]` = itineraries; each entry parsed by
     `_itinerary_from_entry(k)` where `k[0]` = [type, airlines, segments] and
     `k[1][0]` = [None, price]; blob at `k[1][1]` (used for the Select-flight RPC).
     Segment indices: s[3]=from code, s[4]=from name, s[5]=to name, s[6]=to code,
     s[8]=dep time, s[10]=arr time, s[11]=duration min, s[20]/s[21]=dep/arr date
     tuples, s[22]=[airline_code, flight_number, ...].
   - Null variant (`payload[3]` None): page embeds NO itineraries but `payload[6]`
     holds nearby-date suggestions (`_suggestions`, structure `payload[6][0][4][0]`,
     each `[d1, d2, [[None, price], blob], flags]`). In that case
     `fetch_rpc_itins` replays the browser's `GetShoppingResults` RPC:
     URL `RPC_PATH` + f.sid (`FdrFJe` in HTML) + bl (`cfb2h`) + `&curr=EUR`
     (mandatory — otherwise prices come back in HUF!). Request inner JSON =
     `[[None,None,None,<token>], <ds:1 request template from the page>, 0,0,0,1]`
     where token matches regex `[A-Za-z0-9_-]{6,}-{6,}[A-Za-z0-9_-]{6,}`
     (dash count varies!).
   - `errorHasStatus: true` suffix is NOT an exception anymore — same handling as
     no-results variant (harvest suggestions).
   - Retry: exceptions retry 3× with backoff; no-results never retries (deterministic).
3. Round-trips: after outbound parse, `fetch_return_legs` replays the "Select flight"
   RPC (`inner[0]=[None,<blob of chosen outbound>]`, legs array found structurally via
   `_find_legs_index`, outbound leg[0] rebuilt with segment list at index 8 — the
   segment list goes AFTER a null slot, see `fetch_return_legs`). Response parsed the
   same way; cheapest paired return stored in detail["ret"].

Data normalization: prices are TOTAL for the queried passengers; we query 1 adult →
per-person. Currency forced EUR.

## Ranking (build_itineraries)

- RT rows from `price_cache` (kind='RT').
- OJ (open jaw): built from OW one-way rows — outbound `origin→in_city` on d1, return
  `out_city→home` on d2; `in_city/out_city` ∈ {(TYO,OSA),(OSA,TYO)}; home ∈ origins
  freely mixed. (Historical bug: used Japan city as return destination — fixed.)
- SS rows: `_ss_itineraries` synthesizes itineraries from `skyscanner_prices` deals
  (kind='SS'), only deals cheaper than Google's RT for the same pair; airfare=EUR,
  same transfer model. Agent name + Skyscanner page link carried on the row.
- Transfers (config `[costs]`): OJ = shinkansen 90; RT = shinkansen + domestic 65;
  FlixBus 15/direction for every leg involving VIE (out_origin or ret_dest).
- Sort by total = airfare + transfers.

## Skyscanner pipeline (secondary source)

- Runs inside each hourly job when due: state key `skyscanner_last_run` older than
  `min_age_hours` (12h). Fully isolated: per-combo try/except + whole-function
  try/except — a Skyscanner failure can never affect the Google scan or the render.
- Combo selection (`run_skyscanner_if_due`): origin-balanced Google-top (2 cheapest
  distinct pairs per origin) + discovery slots:
  1. winner-adjacent: dep/ret dates shifted +1..+3 days around stored pairs where
     Skyscanner beat Google by >100 EUR;
  2. gap-priority rotation: date pairs ranked by `GoogleRT − OJ` (largest first,
     rotating cursor `skyscanner_discover_cursor`), gap > 50 EUR, not already checked.
  The RT−OJ gap predicts where OTA self-transfer deals undercut Google; it does NOT
  catch everything (e.g. BUD→TYO Apr 1 where OTA fares are cheaper than any Google
  data) — this is a known, accepted limitation.
- Fetch (`skyscanner_spotcheck`): camoufox (anti-fingerprint Firefox, humanize, geoip,
  locale hu-HU) loads `skyscanner.hu` search URLs (`_ss_url`, dates as YYMMDD, city
  codes `bud/vie` + `tyoa/osaa`). PerimeterX challenge = `#px-captcha` press-and-hold
  solved by `_solve_px` (mouse down 11s with tremor, up, re-navigate). Data captured by
  listening for XHR `web-unified-search` status 200 body >100KB; largest body parsed by
  `_parse_skyscanner_payload`: `itineraries.results[]` (price.raw/formatted,
  isSelfTransfer, isProtectedSelfTransfer, pricingOptions→agents + deep link,
  legs with origin/destination ids, stopCount, departure/arrival, durationInMinutes,
  carriers). Keeps top `top_deals` (10) cheapest per combo, adds `eur` conversion
  (HUF via `huf_per_eur`, GBP via `gbp_per_eur`).
- Storage: `skyscanner_prices` table (key origin|dest|d1|d2, deals_json, fetched_at).
- CI validated: works from GitHub datacenter IPs (6-10 combos, ~3 min, no challenges
  needed so far; challenges would be solved automatically).
- Skyscanner links on the page point to the Skyscanner search page (`_ss_url`), NOT the
  agent deep link (agent deeplinks are stored but not rendered).

## DB schema (flights.db / flights_local.db)

- `price_cache(key PK, kind RT|OW, origin, dest, d1, d2 NULL, price NULL, n_results,
  detail JSON, prev_price, prev_fetched_at, fetched_at)` — one row per query. Key =
  `kind|max_stops|checked_bags|origin|dest|d1|d2` (no window in key). detail JSON for
  priced rows = best itinerary dict (price, airlines, route, stops, dep, arr, plus,
  dur_h, airports map, blob, legs, n_results); for empty rows:
  `{"no_exact_results": true, "suggestions": [...]}`; errors: `{"error": "..."}`.
  TTL: rows with price are refetched when older than ttl_hours; no_exact_results rows
  also respect TTL; other NULL rows (errors) are always refetched.
- `price_history(key, price, fetched_at)` — appended per successful fetch (not used by
  the page yet).
- `itinerary_history(run_ts, itin_key, kind, label, airfare, transfers, total)` — top-N
  snapshot per run; feeds deltas (prev run) and both charts.
- `runs(run_ts PK, fetched, cached, failed, note)`.
- `state(key PK, value)` — `scan_cursor` (Google rotating offset), `skyscanner_last_run`,
  `skyscanner_discover_cursor`.
- `skyscanner_prices(key PK = origin|dest|d1|d2, origin, dest, d1, d2, total_results,
  deals_json, fetched_at)` — deals_json = list of {price_raw, price_fmt, eur,
  self_transfer, protected, agents, legs[{from,to,stops,dep,arr,dur_min,carriers}],
  link(agent deeplink, stored not rendered)}.

## HTML page (results.html)

Template string `TEMPLATE` in flight_search.py, placeholders `__*__` replaced in
render_html. Single merged ranking table (RT/OJ/SS mixed, sorted by total):
columns # / Type (badge; SS rows add "via <agent>") / Route / Outbound / Return / Days /
Outbound leg / Return leg (RT: real paired return when available, else ≈ reference from
best one-way; SS: legs from Skyscanner data) / Airfare / Transfers / Total / Δ (vs
previous run via itinerary_history) / Links (last column: ALL sources — Google GF link,
+ OJ second GF link, + Skyscanner page link for SS rows).
Dialog (native `<dialog>`): full row details — airport names, times, durations
(color-coded ≤20h green / ≤24h amber / >24h red — same in table), itemized costs one
per line, all booking links. EUR/HUF toggle (huf_per_eur), sortable headers with ▲▼
indicator (default sort: Total asc). Stats strip: prices tracked (by kind), date pairs
checked/planned, newest/stalest cache age, history points, runs, this-run counts.
Charts (Chart.js CDN): per-itinerary totals over runs (top history_top_n), cheapest
overall per run.

## CI workflow (scan.yml)

Hourly at :23 (off-peak — top-of-hour crons get skipped by GitHub's scheduler; observed
overnight blackout with `0 * * * *`). Steps: checkout@v7 → setup-python@v7 (3.14) →
pip install -r requirements.txt → `python -m camoufox fetch` → `python flight_search.py
--limit 450 --db flights.db --out results.html` → commit `results.html` + `flights.db`
as github-actions[bot] with race-proof commit step (pull --rebase; on conflict reset to
remote — lost rows are refetched next run) → push (Pages redeploys automatically).
Concurrency group `scan` (serial). timeout-minutes 60. Rate config: TTL 4h, 450/run
hourly → full window (1708 queries) refreshed every ~4h; per-second rate ~2 q/s
(unchanged — rate, not volume, is the blocking risk). Backoff: ≥5 consecutive failures
→ pause 60s×min(n-4,5). `workflow_dispatch` for manual runs.

## Google blocking model (empirical)

Fine at ~2 q/s and ~10k queries/day: no captchas observed. Pages vary: normal ds:1,
null+suggestions, null+errorHasStatus — all handled. Google RT payloads contain ONLY
outbound segments (return arrives only via Select-flight RPC). Google does NOT price
OTA self-transfer combos — that gap is Skyscanner's value-add.

## Known limitations

- RT return leg details: real via Select-flight RPC for newly scanned rows; older rows
  fall back to ≈ reference (best one-way same date).
- OJ price = sum of two one-ways (slight overestimate vs true multi-city ticket).
- SS prices are OTA fares: separate tickets/self-transfer, bag fees often extra, agent
  middleman — shown with badge, never mixed into Google ranking semantics.
- SS EUR conversion: HUF via huf_per_eur, GBP via gbp_per_eur (approximate, config).
- PerimeterX: camoufox + press-and-hold has passed consistently (local + CI). Volume
  kept low (≤10 searches per 12h, 5–10s gaps). If PX escalates: volume down or
  residential proxy (camoufox supports `proxy=`).
- Month-grid mining ("cheapest month" survey, one request ≈ 60 date pairs) was
  investigated: the month page bounces direct entries to the homepage (needs search
  flow context) — deferred.

## Conventions & gotchas for agents

- NEVER edit site-packages venv content as a fix; vendor code into the repo instead
  (fast_flights parser was replaced for this reason).
- `gh run view --log` shows nothing for in-progress runs; download logs only after
  completion (zip API 404s mid-run).
- When testing locally, use `--config` with a temp db or flights_local.db; ALWAYS
  `git checkout -- flights.db` if a test dirtied it, and `git pull --rebase` before
  push (the bot may have committed meanwhile).
- Logs: use the `log` logger (INFO default, `--verbose` debug); every line timestamped
  — CI log greps are the debugging tool (`gh run view <id> -R la55u/japan-trips --log |
  grep ...`).
- ranking counts: RT ~1133 + OJ ~3960 + SS rows; planned Google queries: 1708.
- The tfs protobuf request template embedded in each page is session-bound; never cache
  tokens across fetches.

## Current status / open threads

- Merged single ranking table with per-row multi-source links: IMPLEMENTED locally,
  needs CI validation (deployed page updates on next successful run).
- Rotating Google cursor + hourly cron at :23: live.
- Skyscanner: 10 combos/12h cadence live; gap-discovery validated (found deals up to
  1000 EUR below Google).
- Possible future work: month-grid mining (blocked on PX/flow complexity),
  residential proxy fallback, price_history-based charts, Duffel as a third source.