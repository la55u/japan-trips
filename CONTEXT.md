# japan-trips — flight deal scanner (BUD/VIE ⇄ Tokyo/Osaka)

## Goal

Scan Google Flights (and Skyscanner as a secondary source) for cheap round-trip and
open-jaw flights from Budapest (BUD) and Vienna (VIE) to Tokyo (TYO) and Osaka (OSA),
for a 12–16 day trip between 2027-03-22 and 2027-05-31, for 2 people (prices per person,
2-adult queries divided by two, economy, 1 checked bag, max 2 stops). The 12–16 days
means the difference between outbound and return departure dates, not nights in Japan.
Results are ranked by total
per-person cost (airfare + estimated in-Japan transfers + FlixBus to Vienna if used) and
published as a static HTML page via GitHub Pages, refreshed hourly by GitHub Actions.

**AGENTS.md must be kept brief; this file (CONTEXT.md) is the agent knowledge base and
MUST be updated whenever behavior, schema, config, or pipeline changes.**

## Repository layout

- `flight_search.py` — the entire pipeline in one file.
- `test_flight_search.py` — stdlib unittest regression suite.
- `config.toml` — all knobs (window, costs, TTL, cadence, skyscanner settings).
- `flights.db` — CI-owned SQLite (committed). Local runs use `flights_local.db`.
- `index.html` — generated report, deployed to GitHub Pages (CI-owned).
- `results.css` — static stylesheet linked by index.html (must be committed;
  local `--out` runs into another directory won't style the page).
- GitHub Pages has no root redirect page: the canonical URL is
  https://la55u.github.io/japan-trips/ (index.html is the only HTML
  file in the repo).
- `.github/workflows/scan.yml` — hourly scan job.
- `requirements.txt` — pinned runtime and Ruff dependencies.
- `README.md` — human-facing docs.

## Environment

- Python 3.14 venv at `./venv` (NOTE: venv bin scripts may retain a stale shebang after
  a repository rename; always invoke via
  `./venv/bin/python -m pip ...` or `./venv/bin/python script.py`).
- The repository directory was previously renamed; do not rely on venv script shebangs.
- GitHub: repo `la55u/japan-trips`, Pages at
  https://la55u.github.io/japan-trips/index.html (branch `main`, root).
- Local test DB `flights_local.db` was seeded from `flights.db` once; it diverges.

## Data ownership (critical invariant)

| Store | Writer | Committed |
|---|---|---|
| `flights.db`, `index.html` | CI ONLY (workflow passes `--db flights.db --out index.html`) | yes |
| `flights_local.db`, `index_local.html` | local runs (gitignored defaults) | never |

Never commit local-run artifacts. If scanning against `flights.db` locally: `git pull
--rebase` first, push promptly (SQLite cannot merge).

## Google Flights pipeline (primary source)

Query building uses `fast_flights.create_query` (protobuf builder only — its parser is
NOT used; it crashes on fare-less itineraries). All HTTP via `primp` Client
(`impersonate="chrome_145"`) + hardcoded `SOCS` cookie to bypass the EU consent wall.

Fetch flow per query (`fetch_with_retry`), with 30-second HTTP timeouts and a shared
2-request/second gate covering page and RPC requests:
1. GET `https://www.google.com/travel/flights` with the tfs params → page HTML.
2. `parse_payload`: extract the `ds:1` script and parse the JSON after `data:` with
   `json.JSONDecoder().raw_decode` — some page variants append metadata
   (sideFreebird/errorHasStatus) after the payload, which made plain
   `json.loads` fail with "Extra data" (~11/450 pages in one run, all recovered
   by retry; raw_decode ends the failure class).
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
    - Retry: transport, HTTP, parser, and unrecognized-response failures retry 3× with
      backoff. Only a structurally valid RPC response is accepted as confirmed empty.
3. Round-trips: after outbound parse, `fetch_return_legs` replays the "Select flight"
   RPC (`inner[0]=[None,<blob of chosen outbound>]`, legs array found structurally via
   `_find_legs_index`, outbound leg[0] rebuilt with segment list at index 8 — the
   segment list goes AFTER a null slot, see `fetch_return_legs`). Response parsed the
   same way; cheapest paired return stored in detail["ret"].

