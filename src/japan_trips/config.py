from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

# Repo root (src/japan_trips/config.py -> parents[2]); only meaningful for an
# editable install, which is how this project is used.
BASE = Path(__file__).resolve().parents[2]
SOCS_COOKIE = "SOCS=CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzEaAmVuIAEaBgiA_LyaBg"
TYO, OSA = "TYO", "OSA"
VIE = "VIE"
CITIES = {
    "TYO": {"HND", "NRT"},
    "OSA": {"KIX", "ITM", "UKB"},
}
CACHE_VERSION = 2
HTTP_TIMEOUT_SECONDS = 30

log = logging.getLogger("japan_trips")


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args():
    p = argparse.ArgumentParser(
        description="Scan BUD/VIE <-> Tokyo/Osaka flight deals."
    )
    p.add_argument("--config", default=str(BASE / "config.toml"))
    p.add_argument(
        "--db",
        default=None,
        help="override cache db path (repo flights.db is CI-owned)",
    )
    p.add_argument(
        "--force", action="store_true", help="ignore cache TTL, refetch everything"
    )
    p.add_argument(
        "--limit", type=int, default=0, help="max network queries this run (0 = all)"
    )
    p.add_argument("--workers", type=int, default=0, help="override scan.workers")
    p.add_argument("--step", type=int, default=0, help="override search.step_days")
    p.add_argument("--top", type=int, default=0, help="override scan.top_n")
    p.add_argument(
        "--rank-only",
        action="store_true",
        help="skip fetching, rebuild ranking + HTML from cache",
    )
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument(
        "--no-skyscanner",
        action="store_true",
        help="skip the Skyscanner spot-check this run",
    )
    p.add_argument("--out", default=str(BASE / "index_local.html"))
    return p.parse_args()


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _age_hours(ts):
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    return (datetime.now(UTC) - dt).total_seconds() / 3600
