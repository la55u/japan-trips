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
- `results.html` — generated report, deployed to GitHub Pages (CI-owned).
- `index.html` — redirect to results.html.
- `.github/workflows/scan.yml` — hourly scan job.
- `requirements.txt` — pinned runtime and Ruff dependencies.
- `README.md` — human-facing docs.

## Environment

- Python 3.14 venv at `./venv` (NOTE: venv bin scripts may retain a stale shebang after
  a repository rename; always invoke via
  `./venv/bin/python -m pip ...` or `./venv/bin/python script.py`).
- The repository directory was previously renamed; do not rely on venv script shebangs.
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

Fetch flow per query (`fetch_with_retry`), with 30-second HTTP timeouts and a shared
2-request/second gate covering page and RPC requests:
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
- RT rows return to their European origin; VIE RT therefore incurs two FlixBus legs.
- Itinerary keys are asserted unique. OJ generation is independent of the RT
  destination loop, so every OJ combination is generated once.
- `[ranking].max_leg_hours` excludes options with a known leg over 30 hours; the
  cheapest eligible option is selected from each Google response.

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
  codes `bud/vie` + `tyoa/osaa`, 2 adults). The fast path captures the SPA's POST
  headers, replays `web-unified-search` in-page, and polls with the top-level response
  `context.sessionId`; it re-bootstraps every `fast_rebootstrap` queries. Any fast-path
  failure falls back to full navigation/XHR capture. PerimeterX challenge =
  `#px-captcha` press-and-hold solved by `_solve_px`. Responses parse
  `itineraries.results[]` (price.raw/formatted,
  isSelfTransfer, isProtectedSelfTransfer, pricingOptions→agents + deep link,
  legs with origin/destination ids, stopCount, departure/arrival, durationInMinutes,
  carriers). Keeps top `top_deals` (10) cheapest per combo, adds `eur` conversion
  using an explicit configured/response currency (HUF via `huf_per_eur`, GBP via
  `eur_per_gbp`; unknown currencies are rejected). Party totals are divided by adults.
- Storage: versioned `skyscanner_prices` keys include adults; rows carry currency and
  passenger count. `skyscanner_attempts` tracks per-combo attempts, successes, and
  errors. A completely failed batch remains immediately due.
- Ranking ignores SS rows older than `max_age_hours` (36), outside current route/date/
  duration scope, over stop/duration limits, or without a matching Google RT. Stored
  discoveries become eligible for refresh after expiry. A configured EUR 120 checked-
  bag estimate is added per person before comparing with Google. Self-transfer,
  protection, source age, and the bag caveat are retained for display.
- CI validated: works from GitHub datacenter IPs (6-10 combos, ~3 min, no challenges
  needed so far; challenges would be solved automatically).
- Skyscanner links on the page point to the Skyscanner search page (`_ss_url`), NOT the
  agent deep link (agent deeplinks are stored but not rendered).

## DB schema (flights.db / flights_local.db)

- `price_cache(key PK, kind RT|OW, origin, dest, d1, d2 NULL, price NULL, n_results,
  detail JSON, prev_price, prev_fetched_at, fetched_at, last_attempt_at, last_error)` —
  one row per query. Keys are versioned and include cabin, currency, language, adults,
  stops, bags, route, and dates. detail JSON for
  priced rows = best itinerary dict (price, airlines, route, stops, dep, arr, plus,
  dur_h, airports map, blob, legs, n_results); for empty rows:
  `{"no_exact_results": true, "suggestions": [...]}`; errors: `{"error": "..."}`.
  TTL: rows with price and confirmed-empty rows are refetched when older than ttl_hours.
  Failed attempts retain last-good price/detail/fetched_at and only update attempt/error
  fields. Rows older than `max_rank_age_hours` are not published.
- `price_history(key, price, fetched_at)` — appended per successful fetch (not used by
  the page yet).
- `itinerary_history(run_ts, itin_key, kind, label, airfare, transfers, total)` — top-N
  snapshot per run; feeds deltas (prev run) and both charts. `(run_ts, itin_key)` is
  unique; startup removes old duplicates before creating the index. Rank-only renders
  do not create synthetic run/history points.
- `runs(run_ts PK, fetched, cached, failed, note)`.
- `state(key PK, value)` — versioned/adult-specific Skyscanner last-run and discovery
  cursor values. Google scheduling is oldest-stale-first and needs no cursor.
- `skyscanner_prices(key PK, origin, dest, d1, d2, total_results, deals_json,
  fetched_at, adults, currency)` — deals_json = list of {price_raw, price_fmt, eur,
  self_transfer, protected, agents, legs[{from,to,stops,dep,arr,dur_min,carriers}],
  link(agent deeplink, stored not rendered)}.
- `skyscanner_attempts(key PK, last_attempt_at, last_success_at, last_error)`.
- `price_history`, `itinerary_history`, and `runs` use 180-day retention.

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
Client-side filters select departure city and a min/max trip-day range. Rendering keeps
the top `top_n` rows per `(departure city, trip days)` bucket, then the browser displays
at most `top_n` matching rows under the active filters and sort order.
Charts (Chart.js CDN): per-itinerary totals over runs (top history_top_n), cheapest
overall per run.

## CI workflow (scan.yml)

Hourly at :23 (off-peak — top-of-hour crons get skipped by GitHub's scheduler; observed
overnight blackout with `0 * * * *`). Steps: checkout@v7 → setup-python@v7 (3.14) →
pip install → Ruff + unittest → `python -m camoufox fetch` → `python flight_search.py
--limit 450 --db flights.db --out results.html` → commit `results.html` + `flights.db`
as github-actions[bot] with pull --rebase; conflicts fail visibly rather than silently
discarding a completed scan → push (Pages redeploys automatically).
Concurrency group `scan` (serial). timeout-minutes 60. Rate config: TTL 4h, 450/run
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
- OJ price = sum of two one-ways (slight overestimate vs true multi-city ticket).
- SS prices are OTA fares: separate tickets/self-transfer and agent middlemen. They are
  shown with an OTA badge and caveats; a conservative bag estimate improves but cannot
  guarantee checkout-price comparability.
- SS EUR conversion: HUF via `huf_per_eur`, GBP via `eur_per_gbp` (static config rates).
- PerimeterX: camoufox + press-and-hold has passed consistently (local + CI). Volume
  kept low (≤10 searches per 12h; fast requests use 1–2s gaps and navigation fallback
  uses 5–10s gaps). If PX escalates: volume down or
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
- Full-cache ranking counts before duration filtering: RT ~1133 + OJ ~1980 + SS rows;
  planned Google queries: 1708.
- The tfs protobuf request template embedded in each page is session-bound; never cache
  tokens across fetches.

## Current status / open threads

- Merged single ranking table with per-row multi-source links: implemented.
- Oldest-first Google scheduling + hourly cron at :23: implemented locally; needs CI
  validation after the two-adult cache migration.
- Skyscanner: 10 combos/12h cadence live; gap-discovery validated (found deals up to
  1000 EUR below Google). Fast replay plus navigation fallback implemented locally;
  needs CI validation.
- Possible future work: month-grid mining (blocked on PX/flow complexity),
  residential proxy fallback, price_history-based charts, Duffel as a third source.