Data normalization: Google prices are totals for 2 adults and are divided by the
configured passenger count before storage/ranking. Currency is forced to EUR. Parsed
routes, dates, endpoints, and stop counts are validated against the request before a
fare is cached. The selected-return RPC price is treated as the final paired RT fare;
the initial summary price and mismatch are retained in detail for diagnostics.

`summarize` raises `TooLongError` when EVERY returned itinerary exceeds
`[ranking].max_leg_hours` (e.g. the BUD→OSA late-Apr/May 2027 block where Google only
returns >30h itineraries). These are recorded as CONFIRMED rows —
`price_cache.detail = {"too_long": true, "note": ...}` with a fresh `fetched_at` —
so they are TTL-refreshed (4h) like confirmed-empty rows instead of being retried
every run, and they NEVER count toward the consecutive-failure throttling cooldown.
Other validation errors (e.g. ReturnValidationError) still count as failures.

## Ranking (build_itineraries)

- RT rows from `price_cache` (kind='RT').
- OJ (open jaw): built from OW one-way rows — outbound `origin→in_city` on d1, return
  `out_city→home` on d2; `in_city/out_city` ∈ {(TYO,OSA),(OSA,TYO)}; home ∈ origins
  freely mixed. (Historical bug: used Japan city as return destination — fixed.)
- SS rows: `_ss_itineraries` synthesizes itineraries from `skyscanner_prices` deals
  (kind='SS'), ranked INDEPENDENTLY of Google (no matching Google RT required — OTA
  self-transfer fares are often invisible to Google). Up to `eligible_deals` (5) deals
  per checked pair enter the ranking; airfare=EUR, same transfer model. Agent name +
  Skyscanner page link carried on the row. Rows older than `indicative_after_hours`
  (24) are flagged `indicative`; rows older than `max_age_hours` (168) are dropped.
  Two row kinds: RT rows (round-trip searches, key `vN_a|origin|dest|d1|d2`) and OJ
  rows (true multi-city open-jaw searches, key
  `vN_a|OJ|out_origin|in_city|out_city|home|d1|d2`; `ss_oj` flag on the itinerary,
  badge "Open jaw · OTA", Skyscanner link = `/transport/flights/multicity?origin0=..`
  deep link). OJ validation: leg1 out_origin→in_city on d1, leg2 out_city→home on d2,
  transfers = shinkansen only + FlixBus per VIE endpoint (transfers_for "OJ").
- Transfers (config `[costs]`): OJ = shinkansen 90; RT = shinkansen + domestic 65;
  FlixBus 15/direction for every leg involving VIE (out_origin or ret_dest).
- Sort by total = airfare + transfers.
- RT rows return to their European origin; VIE RT therefore incurs two FlixBus legs.
- Itinerary keys are asserted unique. OJ generation is independent of the RT
  destination loop, so every OJ combination is generated once.
- `[ranking].max_leg_hours` excludes options with a known leg over 30 hours; the
  cheapest eligible option is selected from each Google response. OTA deals with legs
  up to `[ranking].max_leg_hours_display` (48) are still stored and ranked — the
  browser's "Max leg h" filter (`[ranking].max_leg_filter_hours`, default 24) hides
  them by default so cheap-but-slow OTA itineraries can be found manually.

## Skyscanner pipeline (secondary source)

- Runs inside each hourly job when due: state key `skyscanner_last_run` older than
  `min_age_hours` (2h). Fully isolated: per-combo try/except + whole-function
  try/except — a Skyscanner failure can never affect the Google scan or the render.
