# japan-trips

Scanner for cheap flights from Budapest/Vienna to Tokyo/Osaka (12–16 day trips,
2027-03-22 → 2027-05-31), published hourly as a static GitHub Pages report.

- **Source**: `flight_search.py` (single file) + `config.toml`. Google Flights via the
  unofficial API (primp + custom parser + RPC replay), Skyscanner via camoufox.
- **Run**: `./venv/bin/python flight_search.py` (full local scan; CI runs it hourly with
  `--db flights.db --out index.html`).
- **Output**: `index.html` → https://la55u.github.io/japan-trips/
- **Data**: SQLite `flights.db` (CI-owned, committed) for cache + price history.

## Setup

```bash
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python -m camoufox fetch   # only needed for the Skyscanner source
```

## Rules for coding agents

- **CONTEXT.md is the knowledge base and MUST be kept up to date at all times.** Any
  change to behavior, schema, config, pipeline, or environment must be reflected there
  in the same change. Read it first; it documents invariants, gotchas, and known bugs.
- README.md is the human-facing doc; keep it consistent but brief.