"""japan-trips — flight deal scanner (BUD/VIE ⇄ Tokyo/Osaka).

Package layout (see CONTEXT.md):
- config     — constants, CLI args, config loading, small time helpers
- db         — SQLite schema/migration
- google     — Google Flights fetch/parse pipeline (primary source)
- skyscanner — Skyscanner/OTA spot-checks + tiered scheduler (secondary source)
- bags       — per-airline baggage fee policy
- ranking    — RT/OJ/SS itinerary ranking
- render     — HTML report generation
- cli        — entry point (python -m japan_trips)

This module is a re-export facade: tests and tools can `import japan_trips as
fs` and reach every name the old single-file module exposed.
"""

# ruff: noqa: F401  (intentional re-exports)

from __future__ import annotations

from datetime import date

from . import bags, db, google, ranking, render, skyscanner
from .bags import BAG_POLICY, _bag_fees, bag_fees_for_legs
from .config import (
    BASE,
    CACHE_VERSION,
    CITIES,
    HTTP_TIMEOUT_SECONDS,
    OSA,
    SOCS_COOKIE,
    TYO,
    VIE,
    load_config,
    now_iso,
    parse_args,
    setup_logging,
)
from .db import init_db
from .google import (
    RequestGate,
    ReturnValidationError,
    TooLongError,
    _itinerary_from_entry,
    _parse_rpc_itins,
    build_query,
    eligible_itineraries,
    fetch_html,
    fetch_return_legs,
    fetch_rpc_itins,
    fetch_with_retry,
    itinerary_matches,
    key_of,
    normalize_per_person,
    parse_payload,
    plan_queries,
    run_scan,
    summarize,
)
from .ranking import (
    _ss_itineraries,
    build_itineraries,
    build_with_optional_skyscanner,
    label_itins,
    load_rows,
    load_ss_rows,
    prev_totals,
    transfers_for,
)
from .render import TEMPLATE, fmt_date, refresh_html, render_html
from .skyscanner import (
    _SS_ENTITY_IDS,
    _select_exploration,
    _ss_eur,
    _ss_fast_payload,
    _ss_fast_payload_legs,
    _ss_multicity_url,
    _ss_oj_universe,
    _ss_universe,
    _ss_url,
    combo_key,
    combo_legs,
    parse_ss_key,
    run_skyscanner_if_due,
    skyscanner_spotcheck,
)

__version__ = "1.0.0"