- Tiered combo selection (`run_skyscanner_if_due`) driven by PER-COMBO refresh times
  from `skyscanner_attempts.last_success_at` (the global cadence only spaces browser
  sessions); ~80 combos/run in three tiers:
  1. **hot** (`hot_combos`=20): stored winners (rows with deals, RT and OJ) whose last
      refresh is older than `hot_refresh_hours` (18) — cheapest first. Keeps displayed
      deals fresh and re-verifies old top deals (a stale top deal stays visible but is
      labelled "indicative" until refreshed).
  2. **neighbours** (`neighbour_combos`=10): ±1..3-day date shifts around stored RT
      pairs whose best deal beats the Google RT by >100 EUR (RT only — the gap is
      defined against the Google RT fare map); candidates already attempted
      within `explore_refresh_hours` are skipped.
  3. **exploration**: every RT combo in the window (`_ss_universe`, same enumeration
      as the RT part of `plan_queries`, 1140 pairs, quota `explore_combos`=30) plus
      every OJ combo (`_ss_oj_universe`: both Japan directions × both origins as
      departure AND return city × all date pairs, ~2840, quota `explore_oj_combos`=20;
      `oj_enabled` turns the OJ tier off) that is unseen or not successfully checked
      within `explore_refresh_hours` (120h); oldest-successful first, round-robin
      quotas per route and trip duration (`_select_exploration`, shape-agnostic:
      route = all city segments, dates = trailing pair). Separate RT/OJ quotas so
      neither universe starves the other.
  - Optional Travelpayouts boost: `_travelpayouts_cheap_pairs` queries the Aviasales
    v3 `prices_for_dates` calendar (cached prices, stdlib urllib, never raises) and
    prioritizes matching exploration pairs within each bucket when
    `[travelpayouts] enabled/token` is set. TP data is never displayed or ranked.
- Fetch (`skyscanner_spotcheck`): camoufox (anti-fingerprint Firefox, humanize, geoip,
  locale hu-HU). Combos are tagged tuples ("RT", o, d, d1, d2) / ("OJ", oo, ic, oc, h,
  d1, d2). The fast path POSTs the same `web-unified-search` API for both kinds — an
  OJ search is just a 2-leg multi-city body (independent leg routes; the API accepts
  them; validated live 2026-09, e.g. VIE→TYO + OSA→BUD returned 1136 itineraries).
  RT bootstraps/re-bootstraps navigate the combo's own RT search URL (headers are
  generic across kinds). The SPA slow path (navigate + XHR capture) is RT-only —
  OJ has no reliable multi-city deep-link URL for the SPA, so OJ is fast-path only:
  on failure it re-bootstraps and retries ONCE (fast POSTs fired immediately after a
  bootstrap are sometimes 403'd by PX), then gives up and the combo stays due.
  `web-unified-search` results can stream in across poll snapshots — a
  complete-but-empty response gets up to 5 extra polls (2s apart) before giving up.
  Keeps top `top_deals` (10) cheapest per combo, adds `eur` conversion using an
  explicit configured/response currency (HUF via `huf_per_eur`, GBP via
  `eur_per_gbp`; unknown currencies are rejected). Party totals are divided by adults.
  ~80 combos ≈ 20-30 min incl. re-bootstraps (CI timeout 60 min is still fine).
- Storage: versioned `skyscanner_prices` keys include adults; RT keys are
  `vN_a|origin|dest|d1|d2` (historical format, unchanged), OJ keys
  `vN_a|OJ|out_origin|in_city|out_city|home|d1|d2` (columns: origin=out_origin,
  dest=home — middle cities live in the key; `parse_ss_key`/`combo_key` convert).
  `skyscanner_attempts` uses the same key format and tracks per-combo attempts,
  successes, and errors AND is the per-combo refresh timestamp that drives
  scheduling. A completely failed batch remains immediately due.
- Ranking freshness: stored deals are eligible up to `max_age_hours` (168 = 7 days);
  deals older than `indicative_after_hours` (24) stay visible but are flagged
  "indicative" in the table badge, detail dialog, and foot note (verify before
  booking). Up to `eligible_deals` (5) unique valid deals per pair are accepted.
  Route/legs/dates, stop, and duration limits are still validated per deal. A
  configured EUR 120 checked-bag estimate is added per person for ranking; the raw
  OTA fare is shown separately. Self-transfer, protection, source age, and the bag
  caveat are retained for display.
- Coverage stats on the page: checked/total pairs (`skyscanner_attempts` successes vs
  `_ss_universe` + `_ss_oj_universe`), stored rows, oldest successful check, and the
  estimated full-sweep time (remaining/`explore_combos`+`explore_oj_combos` ×
  `min_age_hours`).
- CI validated: works from GitHub datacenter IPs (60-combo runs incl. OJ verified
  2026-09-17; occasional PX 412/403 recovered by the fresh-session retry). The
  ~80-combo/2h rate has NOT yet been watched over multiple runs — check the
  coverage/sweep stats and PX escalation after a day.
- Skyscanner links on the page point to the Skyscanner search page (`_ss_url`), NOT the
  agent deep link (agent deeplinks are stored but not rendered).

## DB schema (flights.db / flights_local.db)

- `price_cache(key PK, kind RT|OW, origin, dest, d1, d2 NULL, price NULL, n_results,
  detail JSON, prev_price, prev_fetched_at, fetched_at, last_attempt_at, last_error)` —
  one row per query. Keys are versioned and include cabin, currency, language, adults,
  stops, bags, route, and dates. detail JSON for
  priced rows = best itinerary dict (price, airlines, route, stops, dep, arr, plus,
  dur_h, airports map, blob, legs, n_results); for empty rows:
  `{"no_exact_results": true, "suggestions": [...]}`; over-max-leg rows:
   `{"too_long": true, "note": "..."}` (confirmed, TTL-refreshed); errors:
   `{"error": "..."}`.
  TTL: rows with price, confirmed-empty rows, and confirmed too-long rows are
   refetched when older than ttl_hours.
  Failed attempts retain last-good price/detail/fetched_at and only update attempt/error
  fields. Rows older than `max_rank_age_hours` are not published.
- `price_history(key, price, fetched_at)` — appended per successful fetch (not used by
  the page yet).
- `itinerary_history(run_ts, itin_key, kind, label, airfare, transfers, total)` — top-N
  snapshot per run; feeds deltas (prev run) and both charts. `(run_ts, itin_key)` is
  unique; startup removes old duplicates before creating the index. Rank-only renders
  do not create synthetic run/history points.
- `runs(run_ts PK, fetched, cached, failed, note)`.
- `state(key PK, value)` — versioned/adult-specific Skyscanner last-run value
  (`skyscanner_last_run_vN_<adults>`, spacing only — per-combo due times live in
  `skyscanner_attempts`). The legacy `skyscanner_discover_cursor` key is unused.
  Google scheduling is oldest-stale-first and needs no cursor.
- `skyscanner_prices(key PK, origin, dest, d1, d2, total_results, deals_json,
  fetched_at, adults, currency)` — deals_json = list of {price_raw, price_fmt, eur,
  self_transfer, protected, agents, legs[{from,to,stops,dep,arr,dur_min,carriers}],
  link(agent deeplink, stored not rendered)}. RT keys `vN_a|origin|dest|d1|d2`, OJ
  keys `vN_a|OJ|out_origin|in_city|out_city|home|d1|d2` (origin=out_origin column,
  dest=home column).
- `skyscanner_attempts(key PK, last_attempt_at, last_success_at, last_error)` —
  per-combo refresh times; `last_success_at` drives the tiered scheduler.
- `price_history`, `itinerary_history`, and `runs` use 180-day retention.

## HTML page (index.html)

Template string `TEMPLATE` in flight_search.py, placeholders `__*__` replaced in
render_html. Styles live in the static `results.css` (linked via `<link>`); the page
order: header → compact summary cards → toolbar (EUR/HUF toggle + ALL filters) →
ranking table → insights (Scan status panel + cheapest-per-route chart) →
history charts → collapsible foot notes (`<details>`). Single merged ranking
table (RT/OJ/SS mixed, sorted by total):
columns # / Type (badge; SS rows add "via <agent>", plus a bold "indicative, checked
Xh ago" note when stale) / Route / Outbound / Return / Days / Outbound leg / Return leg
(RT: real paired return when available, else ≈ reference from best one-way; SS: legs
from Skyscanner data) / Airfare (SS rows show the bag-normalized total plus a
"raw fare + bags" breakdown underneath) / Transfers / Total / Δ (vs previous run via
itinerary_history) / Links (last column: ALL sources — Google GF link, + OJ second GF
link, + Skyscanner page link for SS rows).
Dialog (native `<dialog>`): full row details — airport names, times, durations
(color-coded ≤20h green / ≤24h amber / >24h red — same in table), itemized costs one
per line (SS rows itemize OTA base fare and bag estimate), all booking links.
EUR/HUF toggle (huf_per_eur), sortable headers with ▲▼ indicator (default sort: Total
asc). Stats strip: prices tracked (by kind), date pairs checked/planned,
newest/stalest cache age, history points, runs, this-run counts, plus Skyscanner
coverage (checked/total pairs, stored rows, oldest check, full-sweep estimate).
Client-side filters: departure city, source (All / Google RT+OJ / OTA Skyscanner),
max leg hours (default `[ranking].max_leg_filter_hours` = 24; hides rows whose
outbound OR return leg duration exceeds it — raise it to surface the cheap
long-leg OTA rows stored up to `max_leg_hours_display`), a Bag fees checkbox
(default on; unticking rewrites SS rows' airfare/total to the raw OTA fare via
`data-base-fare`/`data-bag`/`data-transfers` and re-sorts — cards and Google rows
are unaffected, and the detail dialog follows the toggle), and a min/max trip-day
range. Rows carry `data-out-dur`/`data-ret-dur` for the filter; legs without a
known duration always pass. Summary cards are computed from the UNFILTERED
ranking (they can disagree with the first visible row when filters hide rows).
Rendering keeps the top `top_n` rows per
`(departure city, trip days)` bucket, then the browser displays at most `top_n`
matching rows under the active filters and sort order.
Summary cards: cheapest overall / round trip / open jaw / OTA (Skyscanner).
Charts (Chart.js CDN): per-itinerary totals over runs (top history_top_n), cheapest
overall per run.

## CI workflows

### verify.yml

Runs Ruff + unittest on push to `main` and on pull requests (setup-python@v7, 3.14,
pip cache). This is the lint/test gate; it is deliberately separate from the scan so a
broken commit fails verification without halting the hourly scans — the scan keeps
running the committed code even while `verify` is red.

### scan.yml

Hourly at :23 (off-peak — top-of-hour crons get skipped by GitHub's scheduler; observed
overnight blackout with `0 * * * *`). Steps: checkout@v7 → setup-python@v7 (3.14) →
pip install → `python -m camoufox fetch` → wait-for-idle
(polls `gh run list` up to 40 min for other in-progress scan runs with startup
jitter; GitHub's `concurrency: scan` group once failed to serialize a
watchdog-dispatch + cron overlap and the loser died on a binary `flights.db`
rebase conflict at the commit step, so this polls as defense in depth) →
`python flight_search.py --limit 450 --db flights.db --out index.html` → commit
`index.html` + `flights.db`
as github-actions[bot] with pull --rebase; conflicts fail visibly rather than silently
discarding a completed scan → push (Pages redeploys automatically).
Concurrency group `scan` (serial). timeout-minutes 120 (raised from 60 after a
post-outage recovery run — full 450-query Google phase + 80 Skyscanner spot-checks —
was killed at the 60 min cap with 5 combos remaining). Rate config: TTL 4h, 450/run
hourly → full window (1708 queries) targets a ~4h refresh. Oldest stale rows are always
selected first and the report distinguishes fresh from deferred stale work. The shared
gate enforces ~2 HTTP requests/s across workers and real cooldowns block workers.
`workflow_dispatch` supports manual runs.

### Local scheduler watchdog

- `watchdog.py` is a stdlib-only script invoked by a systemd user timer. It calls the
  authenticated `/usr/bin/gh`; it never runs the scanner or writes local flight data.
- Default policy: check up to 20 recent `scan.yml` runs, dispatch when the latest
  success is older than 75 minutes, skip if any run is active, and suppress another
  dispatch until the latest attempt is at least 30 minutes old.
- A nonblocking `fcntl` lock at `/tmp/japan-trips-watchdog-<uid>.lock` prevents overlap.
  `--dry-run` performs the GitHub read but never dispatches.
- Version-controlled units live in `systemd/japan-trips-watchdog.{service,timer}` and
  are linked into the user manager. The timer runs every 10 minutes, uses
  `Persistent=true`, and logs to the user journal. The service sets `HOME` and absolute
  binary paths so `gh` finds `~/.config/gh/hosts.yml`.
- User lingering must be enabled for checks to continue after logout. No local timer
  can execute while the machine is powered off; persistent timers catch up at startup.
- Permanent machine removal: disable/stop the timer, remove both linked units from
  `~/.config/systemd/user/`, then run `systemctl --user daemon-reload` and
  `reset-failed`. Run `loginctl disable-linger "$USER"` only if no other user service
  needs lingering. This removes the installation but deliberately preserves repository
  files and shared-journal history; README.md contains the exact commands and checks.

## Google blocking model (empirical)

Fine at ~2 q/s and ~10k queries/day: no captchas observed. Pages vary: normal ds:1,
null+suggestions, null+errorHasStatus — all handled. Google RT payloads contain ONLY
outbound segments (return arrives only via Select-flight RPC). Google does NOT price
OTA self-transfer combos — that gap is Skyscanner's value-add.

## Known limitations

- RT return leg details: real via Select-flight RPC for newly scanned rows; older rows
  fall back to ≈ reference (best one-way same date).
- OJ price = sum of two one-ways for Google OJ rows (slight overestimate vs true
  multi-city ticket). SS OJ rows (Skyscanner multi-city searches) price the whole
  journey as one OTA booking — the closest to the true price, still OTA caveats.
- SS prices are OTA fares: separate tickets/self-transfer and agent middlemen. They are
  shown with an OTA badge and caveats; a conservative bag estimate improves but cannot
  guarantee checkout-price comparability.
- SS EUR conversion: HUF via `huf_per_eur`, GBP via `eur_per_gbp` (static config rates).
- PerimeterX: camoufox + press-and-hold has passed consistently (local + CI). Volume
  kept moderate (≤80 searches per 2h run; fast requests use 1–2s gaps and navigation
  fallback uses 5–10s gaps; session re-bootstraps every 6 fast queries). OJ fast
  POSTs right after a bootstrap are sometimes 403'd — handled by the one fresh-session
  retry. If PX escalates: volume down (tier knobs) or residential proxy (camoufox
  supports `proxy=`).
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
- Full-cache ranking counts before duration filtering: RT ~1133 + OJ ~1980 + SS rows;
  planned Google queries: 1708.
- The tfs protobuf request template embedded in each page is session-bound; never cache
  tokens across fetches.

## Current status / open threads

- Merged single ranking table with per-row multi-source links: implemented.
- Skyscanner true multi-city open-jaw searches (OJ universe + tier, fast-path multi-leg
  payload, OJ ranking/badge/multicity link): implemented and CI-VALIDATED (2026-09-17:
  60 combos/run incl. 10 OJ combos stored with deals from datacenter IPs; one PX 412
  recovered by the fresh-session retry; neighbour-tier tagged-combo date bug found and
  fixed with regression test).
- Oldest-first Google scheduling + hourly cron at :23: implemented locally; needs CI
  validation after the two-adult cache migration.
- Skyscanner tiered scheduler (hot/neighbour/exploration, per-combo refresh times,
  2h cadence, ~80 combos/run incl. separate OJ quota), independent SS ranking (no
  Google match), 5 eligible deals/pair,
  indicative freshness labelling, coverage stats, source filter, OTA summary card,
  and optional Travelpayouts exploration boost: implemented, CI-validated (see
  status above).
- Possible future work: Skyscanner internal
  calendar/flexible-date endpoint (one request could shortlist dozens of dates),
  month-grid mining (blocked on PX/flow complexity), residential proxy fallback,
  price_history-based charts, Duffel as a structured validation source.
