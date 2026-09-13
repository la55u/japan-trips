from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from fast_flights import FlightQuery, Passengers, create_query
from primp import Client

BASE = Path(__file__).resolve().parent
SOCS_COOKIE = "SOCS=CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzEaAmVuIAEaBgiA_LyaBg"
TYO, OSA = "TYO", "OSA"
VIE = "VIE"
CITIES = {
    "TYO": {"HND", "NRT"},
    "OSA": {"KIX", "ITM", "UKB"},
}
CACHE_VERSION = 2
HTTP_TIMEOUT_SECONDS = 30

log = logging.getLogger("flight_search")


class ReturnValidationError(RuntimeError):
    pass


class RequestGate:
    def __init__(self, requests_per_second: float):
        self.interval = 1 / max(requests_per_second, 0.1)
        self.condition = threading.Condition()
        self.next_request = 0.0
        self.cooldown_until = 0.0

    def wait(self):
        with self.condition:
            while True:
                now = time.monotonic()
                start = max(self.next_request, self.cooldown_until)
                delay = start - now
                if delay <= 0:
                    self.next_request = now + self.interval
                    return
                self.condition.wait(timeout=delay)

    def cooldown(self, seconds: float):
        with self.condition:
            self.cooldown_until = max(
                self.cooldown_until, time.monotonic() + max(seconds, 0)
            )
            self.condition.notify_all()


def _ensure_column(conn, table, definition):
    name = definition.split()[0]
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    p.add_argument("--out", default=str(BASE / "results_local.html"))
    return p.parse_args()


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS price_cache (
            key TEXT PRIMARY KEY,
            kind TEXT, origin TEXT, dest TEXT, d1 TEXT, d2 TEXT,
            price REAL, n_results INTEGER, detail TEXT,
            prev_price REAL, prev_fetched_at TEXT,
            fetched_at TEXT
        );
        CREATE TABLE IF NOT EXISTS price_history (
            key TEXT, price REAL, fetched_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ph_key ON price_history(key);
        CREATE TABLE IF NOT EXISTS itinerary_history (
            run_ts TEXT, itin_key TEXT, kind TEXT, label TEXT,
            airfare REAL, transfers REAL, total REAL
        );
        CREATE INDEX IF NOT EXISTS ih_run ON itinerary_history(run_ts);
        CREATE TABLE IF NOT EXISTS runs (
            run_ts TEXT PRIMARY KEY, fetched INTEGER, cached INTEGER,
            failed INTEGER, note TEXT
        );
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY, value TEXT
        );
        CREATE TABLE IF NOT EXISTS skyscanner_prices (
            key TEXT PRIMARY KEY,
            origin TEXT, dest TEXT, d1 TEXT, d2 TEXT,
            total_results INTEGER, deals_json TEXT,
            fetched_at TEXT, adults INTEGER, currency TEXT
        );
        CREATE TABLE IF NOT EXISTS skyscanner_attempts (
            key TEXT PRIMARY KEY, last_attempt_at TEXT,
            last_success_at TEXT, last_error TEXT
        );
        """
    )
    _ensure_column(conn, "price_cache", "last_attempt_at TEXT")
    _ensure_column(conn, "price_cache", "last_error TEXT")
    _ensure_column(conn, "skyscanner_prices", "adults INTEGER")
    _ensure_column(conn, "skyscanner_prices", "currency TEXT")
    conn.execute(
        "DELETE FROM itinerary_history WHERE rowid NOT IN "
        "(SELECT MIN(rowid) FROM itinerary_history GROUP BY run_ts, itin_key)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ih_run_key "
        "ON itinerary_history(run_ts, itin_key)"
    )
    conn.commit()
    return conn


def build_query(kind, origin, dest, d1, d2, cfg):
    s = cfg["search"]
    flights = [FlightQuery(d1, origin, dest)]
    if kind == "RT":
        flights.append(FlightQuery(d2, dest, origin))
    trip = "round-trip" if kind == "RT" else "one-way"
    return create_query(
        trip=trip,
        currency="EUR",
        max_stops=s["max_stops"],
        checked_bags=s["checked_bags"],
        flights=flights,
        passengers=Passengers(adults=s.get("adults", 2)),
        language="en",
    )


def fetch_html(q, gate=None):
    if gate:
        gate.wait()
    client = Client(impersonate="chrome_145", impersonate_os="macos", cookie_store=True)
    resp = client.get(
        "https://www.google.com/travel/flights",
        params=q.params(),
        headers={"Cookie": SOCS_COOKIE},
        timeout=HTTP_TIMEOUT_SECONDS,
        read_timeout=HTTP_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        if gate and resp.status_code in (403, 429):
            gate.cooldown(60)
        raise RuntimeError(f"Google page HTTP {resp.status_code}")
    return client, resp.text


def _itinerary_from_entry(k):
    flight = k[0]
    p = k[1][0] if k[1] else None
    if not isinstance(p, list) or len(p) < 2 or p[1] is None:
        return None
    price = float(p[1])
    segs = []
    airports = {}
    for s in flight[2]:
        seg = {
            "from": s[3],
            "from_name": s[4],
            "to": s[6],
            "to_name": s[5],
            "dep_t": _clock(s[8]),
            "arr_t": _clock(s[10]),
            "dur_min": s[11],
            "dep_d": tuple(s[20]),
            "arr_d": tuple(s[21]),
        }
        airports[seg["from"]] = seg["from_name"]
        airports[seg["to"]] = seg["to_name"]
        segs.append(seg)
    if not segs:
        return None
    airlines = [a if isinstance(a, str) else a[1] for a in (flight[1] or [])]
    codes = [segs[0]["from"]] + [s["to"] for s in segs]
    dep_dt = datetime(*segs[0]["dep_d"], *segs[0]["dep_t"], tzinfo=timezone.utc)
    arr_dt = datetime(*segs[-1]["arr_d"], *segs[-1]["arr_t"], tzinfo=timezone.utc)
    total_min = sum(s["dur_min"] for s in segs)
    for a, b in pairwise(segs):
        lay = (
            datetime(*b["dep_d"], *b["dep_t"], tzinfo=timezone.utc)
            - datetime(*a["arr_d"], *a["arr_t"], tzinfo=timezone.utc)
        ).total_seconds() / 60
        if lay > 0:
            total_min += lay
    plus = (date(*segs[-1]["arr_d"]) - date(*segs[0]["dep_d"])).days
    legs = []
    for s in flight[2]:
        num = s[22] if len(s) > 22 else None
        legs.append(
            {
                "from": s[3],
                "date": date(*s[20]).isoformat(),
                "to": s[6],
                "airline": num[0] if num else None,
                "num": num[1] if num else None,
            }
        )
    blob = k[1][1] if isinstance(k[1], list) and len(k[1]) > 1 else None
    return {
        "price": price,
        "airlines": airlines,
        "route": " -> ".join(codes),
        "stops": len(segs) - 1,
        "dep": dep_dt.strftime("%H:%M"),
        "arr": arr_dt.strftime("%H:%M"),
        "plus": plus,
        "dur_h": round(total_min / 60, 1),
        "airports": airports,
        "blob": blob,
        "legs": legs,
    }


RPC_PATH = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetShoppingResults"
)


def _rpc_extras(page_html):
    token_m = re.search(r"[A-Za-z0-9_-]{6,}-{6,}[A-Za-z0-9_-]{6,}", page_html)
    fsid_m = re.search(r'FdrFJe":"(-?[0-9]+)', page_html)
    bl_m = re.search(r'cfb2h":"([a-z0-9_.-]+)"', page_html)
    req_m = re.search(r"'ds:1'\s*:\s*\{id:'[^']*',request:", page_html)
    if not (token_m and fsid_m and bl_m and req_m):
        return None
    try:
        req_json, _ = json.JSONDecoder().raw_decode(page_html, req_m.end())
    except json.JSONDecodeError:
        return None
    if len(req_json) < 2:
        return None
    return token_m.group(0), fsid_m.group(1), bl_m.group(1), req_json


def _rpc_post(client, fsid, bl, inner_req, gate=None):
    url = (
        f"{RPC_PATH}?f.sid={fsid}&bl={bl}"
        "&hl=en&soc-app=162&soc-platform=1&soc-device=1&_reqid=100001&rt=c"
        "&curr=EUR"
    )
    body = "f.req=" + json.dumps(
        [None, json.dumps(inner_req, separators=(",", ":"))], separators=(",", ":")
    )
    if gate:
        gate.wait()
    resp = client.post(
        url,
        headers={
            "Cookie": SOCS_COOKIE,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
        data=body,
        timeout=HTTP_TIMEOUT_SECONDS,
        read_timeout=HTTP_TIMEOUT_SECONDS,
    )
    if gate and resp.status_code in (403, 429):
        gate.cooldown(60)
    return resp


def _parse_rpc_itins(rpc_text, with_envelope=False):
    itins = []
    seen = set()
    parsed_envelope = False
    for line in rpc_text.split("\n"):
        if not line.startswith("[["):
            continue
        try:
            arr = json.loads(line)
            inner = json.loads(arr[0][2])
        except (json.JSONDecodeError, IndexError, KeyError, TypeError):
            continue
        if not isinstance(inner, list) or len(inner) <= 3:
            continue
        if not any(isinstance(inner[idx], list) for idx in (2, 3)):
            continue
        parsed_envelope = True
        for idx in (2, 3):
            section = inner[idx] if len(inner) > idx else None
            if not isinstance(section, list):
                continue
            for block in section:
                if not isinstance(block, list) or block[0] is None:
                    continue
                for el in block:
                    try:
                        it = _itinerary_from_entry(el)
                    except (IndexError, KeyError, TypeError):
                        it = None
                    if not it:
                        continue
                    key = (it["route"], it["dep"], it["price"])
                    if key not in seen:
                        seen.add(key)
                        itins.append(it)
    if with_envelope:
        return itins, parsed_envelope
    return itins


def fetch_rpc_itins(client, page_html, gate=None):
    """For the null-variant pages (ds:1 empty, suggestions instead of results),
    replay the GetShoppingResults RPC the browser would issue, using the request
    template Google embedded in the page itself."""
    extras = _rpc_extras(page_html)
    if not extras:
        raise RuntimeError("Google RPC metadata missing")
    token, fsid, bl, req_json = extras
    inner_req = [[None, None, None, token], req_json[1], 0, 0, 0, 1]
    resp = _rpc_post(client, fsid, bl, inner_req, gate)
    if resp.status_code != 200:
        raise RuntimeError(f"Google shopping RPC HTTP {resp.status_code}")
    itins, parsed_envelope = _parse_rpc_itins(resp.text, with_envelope=True)
    if not parsed_envelope:
        raise RuntimeError("Google shopping RPC response was not recognized")
    return itins


def _find_legs_index(inner2):
    for i, el in enumerate(inner2):
        if (
            isinstance(el, list)
            and len(el) == 2
            and all(isinstance(x, list) and x and isinstance(x[0], list) for x in el)
        ):
            return i
    return None


def _age_hours(ts):
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def _ss_url(origin, dest, d1, d2, domain, adults=2):
    fmt = lambda d: d.replace("-", "")[2:]
    return (
        f"https://www.{domain}/transport/flights/"
        f"{origin.lower()}/{dest.lower()}a/{fmt(d1)}/{fmt(d2)}/"
        f"?adultsv2={adults}&cabinclass=economy&rtn=1"
    )


def _solve_px(pg):
    cap = pg.locator("#px-captcha").first
    cap.wait_for(timeout=10000)
    box = cap.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    pg.mouse.move(x, y)
    pg.mouse.down()
    for i in range(22):
        pg.wait_for_timeout(500)
        pg.mouse.move(x + (i % 3 - 1), y + (i % 2), steps=2)
    pg.mouse.up()
    pg.wait_for_timeout(8000)


def _parse_skyscanner_payload(body, domain, currency):
    data = json.loads(body)
    it = data.get("itineraries", {})
    results = it.get("results") or []
    agents = {a["id"]: a.get("name", a["id"]) for a in (it.get("agents") or [])}
    deals = []
    for r in results:
        legs = [
            {
                "from": leg["origin"]["id"],
                "to": leg["destination"]["id"],
                "stops": leg.get("stopCount", 0),
                "dep": leg.get("departure", "")[:16],
                "arr": leg.get("arrival", "")[:16],
                "dur_min": leg.get("durationInMinutes"),
                "carriers": [
                    c.get("name", "")
                    for c in leg.get("carriers", {}).get("marketing", [])
                ],
            }
            for leg in (r.get("legs") or [])
        ]
        options = r.get("pricingOptions") or []
        agent_ids = options[0].get("agentIds", []) if options else []
        link = None
        if options:
            items = options[0].get("items") or []
            if items and items[0].get("url"):
                link = "https://www." + domain + items[0]["url"]
        deals.append(
            {
                "price_raw": r["price"]["raw"],
                "price_fmt": r["price"].get("formatted", ""),
                "currency": r["price"].get("currencyCode") or currency,
                "self_transfer": bool(r.get("isSelfTransfer")),
                "protected": bool(r.get("isProtectedSelfTransfer")),
                "agents": [agents.get(a, a) for a in agent_ids],
                "legs": legs,
                "link": link,
            }
        )
    deals.sort(key=lambda d: d["price_raw"])
    total = (it.get("context") or {}).get("totalResults", len(results))
    return total, deals


# Skyscanner internal place entityIds (harvested from the site's own
# web-unified-search requests, 2026-09). Used by the fast in-page fetch path;
# unknown codes automatically fall back to the page-navigation flow.
_SS_ENTITY_IDS = {
    "BUD": "95673439",
    "VIE": "95673444",
    "TYO": "27542089",
    "OSA": "27542908",
}

# Runs inside the live Skyscanner page (camoufox): POSTs the unified-search
# query with the session's own cookies/PX state, polls until complete, and
# extracts the top-N cheapest deals in the same shape as
# _parse_skyscanner_payload. ~1-4s per query vs ~40-60s per page navigation.
_SS_FETCH_JS = """
async ([payload, headers, topN, currency]) => {
  const post = await fetch('/g/radar/api/v2/web-unified-search/', {
    method: 'POST', headers: headers, body: JSON.stringify(payload),
    credentials: 'include',
  });
  if (post.status !== 200) return { http: post.status, err: 'post', deals: [] };
  let data = await post.json();
  let searchCtx = data.context || {};
  const pollHeaders = {...headers};
  delete pollHeaders['CONTENT-TYPE'];
  let tries = 0;
  while (searchCtx.status === 'incomplete' && tries < 10) {
    await new Promise(res => setTimeout(res, 1500));
    const poll = await fetch(
      '/g/radar/api/v2/web-unified-search/' + encodeURIComponent(searchCtx.sessionId),
      { method: 'GET', headers: pollHeaders, credentials: 'include' });
    if (poll.status !== 200) return { http: poll.status, err: 'poll', deals: [] };
    data = await poll.json();
    searchCtx = data.context || {};
    tries++;
  }
  if (searchCtx.status !== 'complete') return { http: 200, err: 'ctx:' + searchCtx.status, deals: [] };
  const it = data.itineraries || {};
  const itCtx = it.context || {};
  const agents = {};
  for (const a of (it.agents || [])) agents[a.id] = a.name || a.id;
  const results = (it.results || []).slice();
  results.sort((x, y) => (x.price && x.price.raw || 1e12) - (y.price && y.price.raw || 1e12));
  const deals = [];
  for (const r of results.slice(0, topN)) {
    const options = r.pricingOptions || [];
    const aids = options.length ? (options[0].agentIds || []) : [];
    const items = options.length ? (options[0].items || []) : [];
    const legs = (r.legs || []).map(l => ({
      from: l.origin && l.origin.id, to: l.destination && l.destination.id,
      stops: l.stopCount || 0, dep: l.departure, arr: l.arrival,
      dur_min: l.durationInMinutes,
      carriers: ((l.carriers && l.carriers.marketing) || []).map(c => c.name || ''),
    }));
    deals.push({
      price_raw: r.price && r.price.raw, price_fmt: r.price && r.price.formatted,
      currency: r.price && r.price.currencyCode || currency,
      self_transfer: !!r.isSelfTransfer, protected: !!r.isProtectedSelfTransfer,
      agents: aids.map(a => agents[a] || a), legs: legs,
      link: items[0] && items[0].url ? items[0].url : null,
    });
  }
  return { http: 200, err: '', total: itCtx.totalResults || results.length, deals: deals };
}
"""


def _ss_fast_payload(origin, dest, d1, d2, adults=2):
    """Build the web-unified-search POST body for an airport pair.
    Place codes are Skyscanner 'a'-suffixed city codes (bud/tyoa) in URLs but
    the API body uses entityIds. Returns None if an entityId is unknown."""
    eo = _SS_ENTITY_IDS.get(origin.upper())
    ed = _SS_ENTITY_IDS.get(dest.upper())
    if not eo or not ed:
        return None
    y1, m1, dd1 = d1.split("-")
    y2, m2, dd2 = d2.split("-")
    return {
        "cabinClass": "ECONOMY",
        "childAges": [],
        "adults": adults,
        "legs": [
            {
                "legOrigin": {"@type": "entity", "entityId": eo},
                "legDestination": {"@type": "entity", "entityId": ed},
                "dates": {"@type": "date", "year": y1, "month": m1, "day": dd1},
                "placeOfStay": ed,
            },
            {
                "legOrigin": {"@type": "entity", "entityId": ed},
                "legDestination": {"@type": "entity", "entityId": eo},
                "dates": {"@type": "date", "year": y2, "month": m2, "day": dd2},
            },
        ],
    }


def skyscanner_spotcheck(cfg, conn, combos):
    """Spot-check Skyscanner (OTA / self-transfer prices) for selected
    route/date combos via a camoufox browser. Returns rows to store.

    Two fetch paths per combo:
    - fast: reuse the live page session and POST the unified-search API from
      inside the page (fetch) — ~1-4s per combo; needs known entityIds and a
      captured bootstrap request (for headers). Re-bootstraps the page every
      `fast_rebootstrap` fast queries; on failure falls back to navigation.
    - slow: navigate the SPA to the combo's search URL and capture the
      response XHR (original behavior).
    Per-combo try/except: one failing combo never blocks the rest."""
    from camoufox.sync_api import Camoufox

    sk_cfg = cfg.get("skyscanner", {})
    domain = sk_cfg.get("domain", "skyscanner.hu")
    top_deals = sk_cfg.get("top_deals", 10)
    rebootstrap_every = max(1, sk_cfg.get("fast_rebootstrap", 4))
    adults = cfg["search"].get("adults", 2)
    currency = sk_cfg.get("currency", "HUF").upper()
    rows = []
    with Camoufox(
        headless=True, humanize=True, geoip=True, locale=["hu-HU"]
    ) as browser:
        pg = browser.new_page()
        captured = []
        captured_req = []

        def _capture(r):
            try:
                if "web-unified-search" not in r.url:
                    return
                if getattr(r, "method", "") == "POST":
                    captured_req.append(r)
                    return
                if r.status == 200:
                    body = r.text()
                    if len(body) > 100000:
                        captured.append(body)
            except Exception as e:  # noqa: BLE001 - browser event callback boundary
                log.debug("skyscanner response capture failed: %s", e)

        pg.on("response", _capture)
        pg.on("request", _capture)

        def _solve_px(url):
            if pg.locator("#px-captcha").count():
                cap = pg.locator("#px-captcha").first
                cap.wait_for(timeout=10000)
                box = cap.bounding_box()
                x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                pg.mouse.move(x, y)
                pg.mouse.down()
                for i in range(22):
                    pg.wait_for_timeout(500)
                    pg.mouse.move(x + (i % 3 - 1), y + (i % 2), steps=2)
                pg.mouse.up()
                pg.wait_for_timeout(8000)
                pg.goto(url, timeout=90000, wait_until="domcontentloaded")

        def _bootstrap(url):
            """Load a search page, solve PX if needed, and return the SPA's own
            web-unified-search request (for replay headers), or None."""
            captured_req.clear()
            pg.goto(url, timeout=90000, wait_until="domcontentloaded")
            for _ in range(4):
                pg.wait_for_timeout(4000)
                if captured_req:
                    break
            if not captured_req:
                _solve_px(url)
            for _ in range(8):
                pg.wait_for_timeout(5000)
                if captured_req:
                    break
            return captured_req[-1] if captured_req else None

        boot_req = _bootstrap(
            _ss_url(*combos[0][:2], combos[0][2], combos[0][3], domain, adults)
        )
        skip_headers = {
            "host",
            "content-length",
            "connection",
            "alt-used",
            "cookie",
            "referer",
            "accept-encoding",
            "user-agent",
            "origin",
        }

        def _replay_headers(req):
            return (
                {
                    k.upper(): v
                    for k, v in req.headers.items()
                    if k.lower() not in skip_headers
                }
                if req
                else {}
            )

        fast_headers = _replay_headers(boot_req)
        fast_mode = bool(fast_headers)
        if fast_mode:
            log.info("skyscanner fast path armed (bootstrap request captured)")
        since_boot = 0
        needs_reboot = False

        def _rebootstrap(combo):
            nonlocal fast_headers, fast_mode, since_boot, needs_reboot
            needs_reboot = False
            fast_mode = False
            try:
                boot_req = _bootstrap(
                    _ss_url(*combo[:2], combo[2], combo[3], domain, adults)
                )
                fast_headers = _replay_headers(boot_req)
                fast_mode = bool(fast_headers)
            except Exception as e:  # noqa: BLE001 - source remains best effort
                log.warning("skyscanner re-bootstrap failed: %s", str(e)[:120])
            since_boot = 0

        def _parse_fast(out):
            deals = []
            for d in out.get("deals", []):
                deals.append(
                    {
                        "price_raw": d["price_raw"],
                        "price_fmt": d.get("price_fmt", ""),
                        "currency": (d.get("currency") or currency).upper(),
                        "self_transfer": bool(d.get("self_transfer")),
                        "protected": bool(d.get("protected")),
                        "agents": d.get("agents", []),
                        "legs": [
                            {
                                "from": l.get("from"),
                                "to": l.get("to"),
                                "stops": l.get("stops", 0),
                                "dep": (l.get("dep") or "")[:16],
                                "arr": (l.get("arr") or "")[:16],
                                "dur_min": l.get("dur_min"),
                                "carriers": l.get("carriers", []),
                            }
                            for l in (d.get("legs") or [])
                        ],
                        "link": ("https://www." + domain + d["link"])
                        if d.get("link")
                        else None,
                    }
                )
            deals.sort(key=lambda d: d["price_raw"])
            return out.get("total", len(deals)), deals

        for origin, dest, d1, d2 in combos:
            if needs_reboot:
                _rebootstrap((origin, dest, d1, d2))
            done = False
            # --- fast path: in-page API fetch ---
            if fast_mode:
                payload = _ss_fast_payload(origin, dest, d1, d2, adults)
                if payload:
                    try:
                        out = pg.evaluate(
                            _SS_FETCH_JS, [payload, fast_headers, top_deals, currency]
                        )
                        if out.get("http") == 200 and out.get("deals"):
                            total, deals = _parse_fast(out)
                            rows.append((origin, dest, d1, d2, total, deals))
                            log.info(
                                "skyscanner(fast) %s->%s %s..%s: %d results, top %s",
                                origin,
                                dest,
                                d1,
                                d2,
                                total,
                                f"{deals[0]['price_fmt']} ({deals[0]['agents'][0] if deals[0]['agents'] else '?'})"
                                if deals
                                else "none",
                            )
                            done = True
                        else:
                            log.info(
                                "skyscanner(fast) %s->%s %s..%s: http=%s err=%s",
                                origin,
                                dest,
                                d1,
                                d2,
                                out.get("http"),
                                str(out.get("err"))[:40],
                            )
                    except Exception as e:  # noqa: BLE001 - fall back to navigation
                        log.warning(
                            "skyscanner(fast) %s->%s failed: %s",
                            origin,
                            dest,
                            str(e)[:120],
                        )
                    since_boot += 1
                    if done and since_boot >= rebootstrap_every:
                        _rebootstrap((origin, dest, d1, d2))
            if done:
                time.sleep(random.uniform(1, 2))
                continue
            # a fast attempt was made and failed: schedule a re-bootstrap so
            # the next combo starts from a fresh session
            if fast_mode and payload:
                needs_reboot = True
            # --- slow path: navigate the SPA (original behavior) ---
            url = _ss_url(origin, dest, d1, d2, domain, adults)
            captured.clear()
            try:
                pg.goto(url, timeout=90000, wait_until="domcontentloaded")
                pg.wait_for_timeout(8000)
                _solve_px(url)
                for attempt in range(12):
                    pg.wait_for_timeout(5000)
                    if captured:
                        break
                if captured:
                    best_body = max(captured, key=len)
                    total, deals = _parse_skyscanner_payload(
                        best_body, domain, currency
                    )
                    rows.append((origin, dest, d1, d2, total, deals))
                    log.info(
                        "skyscanner %s->%s %s..%s: %d results, top %s",
                        origin,
                        dest,
                        d1,
                        d2,
                        total,
                        f"{deals[0]['price_fmt']} ({deals[0]['agents'][0] if deals[0]['agents'] else '?'})"
                        if deals
                        else "none",
                    )
                else:
                    log.warning(
                        "skyscanner %s->%s %s..%s: no results captured",
                        origin,
                        dest,
                        d1,
                        d2,
                    )
            except Exception as e:  # noqa: BLE001 - isolate each source query
                log.warning("skyscanner %s->%s failed: %s", origin, dest, str(e)[:120])
            time.sleep(random.uniform(5, 10))
    return rows


def _ss_eur(price_raw, currency, huf_per_eur, eur_per_gbp):
    currency = currency.upper()
    if currency == "HUF":
        return price_raw / huf_per_eur
    if currency == "GBP":
        return price_raw * eur_per_gbp
    if currency == "EUR":
        return price_raw
    raise ValueError(f"unsupported Skyscanner currency: {currency}")


def run_skyscanner_if_due(cfg, conn, args, itins):
    """Run the Skyscanner spot-check when due; store results in the DB."""
    sk_cfg = cfg.get("skyscanner", {})
    if not sk_cfg.get("enabled", False):
        return
    if args.no_skyscanner or getattr(args, "rank_only", False):
        return
    s = cfg["search"]
    adults = s.get("adults", 2)
    state_suffix = f"v{CACHE_VERSION}_{adults}"
    last_run_key = f"skyscanner_last_run_{state_suffix}"
    cursor_key = f"skyscanner_discover_cursor_{state_suffix}"
    last = conn.execute(
        "SELECT value FROM state WHERE key=?", (last_run_key,)
    ).fetchone()
    if last:
        age = _age_hours(last[0])
        if age is not None and age < sk_cfg.get("min_age_hours", 20):
            log.info(
                "skyscanner spot-check skipped (last run %.1fh ago, min %dh)",
                age,
                sk_cfg.get("min_age_hours", 20),
            )
            return
    seen = set()
    combos = []

    def _combo(it):
        return (it["out_origin"], it["in_city"], it["d1"], it["d2"])

    n_total = sk_cfg.get("combos", 10)
    n_google = min(4, max(2, n_total // 3))
    by_origin = {}
    for it in itins:
        if it["kind"] != "RT":
            continue
        key = _combo(it)
        if key in seen:
            continue
        by_origin.setdefault(it["out_origin"], []).append((it["total"], key))
    for origin in s["origins"]:
        taken = 0
        for _, key in sorted(by_origin.get(origin, [])):
            if key in seen:
                continue
            seen.add(key)
            combos.append(key)
            taken += 1
            if taken >= 2:
                break
    n_google = len(combos)

    n_discovery = max(0, n_total - n_google)
    cursor = 0

    oj_map = {}
    rt_map = {}
    for it in itins:
        key = _combo(it)
        if it["kind"] == "OJ":
            cur = oj_map.get(key)
            if cur is None or it["airfare"] < cur:
                oj_map[key] = it["airfare"]
        elif it["kind"] == "RT":
            rt_map.setdefault(key, it["airfare"])

    if n_discovery:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(hours=sk_cfg.get("max_age_hours", 36))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        existing = {
            (r[0], r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT origin, dest, d1, d2 FROM skyscanner_prices "
                "WHERE adults=? AND fetched_at>=?",
                (adults, cutoff),
            )
        }

        winner_adjacent = []
        for r in conn.execute(
            "SELECT origin, dest, d1, d2, deals_json FROM skyscanner_prices "
            "WHERE adults=? AND fetched_at>=?",
            (adults, cutoff),
        ):
            deals = json.loads(r[4])
            if not deals:
                continue
            best = min(d["eur"] for d in deals)
            g = rt_map.get((r[0], r[1], r[2], r[3]))
            if g is not None and g - best > 100:
                d1d = date.fromisoformat(r[2])
                d2d = date.fromisoformat(r[3])
                for k in (-3, -2, -1, 1, 2, 3):
                    shifted_d1 = d1d + timedelta(days=k)
                    shifted_d2 = d2d + timedelta(days=k)
                    if not (
                        date.fromisoformat(s["date_start"])
                        <= shifted_d1
                        < shifted_d2
                        <= date.fromisoformat(s["date_end"])
                    ):
                        continue
                    candidate = (
                        r[0],
                        r[1],
                        shifted_d1.isoformat(),
                        shifted_d2.isoformat(),
                    )
                    if candidate not in seen and candidate not in existing:
                        seen.add(candidate)
                        winner_adjacent.append(candidate)
        combos.extend(winner_adjacent[:2])

        gaps = sorted(
            (
                (rt_map[k] - v, k)
                for k, v in oj_map.items()
                if k in rt_map and rt_map[k] - v > 50
            ),
            reverse=True,
        )
        cur_row = conn.execute(
            "SELECT value FROM state WHERE key=?", (cursor_key,)
        ).fetchone()
        if cur_row:
            try:
                cursor = int(cur_row[0])
            except ValueError:
                cursor = 0
        if gaps:
            off = cursor % len(gaps)
            ordered = gaps[off:] + gaps[:off]
            for gap, key in ordered:
                if len(combos) >= n_total:
                    break
                if key in existing or key in seen:
                    continue
                seen.add(key)
                combos.append(key)
    log.info(
        "skyscanner combos: %d google-top (origin-balanced) + %d discovery"
        " (winner-adjacent + largest RT-OJ gaps)",
        n_google,
        len(combos) - n_google,
    )
    if not combos:
        return
    log.info("skyscanner spot-check starting for %d combos", len(combos))
    try:
        rows = skyscanner_spotcheck(cfg, conn, combos)
    except Exception as e:  # noqa: BLE001 - secondary source cannot stop Google
        log.warning("skyscanner spot-check failed entirely: %s", str(e)[:200])
        return
    attempted_at = now_iso()
    huf = cfg["currency"]["huf_per_eur"]
    eur_per_gbp = cfg["currency"].get("eur_per_gbp", 1.17)
    expected_currency = sk_cfg.get("currency", "HUF").upper()
    normalized_rows = []
    for origin, dest, d1, d2, total, deals in rows:
        slim = []
        currencies = set()
        for raw_deal in deals[: sk_cfg.get("top_deals", 10)]:
            d = dict(raw_deal)
            currency = (d.get("currency") or expected_currency).upper()
            try:
                party_eur = _ss_eur(d["price_raw"], currency, huf, eur_per_gbp)
            except (KeyError, TypeError, ValueError) as e:
                log.warning("skipping Skyscanner deal: %s", e)
                continue
            currencies.add(currency)
            d["currency"] = currency
            d["party_eur"] = round(party_eur, 2)
            d["eur"] = round(party_eur / adults, 2)
            slim.append(d)
        if slim or total == 0:
            row_currency = (
                next(iter(currencies))
                if len(currencies) == 1
                else "MIXED"
                if currencies
                else expected_currency
            )
            normalized_rows.append((origin, dest, d1, d2, total, slim, row_currency))
        else:
            log.warning(
                "skyscanner %s->%s %s..%s had no valid currency/price rows",
                origin,
                dest,
                d1,
                d2,
            )
    rows = normalized_rows
    successful = {(r[0], r[1], r[2], r[3]) for r in rows}
    for combo in combos:
        attempt_key = "|".join((state_suffix, *combo))
        if combo in successful:
            conn.execute(
                """INSERT INTO skyscanner_attempts
                   (key, last_attempt_at, last_success_at, last_error)
                   VALUES (?,?,?,NULL)
                   ON CONFLICT(key) DO UPDATE SET
                     last_attempt_at=excluded.last_attempt_at,
                     last_success_at=excluded.last_success_at,
                     last_error=NULL""",
                (attempt_key, attempted_at, attempted_at),
            )
        else:
            conn.execute(
                """INSERT INTO skyscanner_attempts
                   (key, last_attempt_at, last_success_at, last_error)
                   VALUES (?,?,NULL,'no conclusive result')
                   ON CONFLICT(key) DO UPDATE SET
                     last_attempt_at=excluded.last_attempt_at,
                     last_error=excluded.last_error""",
                (attempt_key, attempted_at),
            )
    if not rows:
        conn.commit()
        log.warning(
            "skyscanner spot-check produced no conclusive results; retry remains due"
        )
        return

    for origin, dest, d1, d2, total, slim, row_currency in rows:
        key = f"{state_suffix}|{origin}|{dest}|{d1}|{d2}"
        conn.execute(
            "INSERT INTO skyscanner_prices (key, origin, dest, d1, d2, total_results, deals_json, fetched_at, adults, currency)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET total_results=excluded.total_results,"
            " deals_json=excluded.deals_json, fetched_at=excluded.fetched_at,"
            " adults=excluded.adults, currency=excluded.currency",
            (
                key,
                origin,
                dest,
                d1,
                d2,
                total,
                json.dumps(slim, ensure_ascii=False),
                attempted_at,
                adults,
                row_currency,
            ),
        )
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (last_run_key, attempted_at),
    )
    if n_discovery:
        conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (cursor_key, str(cursor + n_discovery)),
        )
    conn.commit()
    log.info("skyscanner spot-check stored %d combos", len(rows))


def fetch_return_legs(client, page_html, best, gate=None):
    """Replay the 'Select flight' RPC: returns the actual return itineraries
    bookable in combination with the given (selected) outbound."""
    extras = _rpc_extras(page_html)
    if not extras or not best.get("blob") or not best.get("legs"):
        return []
    _, fsid, bl, req_json = extras
    inner2 = json.loads(json.dumps(req_json[1]))
    li = _find_legs_index(inner2)
    if li is None:
        return []
    legs = inner2[li]
    out_leg = legs[0]
    segs = [
        [leg["from"], leg["date"], leg["to"], None, leg["airline"], leg["num"]]
        for leg in best["legs"]
    ]
    d1 = best["legs"][0]["date"]
    base = out_leg[:6]
    tail = out_leg[-1]
    legs[0] = [*base, d1, None, segs, None, None, None, None, None, tail]
    inner_req = [[None, best["blob"]], inner2, 0, 0, 0, 1]
    resp = _rpc_post(client, fsid, bl, inner_req, gate)
    if resp.status_code != 200:
        return []
    return _parse_rpc_itins(resp.text)


def _clock(pair):
    a = pair or []
    padded = [*(a or []), None, None]
    return (padded[0] or 0, padded[1] or 0)


def _suggestions(payload):
    out = []
    try:
        sec = payload[6][0][4][0]
        for s in sec:
            d1, d2, price_thing = s[0], s[1], s[2]
            price = None
            if isinstance(price_thing, list) and price_thing:
                p = price_thing[0]
                if isinstance(p, list) and len(p) > 1 and p[1] is not None:
                    price = float(p[1])
            if d1 and d2 and price:
                out.append({"d1": d1, "d2": d2, "price": price})
    except (IndexError, KeyError, TypeError):
        pass
    return out


def parse_payload(html_text):
    m = re.search(r"<script[^>]*ds:1[^>]*>(.*?)</script>", html_text, re.DOTALL)
    if not m:
        raise RuntimeError("no data script found in page")
    js = m.group(1)
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        log.debug("google error-status page (treated as no-exact-results)")
    payload = json.loads(data)
    suggestions = _suggestions(payload)
    if payload[3] is None or payload[3][0] is None:
        return [], suggestions
    itins = []
    for k in payload[3][0]:
        try:
            it = _itinerary_from_entry(k)
        except (IndexError, KeyError, TypeError):
            it = None
        if it:
            itins.append(it)
    return itins, suggestions


def fetch_with_retry(q, attempts=3, gate=None):
    last = None
    for i in range(attempts):
        try:
            client, page = fetch_html(q, gate)
            itins, suggestions = parse_payload(page)
            if not itins and suggestions:
                rpc = fetch_rpc_itins(client, page, gate)
                return rpc, None, suggestions, client, page
            if itins:
                return itins, None, [], client, page
            raise RuntimeError("Google page had no itineraries or date suggestions")
        except Exception as e:  # noqa: BLE001 - retry all transport/parser failures
            last = f"{type(e).__name__}: {e}"
            if gate and any(
                x in last.lower() for x in ("captcha", "http 403", "http 429")
            ):
                gate.cooldown(60)
            if i < attempts - 1:
                delay = 2 + i * 3 + random.random() * 2
                log.warning(
                    "attempt %d/%d failed (%s), retrying in %.0fs",
                    i + 1,
                    attempts,
                    last,
                    delay,
                )
                time.sleep(delay)
    return None, last, [], None, None


def normalize_per_person(itins, adults):
    for itin in itins:
        party_price = float(itin["price"])
        itin["party_price"] = party_price
        itin["price"] = party_price / adults
    return itins


def itinerary_matches(itin, origin, dest, departure):
    legs = itin.get("legs") or []
    if not legs:
        return False
    valid_origins = CITIES.get(origin, {origin})
    valid_destinations = CITIES.get(dest, {dest})
    return (
        legs[0].get("from") in valid_origins
        and legs[-1].get("to") in valid_destinations
        and legs[0].get("date") == departure
    )


def eligible_itineraries(itins, cfg):
    max_hours = cfg.get("ranking", {}).get("max_leg_hours", 0)
    return [i for i in itins if not max_hours or i.get("dur_h", 0) <= max_hours]


def summarize(itins, cfg):
    eligible = eligible_itineraries(itins, cfg)
    max_hours = cfg.get("ranking", {}).get("max_leg_hours", 0)
    if not eligible:
        raise RuntimeError(f"all {len(itins)} itineraries exceed {max_hours}h")
    best = min(eligible, key=lambda f: f["price"])
    detail = dict(best)
    detail["n_results"] = len(itins)
    detail["n_eligible"] = len(eligible)
    return float(best["price"]), detail


def key_of(kind, origin, dest, d1, d2, cfg):
    s = cfg["search"]
    tail = f"{d1}|{d2 or ''}"
    return (
        f"v{CACHE_VERSION}|{kind}|economy|EUR|en|{s.get('adults', 2)}|"
        f"{s['max_stops']}|{s['checked_bags']}|{origin}|{dest}|{tail}"
    )


def plan_queries(cfg):
    s = cfg["search"]
    start = date.fromisoformat(s["date_start"])
    end = date.fromisoformat(s["date_end"])
    step = s["step_days"]
    dep_dates = []
    d = start
    while d <= end - timedelta(days=s["trip_min_days"]):
        dep_dates.append(d)
        d += timedelta(days=step)

    rt = []
    for origin in s["origins"]:
        for dest in s["destinations"]:
            for d1 in dep_dates:
                for dur in range(s["trip_min_days"], s["trip_max_days"] + 1):
                    d2 = d1 + timedelta(days=dur)
                    if d2 > end:
                        continue
                    rt.append(("RT", origin, dest, d1.isoformat(), d2.isoformat()))

    ow_dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    ow = []
    for origin in s["origins"]:
        for dest in s["destinations"]:
            for d1 in ow_dates:
                ow.append(("OW", origin, dest, d1.isoformat(), None))
            for d2 in ow_dates:
                ow.append(("OW", dest, origin, d2.isoformat(), None))
    return rt + ow


def run_scan(cfg, conn, args, run_ts):
    scan = cfg["scan"]
    ttl = cfg["cache"]["ttl_hours"] * 3600
    workers = args.workers or scan["workers"]
    if args.step:
        cfg["search"]["step_days"] = args.step
    s = cfg["search"]
    all_queries = plan_queries(cfg)
    stale = []
    if args.rank_only:
        log.info("--rank-only: skipping all fetching, rebuilding from cache")
    now = time.time()
    for spec in all_queries:
        kind, origin, dest, d1, d2 = spec
        key = key_of(kind, origin, dest, d1, d2, cfg)
        row = conn.execute(
            "SELECT price, fetched_at, detail, last_attempt_at "
            "FROM price_cache WHERE key=?",
            (key,),
        ).fetchone()
        if args.force or row is None or row[1] is None:
            stale.append((row[3] if row and row[3] else "", spec, key))
        else:
            no_exact = row[2] is not None and "no_exact_results" in row[2]
            age = (
                now - datetime.fromisoformat(row[1].replace("Z", "+00:00")).timestamp()
            )
            if age > ttl or (row[0] is None and not no_exact):
                stale.append((row[3] or row[1] or "", spec, key))
    stale.sort(key=lambda item: item[0])
    stale_total = len(stale)
    todo = [(spec, key) for _, spec, key in stale]
    if args.rank_only:
        todo = []
    if args.limit and len(todo) > args.limit:
        todo = todo[: args.limit]
        log.info(
            "selected %d oldest of %d stale queries",
            len(todo),
            stale_total,
        )

    log.info(
        "run %s | window %s..%s | trip %d-%dd | origins=%s destinations=%s "
        "max_stops=%d checked_bags=%d | ttl=%dh workers=%d force=%s limit=%s",
        run_ts,
        s["date_start"],
        s["date_end"],
        s["trip_min_days"],
        s["trip_max_days"],
        ",".join(s["origins"]),
        ",".join(s["destinations"]),
        s["max_stops"],
        s["checked_bags"],
        cfg["cache"]["ttl_hours"],
        workers,
        args.force,
        args.limit or "none",
    )
    log.info(
        "%d planned queries: %d to fetch, %d fresh in cache, %d stale deferred",
        len(all_queries),
        len(todo),
        len(all_queries) - stale_total,
        stale_total - len(todo),
    )

    lock = threading.Lock()
    progress = {
        "n": 0,
        "fail": 0,
        "empty": 0,
        "cached": len(all_queries) - stale_total,
        "deferred": stale_total - len(todo),
        "selected": len(todo),
    }
    gate = RequestGate(scan.get("requests_per_second", 2.0))
    scan_start = time.time()

    def work(item):
        spec, key = item
        kind, origin, dest, d1, d2 = spec
        q = build_query(kind, origin, dest, d1, d2, cfg)
        t0 = time.time()
        out = fetch_with_retry(q, gate=gate)
        return spec, key, out, time.time() - t0

    consec_fail = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(work, item): item for item in todo}
        for fut in as_completed(futures):
            try:
                spec, key, out, elapsed = fut.result()
            except Exception as e:  # noqa: BLE001 - contain individual futures
                spec, key = futures[fut]
                out = (None, f"{type(e).__name__}: {e}", [], None, None)
                elapsed = 0.0
            itins, err, suggestions, client, page = out
            fetched = now_iso()
            price = None
            detail = None
            n_results = 0
            if itins:
                try:
                    itins = [
                        itin
                        for itin in itins
                        if itinerary_matches(itin, spec[1], spec[2], spec[3])
                        and itin.get("stops", 0) <= s["max_stops"]
                    ]
                    if not itins:
                        raise RuntimeError(
                            "Google returned no itineraries matching requested route/date"
                        )
                    normalize_per_person(itins, s.get("adults", 2))
                    price, detail = summarize(itins, cfg)
                    n_results = len(itins)
                    if spec[0] == "RT" and client is not None:
                        try:
                            eligible_outbounds = sorted(
                                eligible_itineraries(itins, cfg),
                                key=lambda candidate: candidate["price"],
                            )
                            for outbound in eligible_outbounds:
                                candidate_detail = dict(outbound)
                                candidate_detail["n_results"] = len(itins)
                                candidate_detail["n_eligible"] = len(eligible_outbounds)
                                raw_rets = fetch_return_legs(
                                    client, page, candidate_detail, gate
                                )
                                if not raw_rets:
                                    price = float(outbound["price"])
                                    detail = candidate_detail
                                    break
                                max_hours = cfg.get("ranking", {}).get(
                                    "max_leg_hours", 0
                                )
                                rets = [
                                    ret
                                    for ret in raw_rets
                                    if itinerary_matches(ret, spec[2], spec[1], spec[4])
                                    and ret.get("stops", 0) <= s["max_stops"]
                                    and (
                                        not max_hours
                                        or not ret.get("dur_h")
                                        or ret["dur_h"] <= max_hours
                                    )
                                ]
                                if not rets:
                                    continue
                                normalize_per_person(rets, s.get("adults", 2))
                                ret_price, best_ret = summarize(rets, cfg)
                                initial_price = float(outbound["price"])
                                candidate_detail["ret"] = {
                                    k: v
                                    for k, v in best_ret.items()
                                    if k not in ("blob", "legs")
                                }
                                candidate_detail["paired_price"] = ret_price
                                candidate_detail["paired_price_mismatch"] = round(
                                    ret_price - initial_price, 2
                                )
                                candidate_detail["initial_summary_price"] = (
                                    initial_price
                                )
                                candidate_detail["price"] = ret_price
                                detail = candidate_detail
                                price = ret_price
                                break
                            else:
                                raise ReturnValidationError(
                                    "no outbound had a route/date/duration-valid return"
                                )
                        except ReturnValidationError:
                            raise
                        except Exception as e:  # noqa: BLE001 - fare remains usable
                            log.warning("return-leg fetch failed for %s: %s", key, e)
                    detail = json.dumps(detail, ensure_ascii=False)
                except Exception as e:  # noqa: BLE001 - convert to cached attempt error
                    err = f"{type(e).__name__}: {e}"
                    price = None
                    detail = json.dumps({"error": err})
            elif err is None:
                detail = json.dumps(
                    {"no_exact_results": True, "suggestions": suggestions},
                    ensure_ascii=False,
                )
            else:
                detail = json.dumps({"error": err})
            with lock:
                progress["n"] += 1
                if err is not None:
                    progress["fail"] += 1
                n = progress["n"]
            tag = f"{spec[0]} {spec[1]}->{spec[2]} {spec[3]}..{spec[4] or ''}"
            if err is None and price is not None:
                consec_fail = 0
                log.info(
                    "[%d/%d] %s: %.0f EUR, %d itineraries, best %s, %.1fs",
                    n,
                    len(todo),
                    tag,
                    price,
                    n_results,
                    json.loads(detail).get("route", "?"),
                    elapsed,
                )
            elif err is None:
                consec_fail = 0
                with lock:
                    progress["empty"] += 1
                sugg_txt = ", ".join(
                    f"{s['d1']}→{s['d2']} {s['price']:.0f}" for s in suggestions[:4]
                )
                log.info(
                    "[%d/%d] %s: no exact results (nearby: %s%s), %.1fs",
                    n,
                    len(todo),
                    tag,
                    sugg_txt or "none",
                    f" +{len(suggestions) - 4} more" if len(suggestions) > 4 else "",
                    elapsed,
                )
            else:
                consec_fail += 1
                if consec_fail >= 5:
                    pause = 60 * min(consec_fail - 4, 5)
                    log.warning(
                        "%d consecutive failures - possible throttling/captcha, "
                        "pausing all workers for %ds",
                        consec_fail,
                        pause,
                    )
                    gate.cooldown(pause)
                    consec_fail = 0
                log.warning(
                    "[%d/%d] %s: FAILED after retries (%s), %.1fs",
                    n,
                    len(todo),
                    tag,
                    err,
                    elapsed,
                )
            prev = conn.execute(
                "SELECT price, fetched_at FROM price_cache WHERE key=?", (key,)
            ).fetchone()
            if err is not None:
                if prev:
                    conn.execute(
                        "UPDATE price_cache SET last_attempt_at=?, last_error=? WHERE key=?",
                        (fetched, err, key),
                    )
                else:
                    conn.execute(
                        """INSERT INTO price_cache
                           (key, kind, origin, dest, d1, d2, price, n_results,
                            detail, fetched_at, last_attempt_at, last_error)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            key,
                            spec[0],
                            spec[1],
                            spec[2],
                            spec[3],
                            spec[4],
                            None,
                            0,
                            detail,
                            None,
                            fetched,
                            err,
                        ),
                    )
            else:
                conn.execute(
                    """INSERT INTO price_cache
                       (key, kind, origin, dest, d1, d2, price, n_results, detail,
                        prev_price, prev_fetched_at, fetched_at, last_attempt_at,
                        last_error)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                       ON CONFLICT(key) DO UPDATE SET
                         price=excluded.price, n_results=excluded.n_results,
                         detail=excluded.detail,
                         prev_price=excluded.prev_price,
                         prev_fetched_at=excluded.prev_fetched_at,
                         fetched_at=excluded.fetched_at,
                         last_attempt_at=excluded.last_attempt_at,
                         last_error=NULL""",
                    (
                        key,
                        spec[0],
                        spec[1],
                        spec[2],
                        spec[3],
                        spec[4],
                        price,
                        n_results,
                        detail,
                        prev[0] if prev else None,
                        prev[1] if prev else None,
                        fetched,
                        fetched,
                    ),
                )
            if err is None and price is not None:
                conn.execute(
                    "INSERT INTO price_history (key, price, fetched_at) VALUES (?,?,?)",
                    (key, price, fetched),
                )
            conn.commit()

    log.info(
        "scan finished in %.0fs: fetched=%d failed=%d empty=%d fresh=%d deferred=%d",
        time.time() - scan_start,
        progress["n"],
        progress["fail"],
        progress["empty"],
        progress["cached"],
        progress["deferred"],
    )
    return progress


def transfers_for(kind, out_origin, ret_dest, cfg):
    c = cfg["costs"]
    items = []
    total = 0.0
    if kind == "OJ":
        total += c["shinkansen_eur"]
        items.append(("Shinkansen", c["shinkansen_eur"]))
    else:
        total += c["shinkansen_eur"] + c["domestic_flight_eur"]
        items.append(
            (
                "Shinkansen + domestic flight",
                c["shinkansen_eur"] + c["domestic_flight_eur"],
            )
        )
    n_bus = (1 if out_origin == VIE else 0) + (1 if ret_dest == VIE else 0)
    if n_bus:
        total += n_bus * c["flixbus_eur"]
        items.append((f"FlixBus x{n_bus}", n_bus * c["flixbus_eur"]))
    return total, items


def load_rows(conn, cfg):
    rows = {}
    for r in conn.execute(
        "SELECT key, kind, origin, dest, d1, d2, price, detail, fetched_at "
        "FROM price_cache WHERE price IS NOT NULL"
    ):
        key, kind, origin, dest, d1, d2, price, detail, fetched_at = r
        if key != key_of(kind, origin, dest, d1, d2, cfg):
            continue
        max_age = cfg.get("cache", {}).get("max_rank_age_hours", 12)
        if max_age and (
            _age_hours(fetched_at) is None or _age_hours(fetched_at) > max_age
        ):
            continue
        d = json.loads(detail) if detail else {}
        d.setdefault("airlines", [])
        d.setdefault("route", "")
        d.setdefault("stops", 0)
        d["price"] = float(price)
        rows[(kind, origin, dest, d1, d2 or "")] = {**d, "fetched_at": fetched_at}
    return rows


def load_ss_rows(conn, cfg):
    prefix = f"v{CACHE_VERSION}_{cfg['search'].get('adults', 2)}|%"
    return conn.execute(
        "SELECT origin, dest, d1, d2, total_results, deals_json, fetched_at, "
        "adults, currency FROM skyscanner_prices WHERE key LIKE ? "
        "ORDER BY fetched_at DESC",
        (prefix,),
    ).fetchall()


def _ss_itineraries(cfg, ss_rows, rt_prices):
    """Synthesize itineraries from Skyscanner deals, merged into the ranking.
    Only deals cheaper than the Google round-trip for the same pair are kept."""
    out = []
    s = cfg["search"]
    max_hours = cfg.get("ranking", {}).get("max_leg_hours", 0)
    max_age = cfg.get("skyscanner", {}).get("max_age_hours", 36)
    bag_estimate = cfg.get("skyscanner", {}).get("checked_bag_estimate_eur", 0)
    start = date.fromisoformat(s["date_start"])
    end = date.fromisoformat(s["date_end"])
    for (
        origin,
        dest,
        d1,
        d2,
        _total_results,
        deals_json,
        fetched_at,
        adults,
        _currency,
    ) in ss_rows:
        dep_date = date.fromisoformat(d1)
        ret_date = date.fromisoformat(d2)
        duration = (ret_date - dep_date).days
        age = _age_hours(fetched_at)
        if (
            adults != s.get("adults", 2)
            or origin not in s["origins"]
            or dest not in s["destinations"]
            or not start <= dep_date < ret_date <= end
            or not s["trip_min_days"] <= duration <= s["trip_max_days"]
            or age is None
            or age > max_age
        ):
            continue
        deals = json.loads(deals_json)
        if not deals:
            continue
        g = rt_prices.get((origin, dest, d1, d2))
        accepted_keys = set()
        valid_destinations = {dest, *CITIES.get(dest, set())}
        for deal in deals:
            eur = deal.get("eur")
            legs = deal.get("legs") or []
            if (
                eur is None
                or g is None
                or eur + bag_estimate >= g
                or len(legs) != 2
                or legs[0].get("from") != origin
                or legs[0].get("to") not in valid_destinations
                or (legs[0].get("dep") or "")[:10] != d1
                or legs[1].get("from") not in valid_destinations
                or legs[1].get("to") != origin
                or (legs[1].get("dep") or "")[:10] != d2
                or any((leg.get("stops") or 0) > s["max_stops"] for leg in legs)
                or any(
                    max_hours and leg.get("dur_min") and leg["dur_min"] / 60 > max_hours
                    for leg in legs
                )
            ):
                continue
            leg_details = []
            for leg in legs:
                dep, arr = leg.get("dep", ""), leg.get("arr", "")
                plus = 0
                dur_h = None
                if dep and arr:
                    d1d = datetime.fromisoformat(dep)
                    d2d = datetime.fromisoformat(arr)
                    plus = (d2d.date() - d1d.date()).days
                    if leg.get("dur_min"):
                        dur_h = round(leg["dur_min"] / 60, 1)
                leg_details.append(
                    {
                        "route": f"{leg['from']} -> {leg['to']}",
                        "stops": leg.get("stops", 0),
                        "dep": dep[11:16] or None,
                        "arr": arr[11:16] or None,
                        "plus": plus,
                        "dur_h": dur_h,
                        "airlines": [c for c in leg.get("carriers", []) if c],
                        "airports": {},
                        "fetched_at": fetched_at,
                    }
                )
            while len(leg_details) < 2:
                leg_details.append(None)
            tr_total, tr_items = transfers_for("RT", origin, origin, cfg)
            agent = ", ".join(deal.get("agents", [])[:1])
            identity = json.dumps(
                {
                    "route": [origin, dest, d1, d2],
                    "agents": deal.get("agents", []),
                    "legs": [
                        {
                            "from": leg.get("from"),
                            "to": leg.get("to"),
                            "dep": leg.get("dep"),
                            "arr": leg.get("arr"),
                            "carriers": leg.get("carriers", []),
                        }
                        for leg in legs
                    ],
                    "self_transfer": bool(deal.get("self_transfer")),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
            itinerary_key = f"SS|{origin}|{dest}|{d1}|{d2}|{digest}"
            out.append(
                {
                    "key": itinerary_key,
                    "kind": "SS",
                    "out_origin": origin,
                    "ret_dest": origin,
                    "in_city": dest,
                    "out_city": dest,
                    "d1": d1,
                    "d2": d2,
                    "airfare": eur + bag_estimate,
                    "ota_base_fare": eur,
                    "bag_estimate": bag_estimate,
                    "out_detail": leg_details[0],
                    "ret_detail": leg_details[1],
                    "ret_unavailable": False,
                    "transfers": tr_total,
                    "transfer_items": tr_items,
                    "agent": agent,
                    "link": deal.get("link"),
                    "self_transfer": bool(deal.get("self_transfer")),
                    "protected": bool(deal.get("protected")),
                    "bag_included": False,
                    "fetched_at": fetched_at,
                }
            )
            accepted_keys.add(itinerary_key)
            if len(accepted_keys) == 2:
                break
    for it in out:
        it["total"] = it["airfare"] + it["transfers"]
    deduplicated = {}
    for it in out:
        current = deduplicated.get(it["key"])
        if current is None or it["total"] < current["total"]:
            deduplicated[it["key"]] = it
    return list(deduplicated.values())


def build_itineraries(cfg, rows, ss_rows=None):
    s = cfg["search"]
    start = date.fromisoformat(s["date_start"])
    end = date.fromisoformat(s["date_end"])
    step = s["step_days"]
    dep_dates = []
    d = start
    while d <= end - timedelta(days=s["trip_min_days"]):
        dep_dates.append(d)
        d += timedelta(days=step)

    itins = []
    max_hours = cfg.get("ranking", {}).get("max_leg_hours", 0)

    def allowed(detail):
        return bool(detail) and (
            not max_hours or not detail.get("dur_h") or detail["dur_h"] <= max_hours
        )

    for origin in s["origins"]:
        for dest in s["destinations"]:
            for d1 in dep_dates:
                for dur in range(s["trip_min_days"], s["trip_max_days"] + 1):
                    d2 = d1 + timedelta(days=dur)
                    if d2 > end:
                        continue
                    d1s, d2s = d1.isoformat(), d2.isoformat()
                    rt = rows.get(("RT", origin, dest, d1s, d2s))
                    if rt and allowed(rt):
                        tr_total, tr_items = transfers_for("RT", origin, origin, cfg)
                        ret_detail = None
                        if isinstance(rt.get("ret"), dict):
                            ret_detail = dict(rt["ret"])
                            ret_detail["ret_real"] = True
                        else:
                            ow_ref = rows.get(("OW", dest, origin, d2s, ""))
                            if ow_ref:
                                ret_detail = dict(ow_ref)
                                ret_detail["is_reference"] = True
                        itins.append(
                            {
                                "key": f"RT|{origin}|{dest}|{d1s}|{d2s}",
                                "kind": "RT",
                                "out_origin": origin,
                                "ret_dest": origin,
                                "in_city": dest,
                                "out_city": dest,
                                "d1": d1s,
                                "d2": d2s,
                                "airfare": rt["price"],
                                "out_detail": rt,
                                "ret_detail": ret_detail,
                                "ret_unavailable": ret_detail is None,
                                "transfers": tr_total,
                                "transfer_items": tr_items,
                            }
                        )
    for origin in s["origins"]:
        for d1 in dep_dates:
            for dur in range(s["trip_min_days"], s["trip_max_days"] + 1):
                d2 = d1 + timedelta(days=dur)
                if d2 > end:
                    continue
                d1s, d2s = d1.isoformat(), d2.isoformat()
                for in_city, out_city in ((OSA, TYO), (TYO, OSA)):
                    for home in s["origins"]:
                        out_leg = rows.get(("OW", origin, in_city, d1s, ""))
                        ret_leg = rows.get(("OW", out_city, home, d2s, ""))
                        if not (allowed(out_leg) and allowed(ret_leg)):
                            continue
                        tr_total, tr_items = transfers_for("OJ", origin, home, cfg)
                        itins.append(
                            {
                                "key": f"OJ|{origin}|{home}|{in_city}|{out_city}|{d1s}|{d2s}",
                                "kind": "OJ",
                                "out_origin": origin,
                                "ret_dest": home,
                                "in_city": in_city,
                                "out_city": out_city,
                                "d1": d1s,
                                "d2": d2s,
                                "airfare": out_leg["price"] + ret_leg["price"],
                                "out_detail": out_leg,
                                "ret_detail": ret_leg,
                                "transfers": tr_total,
                                "transfer_items": tr_items,
                            }
                        )
    for it in itins:
        it["total"] = it["airfare"] + it["transfers"]
    if ss_rows:
        rt_prices = {}
        for it in itins:
            if it["kind"] == "RT":
                key = (it["out_origin"], it["in_city"], it["d1"], it["d2"])
                rt_prices.setdefault(key, it["airfare"])
        itins.extend(_ss_itineraries(cfg, ss_rows, rt_prices))
    keys = [it["key"] for it in itins]
    if len(keys) != len(set(keys)):
        raise RuntimeError("duplicate itinerary keys generated")
    itins.sort(key=lambda x: x["total"])
    return itins


def build_with_optional_skyscanner(cfg, conn, rows):
    google_itins = build_itineraries(cfg, rows)
    try:
        return build_itineraries(cfg, rows, load_ss_rows(conn, cfg))
    except Exception:
        log.exception(
            "stored Skyscanner data is invalid; rendering Google-only results"
        )
        return google_itins


def prev_totals(conn):
    prev = {}
    last_run = conn.execute(
        "SELECT run_ts FROM itinerary_history ORDER BY run_ts DESC LIMIT 1"
    ).fetchone()
    if not last_run:
        return prev, None
    ts = last_run[0]
    for r in conn.execute(
        "SELECT itin_key, total FROM itinerary_history WHERE run_ts=?", (ts,)
    ):
        prev[r[0]] = r[1]
    return prev, ts


def fmt_date(iso):
    return date.fromisoformat(iso).strftime("%a %d %b %Y")


def render_html(cfg, itins, prev, prev_ts, run_ts, conn, args, progress):
    top_n = args.top or cfg["scan"]["top_n"]
    huf = cfg["currency"]["huf_per_eur"]
    bucket_counts = {}
    shown = []
    for it in itins:
        trip_days = (date.fromisoformat(it["d2"]) - date.fromisoformat(it["d1"])).days
        bucket = (it["out_origin"], trip_days)
        if bucket_counts.get(bucket, 0) >= top_n:
            continue
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        shown.append(it)

    best_rt = next((i for i in itins if i["kind"] == "RT"), None)
    best_oj = next((i for i in itins if i["kind"] == "OJ"), None)

    def card(label, it, best=False):
        cls = "card best" if best else "card"
        if not it:
            return f'<div class="{cls}"><div class="card-label">{label}</div><div class="card-value">no data yet</div></div>'
        return (
            f'<div class="{cls}"><div class="card-label">{label}</div>'
            f'<div class="card-value" data-eur="{it["total"]:.0f}">{it["total"]:.0f} <span class="cur">EUR</span></div>'
            f'<div class="card-sub">{html.escape(it["label"])}</div></div>'
        )

    def _dur_cls(h):
        if h <= 20:
            return "dur-ok"
        if h <= 24:
            return "dur-mid"
        return "dur-bad"

    def leg_cell(d, unavailable=False):
        if not d:
            return '<td data-sort-number="999999">—</td>'
        if unavailable:
            return (
                '<td data-sort-number="999999"><small>return leg details not exposed by API</small><br>'
                f"<small>{', '.join(html.escape(a) for a in d.get('airlines', []))}</small></td>"
            )
        names = d.get("airports") or {}
        route = d.get("route", "")
        ref = d.get("is_reference")
        route_html = ("≈ " if ref else "") + " → ".join(
            f'<span title="{html.escape(names.get(c, c))}">{html.escape(c)}</span>'
            for c in route.split(" -> ")
        )
        times = ""
        if d.get("dep"):
            times = f"{html.escape(d['dep'])} → {html.escape(d['arr'])}"
            if d.get("plus"):
                times += f" (+{d['plus']})"
            if d.get("dur_h"):
                times += f' · <span class="{_dur_cls(d["dur_h"])}">{d["dur_h"]}h</span>'
        stops = d.get("stops")
        stops_txt = f" ({stops} stop{'s' if stops != 1 else ''})" if stops else ""
        ref_txt = (
            "<br><small><i>reference: best one-way on this date</i></small>"
            if ref
            else ""
        )
        return (
            f'<td data-sort-number="{d.get("dur_h") or 999999}">{route_html}{stops_txt}<br>'
            f"<small>{times}</small><br>"
            f"<small>{', '.join(html.escape(a) for a in d.get('airlines', []))}</small>"
            f"{ref_txt}</td>"
        )

    def slim(d):
        if not isinstance(d, dict):
            return d
        return {k: v for k, v in d.items() if k not in ("blob", "legs")}

    rows_html = []
    shown_json = []
    ss_domain = cfg.get("skyscanner", {}).get("domain", "skyscanner.hu")
    for i, it in enumerate(shown, 1):
        o, r = it["out_detail"], it["ret_detail"]
        trip_days = (date.fromisoformat(it["d2"]) - date.fromisoformat(it["d1"])).days
        kind_label = {
            "RT": "Round trip",
            "OJ": "Open jaw",
            "SS": "Round trip · OTA",
        }[it["kind"]]
        route_txt = f"{it['out_origin']} → {it['in_city']} · {it['out_city']} → {it['ret_dest']}"
        delta = ""
        if it["key"] in prev and prev[it["key"]] != it["total"]:
            diff = it["total"] - prev[it["key"]]
            cls = "down" if diff < 0 else "up"
            arrow = "▼" if diff < 0 else "▲"
            delta = f'<span class="{cls}">{arrow} {abs(diff):.0f}</span>'
        gf_links = []
        q1 = build_query(
            "OW" if it["kind"] == "OJ" else "RT",
            it["out_origin"],
            it["in_city"],
            it["d1"],
            None if it["kind"] == "OJ" else it["d2"],
            cfg,
        )
        gf_links.append(("Google", q1.url()))
        if it["kind"] == "OJ":
            q2 = build_query("OW", it["out_city"], it["ret_dest"], it["d2"], None, cfg)
            gf_links.append(("Google", q2.url()))
        if it["kind"] == "SS":
            gf_links.append(
                (
                    "Skyscanner",
                    _ss_url(
                        it["out_origin"],
                        it["in_city"],
                        it["d1"],
                        it["d2"],
                        ss_domain,
                        cfg["search"].get("adults", 2),
                    ),
                )
            )
        links = " ".join(
            f'<a class="gf" href="{u}" target="_blank" rel="noopener">{name} ↗</a>'
            for name, u in gf_links
        )
        tr_items = "; ".join(f"{n} ({v:.0f})" for n, v in it["transfer_items"])
        idx = i - 1
        badge_cls = "badge" if it["kind"] in ("RT", "SS") else "badge oj"
        agent_html = (
            f'<br><small class="muted">via {html.escape(it.get("agent", ""))}'
            f"{' · self-transfer' if it.get('self_transfer') else ''}"
            f"{' · protected' if it.get('protected') else ''}</small>"
            if it["kind"] == "SS"
            else ""
        )
        rows_html.append(
            f'<tr data-eur-total="{it["total"]:.2f}" data-origin="{html.escape(it["out_origin"])}" '
            f'data-days="{trip_days}" data-i="{idx}" title="click for details">'
            f'<td class="rank">{i}</td><td><span class="{badge_cls}">{kind_label}</span>{agent_html}</td><td>{route_txt}</td>'
            f'<td data-sort="{it["d1"]}">{fmt_date(it["d1"])}</td>'
            f'<td data-sort="{it["d2"]}">{fmt_date(it["d2"])}</td>'
            f'<td class="num" data-sort-number="{trip_days}">{trip_days}</td>'
            f"{leg_cell(o)}"
            f"{leg_cell(r, unavailable=it.get('ret_unavailable', False))}"
            f'<td class="num" data-eur="{it["airfare"]:.0f}" data-sort-number="{it["airfare"]:.2f}">{it["airfare"]:.0f}</td>'
            f'<td class="num" data-eur="{it["transfers"]:.0f}" data-sort-number="{it["transfers"]:.2f}" title="{html.escape(tr_items)}">{it["transfers"]:.0f}</td>'
            f'<td class="num total" data-eur="{it["total"]:.0f}" data-sort-number="{it["total"]:.2f}">{it["total"]:.0f}</td>'
            f'<td class="num">{delta}</td><td>{links}</td></tr>'
        )
        shown_json.append(
            {
                "kind_label": kind_label,
                "route_txt": route_txt,
                "d1": it["d1"],
                "d2": it["d2"],
                "days": trip_days,
                "airfare": it["airfare"],
                "transfers": it["transfers"],
                "total": it["total"],
                "prev_total": prev.get(it["key"]),
                "transfer_items": it["transfer_items"],
                "gf_links": gf_links,
                "out": slim(o),
                "ret": slim(r),
                "ret_unavailable": it.get("ret_unavailable", False),
                "self_transfer": it.get("self_transfer", False),
                "protected": it.get("protected", False),
                "bag_included": it.get("bag_included", True),
                "ota_base_fare": it.get("ota_base_fare"),
                "bag_estimate": it.get("bag_estimate"),
                "fetched_at": it.get("fetched_at"),
            }
        )

    hist_top = shown[: cfg["scan"]["history_top_n"]]
    chart_labels = []
    chart_series = {it["key"]: [] for it in hist_top}
    series_meta = {it["key"]: it for it in hist_top}
    history_cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=cfg.get("history", {}).get("retention_days", 180))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    hist_rows = conn.execute(
        "SELECT run_ts, itin_key, total FROM itinerary_history "
        "WHERE run_ts>=? ORDER BY run_ts",
        (history_cutoff,),
    ).fetchall()
    by_run = {}
    for ts, k, total in hist_rows:
        by_run.setdefault(ts, {})[k] = total
    all_run_ts = sorted({ts for ts, _, _ in hist_rows})
    for ts in all_run_ts:
        short = ts[5:16].replace("T", " ")
        chart_labels.append(short)
        for k, values in chart_series.items():
            values.append(by_run.get(ts, {}).get(k))

    best_per_run = []
    for ts in all_run_ts:
        vals = [v for v in by_run.get(ts, {}).values() if v is not None]
        best_per_run.append(min(vals) if vals else None)

    datasets = []
    palette = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948"]
    for idx, (k, vals) in enumerate(chart_series.items()):
        meta = series_meta[k]
        datasets.append(
            {
                "label": meta["label"],
                "data": vals,
                "borderColor": palette[idx % len(palette)],
                "backgroundColor": palette[idx % len(palette)],
                "spanGaps": True,
                "tension": 0.25,
                "pointRadius": 3,
            }
        )

    chart_data = {"labels": chart_labels, "datasets": datasets}
    best_chart = {
        "labels": chart_labels,
        "datasets": [
            {
                "label": "Cheapest total (EUR/person)",
                "data": best_per_run,
                "borderColor": "#4e79a7",
                "spanGaps": True,
                "tension": 0.25,
            }
        ],
    }

    def fmt_run_ts(ts):
        if not ts:
            return "none"
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return d.strftime("%d %b %Y, %H:%M UTC")

    def _stats_age(ts):
        if not ts:
            return None
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600

    key_pattern = (
        f"v{CACHE_VERSION}|%|economy|EUR|en|{cfg['search'].get('adults', 2)}|%"
    )
    priced = conn.execute(
        "SELECT COUNT(*) FROM price_cache WHERE key LIKE ? AND price IS NOT NULL",
        (key_pattern,),
    ).fetchone()[0]
    empty = conn.execute(
        "SELECT COUNT(*) FROM price_cache WHERE key LIKE ? "
        "AND detail LIKE '%no_exact_results%'",
        (key_pattern,),
    ).fetchone()[0]
    kinds = dict(
        conn.execute(
            "SELECT kind, COUNT(*) FROM price_cache "
            "WHERE key LIKE ? AND price IS NOT NULL GROUP BY kind",
            (key_pattern,),
        ).fetchall()
    )
    hist = conn.execute(
        "SELECT COUNT(*) FROM price_history WHERE key LIKE ?", (key_pattern,)
    ).fetchone()[0]
    runs_n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    oldest, newest = conn.execute(
        "SELECT MIN(fetched_at), MAX(fetched_at) FROM price_cache "
        "WHERE key LIKE ? AND price IS NOT NULL",
        (key_pattern,),
    ).fetchone()
    planned = len(plan_queries(cfg))

    kind_txt = " · ".join(f"{kinds[k]:,} {k}" for k in ("RT", "OW") if kinds.get(k))
    newest_age = _stats_age(newest)
    oldest_age = _stats_age(oldest)
    freshness_note = None
    if oldest_age is not None:
        ttl = cfg["cache"]["ttl_hours"]
        freshness_note = (
            f"within {ttl}h refresh target"
            if oldest_age <= ttl
            else f"{oldest_age - ttl:.0f}h beyond {ttl}h refresh target"
        )
    stat_items = [
        (f"{priced:,}", "prices tracked", kind_txt),
        (f"{priced + empty}/{planned}", "date pairs checked", f"{empty} flexible-only"),
        (
            f"{newest_age:.0f}h" if newest_age is not None else "–",
            "newest cached price",
            None,
        ),
        (
            f"{oldest_age:.0f}h" if oldest_age is not None else "–",
            "stalest cached price",
            freshness_note,
        ),
        (f"{hist:,}", "history points", None),
        (f"{runs_n}", "runs recorded", None),
        (
            f"{progress['n']}",
            "fetched this run",
            f"{progress.get('empty', 0)} flexible-only, {progress['fail']} failed",
        ),
        (
            f"{progress.get('deferred', 0)}",
            "stale queries deferred",
            None,
        ),
    ]
    stats_html = ""
    for value, label, sub in stat_items:
        sub_html = f"<small>{html.escape(sub)}</small>" if sub else ""
        stats_html += (
            f'<span class="stat" title="{html.escape(label)}">'
            f"<b>{value}</b> {html.escape(label)} {sub_html}</span>"
        )

    html_doc = TEMPLATE
    html_doc = html_doc.replace("__RUN_TS__", run_ts)
    html_doc = html_doc.replace("__HUF__", str(huf))
    html_doc = html_doc.replace("__ADULTS__", str(cfg["search"].get("adults", 2)))
    html_doc = html_doc.replace("__MAX_STOPS__", str(cfg["search"]["max_stops"]))
    html_doc = html_doc.replace("__TABLE_LIMIT__", str(top_n))
    html_doc = html_doc.replace("__MIN_DAYS__", str(cfg["search"]["trip_min_days"]))
    html_doc = html_doc.replace("__MAX_DAYS__", str(cfg["search"]["trip_max_days"]))
    city_names = {"BUD": "Budapest", "VIE": "Vienna"}
    origin_options = '<option value="">All departure cities</option>' + "".join(
        f'<option value="{html.escape(origin)}">'
        f"{html.escape(city_names.get(origin, origin))} ({html.escape(origin)})</option>"
        for origin in cfg["search"]["origins"]
    )
    html_doc = html_doc.replace("__ORIGIN_OPTIONS__", origin_options)
    html_doc = html_doc.replace(
        "__CARDS__",
        card("Cheapest overall", itins[0] if itins else None, best=True)
        + card("Cheapest round trip", best_rt)
        + card("Cheapest open jaw", best_oj),
    )
    html_doc = html_doc.replace("__STATS__", stats_html)
    html_doc = html_doc.replace("__ROWS__", "\n".join(rows_html))

    def script_json(value):
        return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")

    html_doc = html_doc.replace("__ITINS__", script_json(shown_json))
    html_doc = html_doc.replace("__CHART_DATA__", script_json(chart_data))
    html_doc = html_doc.replace("__BEST_CHART__", script_json(best_chart))
    html_doc = html_doc.replace(
        "__META__",
        f"Generated {fmt_run_ts(run_ts)} · per-person prices from "
        f"{cfg['search'].get('adults', 2)}-adult queries · "
        f"previous run: {fmt_run_ts(prev_ts)}",
    )
    html_doc = html_doc.replace(
        "__WINDOW__",
        f"{cfg['search']['date_start']} → {cfg['search']['date_end']}",
    )
    return html_doc


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BUD/VIE -&gt; Japan flight deals</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
 :root {
   --bg: #f5f6f8; --card: #ffffff; --line: #e6e8ec; --line-soft: #eef0f3;
   --text: #191d24; --muted: #69707a; --accent: #2456c8; --accent-soft: #eef3fd;
   --good: #17803d; --good-soft: #e8f5ec; --bad: #b42318; --bad-soft: #fdeceb;
 }
 * { box-sizing: border-box; }
 body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0;
        background: var(--bg); color: var(--text); line-height: 1.45; }
 .wrap { max-width: 1280px; margin: 0 auto; padding: 22px 18px 44px; }
 .top { padding: 2px 0 4px; }
 h1 { font-size: 1.3rem; margin: 0 0 2px; letter-spacing: -0.01em; }
 .meta { color: var(--muted); font-size: .82rem; }
 .toolbar { display: flex; align-items: center; justify-content: space-between;
            flex-wrap: wrap; gap: 10px; margin: 16px 0 12px; }
 .seg { display: inline-flex; background: var(--card); border: 1px solid var(--line);
        border-radius: 999px; padding: 3px; }
 .seg button { border: none; background: transparent; padding: 5px 16px;
               border-radius: 999px; cursor: pointer; font-size: .85rem; color: var(--muted); }
 .seg button.active { background: var(--text); color: #fff; }
 .filters { display: flex; align-items: end; flex-wrap: wrap; gap: 8px; }
 .filter { display: grid; gap: 3px; color: var(--muted); font-size: .68rem;
           letter-spacing: .03em; text-transform: uppercase; }
 .filter select, .filter input { height: 32px; border: 1px solid var(--line);
           border-radius: 8px; background: var(--card); color: var(--text);
           padding: 4px 9px; font: inherit; font-size: .8rem; letter-spacing: 0;
           text-transform: none; }
 .day-range { display: flex; align-items: center; gap: 5px; }
 .day-range input { width: 62px; }
 .match-count { color: var(--muted); font-size: .76rem; padding-bottom: 6px; }
 .stats { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 14px; }
 .stat { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
         padding: 6px 12px; font-size: .78rem; color: var(--muted); }
 .stat b { color: var(--text); font-weight: 650; font-variant-numeric: tabular-nums; }
 .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
          gap: 12px; margin-bottom: 14px; }
 .card { background: var(--card); border: 1px solid var(--line); border-radius: 14px;
         padding: 13px 18px; }
 .card.best { border-color: var(--accent); background: linear-gradient(0deg, var(--accent-soft), var(--card) 70%); }
 .card-label { font-size: .7rem; letter-spacing: .07em; text-transform: uppercase;
               color: var(--muted); font-weight: 600; }
 .card-value { font-size: 1.65rem; font-weight: 700; margin: 2px 0;
               font-variant-numeric: tabular-nums; }
 .card-value .cur { font-size: 1rem; font-weight: 600; color: var(--muted); }
 .card-sub { font-size: .8rem; color: var(--muted); }
 .panel { background: var(--card); border: 1px solid var(--line); border-radius: 14px; }
 .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; border-radius: 14px; }
 table { border-collapse: collapse; font-size: .85rem; width: 100%; }
 thead th { position: sticky; top: 0; z-index: 2; background: var(--card);
            border-bottom: 2px solid var(--line); padding: 10px;
            font-size: .68rem; letter-spacing: .05em; text-transform: uppercase;
            color: var(--muted); text-align: left; cursor: pointer; user-select: none;
            white-space: nowrap; }
 tbody td { padding: 9px 10px; border-bottom: 1px solid var(--line-soft);
            vertical-align: top; }
 tbody tr:last-child td { border-bottom: none; }
 tbody tr { cursor: pointer; }
 tbody tr:hover { background: var(--accent-soft); }
 .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
 td.total { font-weight: 700; color: var(--accent); font-size: .95rem; }
 .rank { color: var(--muted); }
 td small, .dlg small { color: var(--muted); }
 .badge { display: inline-block; font-size: .68rem; font-weight: 600; padding: 2px 9px;
          border-radius: 999px; white-space: nowrap; background: var(--accent-soft);
          color: var(--accent); }
 .badge.oj { background: #fdf1e3; color: #a15c07; }
 .down { color: var(--good); font-weight: 600; }
 .up { color: var(--bad); font-weight: 600; }
 .dur-ok { color: var(--good); font-weight: 600; }
 .dur-mid { color: #b45309; font-weight: 600; }
 .dur-bad { color: var(--bad); font-weight: 600; }
 .ind { color: var(--accent); font-size: .68rem; }
 .gf { font-size: .75rem; text-decoration: none; margin-right: 6px; white-space: nowrap; }
 .gf:hover { text-decoration: underline; }
 .charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
           gap: 12px; margin-top: 14px; }
 .chart-box { background: var(--card); border: 1px solid var(--line);
              border-radius: 14px; padding: 14px; }
 .chart-box h3 { margin: 0 0 10px; font-size: .88rem; font-weight: 600; }
 .foot { color: var(--muted); font-size: .78rem; margin-top: 16px; max-width: 920px; }
 dialog { border: none; border-radius: 16px; padding: 0; width: min(560px, 94vw);
          max-height: 86vh; box-shadow: 0 24px 60px rgba(10, 15, 30, .35); }
 dialog::backdrop { background: rgba(15, 20, 30, .5); backdrop-filter: blur(2px); }
 .dlg-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
             background: var(--text); color: #fff; padding: 14px 18px;
             border-radius: 16px 16px 0 0; }
 .dlg-head h2 { margin: 0; font-size: 1rem; font-weight: 650; line-height: 1.3; }
 .dlg-close { background: none; border: none; color: #fff; font-size: 1.3rem; line-height: 1;
              cursor: pointer; padding: 3px 8px; border-radius: 8px; flex-shrink: 0; }
 .dlg-close:hover { background: rgba(255,255,255,.15); }
 .dlg-body { padding: 16px 18px 18px; overflow-y: auto; font-size: .9rem; }
 .dlg-dates { color: var(--muted); margin-bottom: 12px; font-size: .85rem; }
 .dlg-leg { background: #f8f9fb; border: 1px solid var(--line-soft); border-radius: 10px;
            padding: 11px 13px; margin-bottom: 10px; }
 .dlg-leg h3 { margin: 0 0 5px; font-size: .72rem; letter-spacing: .06em;
               text-transform: uppercase; color: var(--accent); }
 .dlg-leg .route { font-size: 1.02rem; font-weight: 650; }
 .dlg-leg .airports { color: var(--muted); font-size: .78rem; margin: 3px 0 5px; }
 .dlg-leg .fetched { color: var(--muted); font-size: .74rem; margin-top: 5px; }
 .dlg-costs { width: 100%; border-collapse: collapse; margin: 6px 0 2px; }
 .dlg-costs td { border: none; border-top: 1px solid var(--line-soft); padding: 6px 4px; }
 .dlg-costs td:last-child { text-align: right; white-space: nowrap;
                            font-variant-numeric: tabular-nums; }
 .dlg-costs .grand { font-weight: 700; font-size: 1.08rem; color: var(--accent); }
 .dlg-links { margin-top: 12px; }
 .dlg-links a { display: inline-block; margin: 0 8px 6px 0; padding: 6px 14px;
                border: 1px solid var(--accent); border-radius: 8px;
                text-decoration: none; font-size: .83rem; color: var(--accent); }
 .dlg-links a:hover { background: var(--accent); color: #fff; }
 .delta-chip { display: inline-block; padding: 2px 9px; border-radius: 999px;
               font-size: .78rem; font-weight: 600; }
 .delta-chip.down { background: var(--good-soft); color: var(--good); }
 .delta-chip.up { background: var(--bad-soft); color: var(--bad); }
 .muted { color: var(--muted); }
 @media (max-width: 720px) {
   .wrap { padding: 14px 10px 32px; }
   h1 { font-size: 1.05rem; }
   .meta { font-size: .75rem; }
   .cards { grid-template-columns: 1fr; gap: 8px; }
   .card { padding: 10px 14px; }
   .card-value { font-size: 1.3rem; }
   .toolbar { align-items: flex-start; }
   .filters { width: 100%; }
   .filter:first-child { flex: 1; }
   .filter select { width: 100%; }
   table { font-size: .72rem; }
   thead th { padding: 8px 6px; }
   tbody td { padding: 7px 6px; }
   .chart-box { padding: 10px; }
   .dlg-body { padding: 12px 14px 14px; }
 }
</style>
</head>
<body>
<div class="wrap">
 <div class="top">
  <h1>BUD/VIE &harr; Tokyo/Osaka deals &mdash; 12&ndash;16 days, <span id="window">__WINDOW__</span></h1>
  <div class="meta">__META__</div>
 </div>
 <div class="toolbar">
  <div class="seg">
   <button id="btn-eur" class="active" onclick="setCur('EUR')">EUR</button>
   <button id="btn-huf" onclick="setCur('HUF')">HUF</button>
  </div>
  <div class="filters" aria-label="Table filters">
   <label class="filter">Departure city
    <select id="filter-origin">__ORIGIN_OPTIONS__</select>
   </label>
   <label class="filter">Trip days
    <span class="day-range">
     <input id="filter-days-min" type="number" min="__MIN_DAYS__" max="__MAX_DAYS__" value="__MIN_DAYS__" aria-label="Minimum trip days">
     <span>to</span>
     <input id="filter-days-max" type="number" min="__MIN_DAYS__" max="__MAX_DAYS__" value="__MAX_DAYS__" aria-label="Maximum trip days">
    </span>
   </label>
   <span class="match-count" id="match-count" aria-live="polite"></span>
  </div>
 </div>
 <div class="stats">__STATS__</div>
 <div class="cards">__CARDS__</div>
 <div class="panel table-wrap">
 <table id="tbl">
 <thead><tr>
  <th data-k="0">#</th><th data-k="1">Type</th><th data-k="2">Route</th>
  <th data-k="3">Outbound</th><th data-k="4">Return</th><th data-k="5" class="num">Days</th>
  <th data-k="6">Outbound leg</th><th data-k="7">Return leg</th>
  <th data-k="8" class="num">Airfare</th><th data-k="9" class="num">Transfers</th><th data-k="10" class="num">Total</th>
  <th data-k="11" class="num">&Delta;</th><th>Links</th>
 </tr></thead>
 <tbody>
__ROWS__
</tbody>
 </table>
 </div>
 <p class="foot">Prices per person from __ADULTS__-adult queries, max __MAX_STOPS__ stops.
 Google fares request one checked bag; Skyscanner/OTA base fares add a conservative
 configured checked-bag estimate and are labelled separately.
 Open jaw = sum of two one-ways (verify the true multi-city price via the GF links).
 Times are local; (+n) = arrival n days after departure; duration includes layovers.
 For round trips the return leg is the actual flight paired with the shown outbound when
 available (fetched via Google's selection API), otherwise a <i>reference</i> (the best
 one-way on the same date, marked &asymp;) &mdash; verify via the GF links.
 Transfers are config estimates, added per person: open jaw = shinkansen;
 round trip = shinkansen + domestic flight; FlixBus per Vienna leg
 (shinkansen €90, domestic flight €65, FlixBus €15/direction). &Delta; vs previous run. Airport codes carry full names
 on hover &mdash; or click any row for a detail card.</p>
 <div class="charts">
  <div class="chart-box"><h3>Top itineraries &mdash; total price over runs</h3><canvas id="c1"></canvas></div>
  <div class="chart-box"><h3>Cheapest overall over runs</h3><canvas id="c2"></canvas></div>
 </div>
</div>
<dialog id="dlg">
 <div class="dlg-head">
  <h2 id="dlg-title"></h2>
  <button class="dlg-close" aria-label="Close" onclick="document.getElementById('dlg').close()">&#10005;</button>
 </div>
 <div class="dlg-body" id="dlg-body"></div>
</dialog>
<script>
const HUF = __HUF__;
let cur = 'EUR';
function fmt(v) {
  return cur === 'EUR' ? Math.round(v).toLocaleString('en') + ' €'
    : Math.round(v * HUF).toLocaleString('hu') + ' Ft';
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function apply() {
  document.querySelectorAll('[data-eur]').forEach(el => { el.textContent = fmt(parseFloat(el.dataset.eur)); });
  document.getElementById('btn-eur').classList.toggle('active', cur === 'EUR');
  document.getElementById('btn-huf').classList.toggle('active', cur === 'HUF');
  redrawSort();
}
document.querySelectorAll('#tbl th[data-k]').forEach(th => {
  th.dataset.label = th.innerHTML;
  th.addEventListener('click', () => sortBy(th));
});
let sortKey = 10, sortAsc = true;
const TABLE_LIMIT = __TABLE_LIMIT__;
function applyFilters() {
  const origin = document.getElementById('filter-origin').value;
  const rawMin = Number(document.getElementById('filter-days-min').value);
  const rawMax = Number(document.getElementById('filter-days-max').value);
  const minDays = Math.min(rawMin, rawMax), maxDays = Math.max(rawMin, rawMax);
  const rows = [...document.querySelectorAll('#tbl tbody tr')];
  let matching = 0, visible = 0;
  rows.forEach(row => {
    const match = (!origin || row.dataset.origin === origin) &&
      Number(row.dataset.days) >= minDays && Number(row.dataset.days) <= maxDays;
    if (match) matching++;
    const show = match && visible < TABLE_LIMIT;
    row.hidden = !show;
    if (show) {
      visible++;
      row.querySelector('.rank').textContent = visible;
    }
  });
  document.getElementById('match-count').textContent =
    `${visible} shown · ${matching} matching`;
}
function redrawSort() {
  const tb = document.querySelector('#tbl tbody');
  const rows = [...tb.querySelectorAll('tr')];
  rows.sort((a, b) => {
    const ca = a.children[sortKey], cb = b.children[sortKey];
    if (ca.dataset.sortNumber != null && cb.dataset.sortNumber != null) {
      const r = Number(ca.dataset.sortNumber) - Number(cb.dataset.sortNumber);
      return sortAsc ? r : -r;
    }
    let va = ca.dataset.sort ?? ca.innerText.trim(), vb = cb.dataset.sort ?? cb.innerText.trim();
    const na = parseFloat(va.replace(/[^0-9.,-]/g, '').replace(',', '')) || null;
    const nb = parseFloat(vb.replace(/[^0-9.,-]/g, '').replace(',', '')) || null;
    let r;
    if (na !== null && nb !== null) r = na - nb; else r = va.localeCompare(vb);
    return sortAsc ? r : -r;
  });
  rows.forEach(r => tb.appendChild(r));
  document.querySelectorAll('#tbl th[data-k]').forEach(th => {
    th.innerHTML = parseInt(th.dataset.k) === sortKey
      ? th.dataset.label + ' <span class="ind">' + (sortAsc ? '▲' : '▼') + '</span>'
      : th.dataset.label;
  });
  applyFilters();
}
function sortBy(th) {
  const k = parseInt(th.dataset.k);
  if (sortKey === k) sortAsc = !sortAsc; else { sortKey = k; sortAsc = k === 0; }
  redrawSort();
}
function setCur(c) { cur = c; apply(); }
document.getElementById('filter-origin').addEventListener('change', applyFilters);
document.getElementById('filter-days-min').addEventListener('input', applyFilters);
document.getElementById('filter-days-max').addEventListener('input', applyFilters);
const ITINS = __ITINS__;
function fdate(iso) {
  return new Date(iso + 'T12:00:00').toLocaleDateString('en-GB',
    { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
}
function durCls(h) {
  return h <= 20 ? 'dur-ok' : (h <= 24 ? 'dur-mid' : 'dur-bad');
}
function legHtml(d, label, unavailable) {
  if (!d || (unavailable && !d.is_reference)) {
    return `<div class="dlg-leg"><h3>${label}</h3>
      <div class="muted">No leg details available for this row &mdash;
      use the Google Flights link below to verify.</div></div>`;
  }
  const ref = !!d.is_reference;
  const names = d.airports || {};
  const codes = (d.route || '').split(' -> ');
  const route = (ref ? '≈ ' : '') + codes.map(c => `<span title="${esc(names[c] || c)}">${esc(c)}</span>`).join(' → ');
  const nameList = [...new Set(codes.map(c => names[c] || c))].join(' · ');
  const times = d.dep ? `${esc(d.dep)} → ${esc(d.arr)}` +
    (d.plus ? ` (+${d.plus})` : '') +
    (d.dur_h ? ` · <span class="${durCls(d.dur_h)}">${d.dur_h}h</span>` : '') : '';
  const stops = (d.stops ?? null) === null ? '' :
    ` · ${d.stops} stop${d.stops === 1 ? '' : 's'}`;
  const fetched = d.fetched_at ? `fetched ${esc(d.fetched_at.replace('T', ' ').replace('Z', ' UTC'))}` : '';
  const refNote = ref
    ? `<div class="muted" style="font-size:.78rem;margin-top:4px">Reference only: the API does not expose
       round-trip return legs, so this is the best one-way on the same date &mdash; your actual
       return flight may differ. Verify via the Google Flights link.</div>`
    : '';
  return `<div class="dlg-leg"><h3>${label}${ref ? ' (reference)' : ''}</h3>
    <div class="route">${route}</div>
    <div class="airports">${esc(nameList)}</div>
    <div>${times}${stops}</div>
    <div><small>${esc((d.airlines || []).join(', '))}</small></div>
    ${refNote}
    <div class="fetched">${fetched}</div></div>`;
}
function showDetails(it) {
  document.getElementById('dlg-title').textContent =
    `${it.kind_label} — ${it.route_txt}`;
  let delta = '';
  if (it.prev_total != null && it.prev_total !== it.total) {
    const diff = it.total - it.prev_total;
    delta = diff < 0
      ? `<span class="delta-chip down">▼ ${fmt(Math.abs(diff)).trim()} cheaper</span>`
      : `<span class="delta-chip up">▲ ${fmt(diff).trim()} more</span>`;
  }
  const rows = [
    ...(it.ota_base_fare != null
      ? [['OTA base fare', fmt(it.ota_base_fare)], ['Estimated checked bag', fmt(it.bag_estimate)]]
      : [['Airfare', fmt(it.airfare)]]),
    ...it.transfer_items.map(([n, v]) => [esc(n), fmt(v)]),
  ];
  const costs = rows.map(([n, v]) =>
    `<tr><td>${n}</td><td>${v}</td></tr>`).join('');
  const links = it.gf_links
    .map(([name, u]) => `<a href="${u}" target="_blank" rel="noopener">${esc(name)}</a>`)
    .join('');
  const otaNote = !it.bag_included
    ? `<div class="dlg-leg"><b>OTA caveat:</b> the checked-bag amount is an estimate, not a verified quote.` +
      `${it.self_transfer ? ' This is a self-transfer itinerary.' : ''}` +
      `${it.protected ? ' The provider marks the transfer as protected.' : ''}` +
      `${it.fetched_at ? ' Fetched ' + esc(it.fetched_at.replace('T', ' ').replace('Z', ' UTC')) + '.' : ''}</div>`
    : '';
  document.getElementById('dlg-body').innerHTML = `
    <div class="dlg-dates">${fdate(it.d1)} → ${fdate(it.d2)} · ${it.days} days · price per person</div>
    ${legHtml(it.out, 'Outbound')}
    ${legHtml(it.ret, 'Return', it.ret_unavailable)}
    ${otaNote}
    <table class="dlg-costs">
      ${costs}
      <tr class="grand"><td>Total per person</td><td class="grand">${fmt(it.total)}</td></tr>
    </table>
    <div>${delta}</div>
    <div class="dlg-links">${links}</div>`;
  document.getElementById('dlg').showModal();
}
const dlg = document.getElementById('dlg');
document.querySelectorAll('#tbl tbody tr').forEach(tr => {
  tr.addEventListener('click', e => {
    if (e.target.closest('a')) return;
    const it = ITINS[parseInt(tr.dataset.i)];
    if (it) showDetails(it);
  });
});
dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
const chartData = __CHART_DATA__;
const bestChart = __BEST_CHART__;
new Chart(document.getElementById('c1'), {
  type: 'line', data: chartData,
  options: { scales: { y: { title: { display: true, text: 'EUR/person' } } },
             plugins: { legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 10 } } } } }
});
new Chart(document.getElementById('c2'), {
  type: 'line', data: bestChart,
  options: { plugins: { legend: { display: false } } }
});
apply();
</script>
</body>
</html>
"""


def label_itins(itins):
    labels = {"RT": "Round trip", "OJ": "Open jaw", "SS": "Skyscanner RT"}
    for it in itins:
        direction = f"{it['in_city']}/{it['out_city']}"
        it["label"] = (
            f"{labels[it['kind']]} {it['out_origin']}→{it['ret_dest']} "
            f"(via {direction}) {it['d1']} → {it['d2']}"
        )


def refresh_html(cfg, conn, args, run_ts, progress):
    rows = load_rows(conn, cfg)
    itins = build_with_optional_skyscanner(cfg, conn, rows)
    label_itins(itins)
    prev, prev_ts = prev_totals(conn)
    html_doc = render_html(cfg, itins, prev, prev_ts, run_ts, conn, args, progress)
    Path(args.out).write_text(html_doc, encoding="utf-8")
    return itins, prev, prev_ts


def main():
    args = parse_args()
    setup_logging(args.verbose)
    started = time.time()
    cfg = load_config(args.config)
    if args.db:
        cfg["cache"]["db"] = args.db
    conn = init_db(cfg["cache"]["db"])
    run_ts = now_iso()

    progress = run_scan(cfg, conn, args, run_ts)
    rows = load_rows(conn, cfg)
    itins = build_itineraries(cfg, rows)
    label_itins(itins)
    try:
        run_skyscanner_if_due(cfg, conn, args, itins)
    except Exception:
        conn.rollback()
        log.exception("skyscanner post-processing failed; continuing with Google")
    rows = load_rows(conn, cfg)
    itins = build_with_optional_skyscanner(cfg, conn, rows)
    label_itins(itins)
    prev, prev_ts = prev_totals(conn)
    log.info(
        "ranked %d itineraries (prev run for deltas: %s)",
        len(itins),
        prev_ts or "none",
    )

    if not args.rank_only:
        retention_cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=cfg.get("history", {}).get("retention_days", 180))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "DELETE FROM price_history WHERE fetched_at<?", (retention_cutoff,)
        )
        conn.execute(
            "DELETE FROM itinerary_history WHERE run_ts<?", (retention_cutoff,)
        )
        conn.execute("DELETE FROM runs WHERE run_ts<?", (retention_cutoff,))
        for it in itins[: cfg["scan"]["top_n"]]:
            conn.execute(
                "INSERT INTO itinerary_history (run_ts, itin_key, kind, label, airfare, transfers, total)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    run_ts,
                    it["key"],
                    it["kind"],
                    it["label"],
                    it["airfare"],
                    it["transfers"],
                    it["total"],
                ),
            )
        conn.execute(
            "INSERT INTO runs (run_ts, fetched, cached, failed, note) VALUES (?,?,?,?,?)",
            (run_ts, progress["n"], progress["cached"], progress["fail"], ""),
        )
        conn.commit()
        log.info(
            "recorded %d itinerary snapshots + run stats",
            min(len(itins), cfg["scan"]["top_n"]),
        )

    html_doc = render_html(cfg, itins, prev, prev_ts, run_ts, conn, args, progress)
    Path(args.out).write_text(html_doc, encoding="utf-8")

    log.info("%d itineraries ranked. Top 10 (per person):", len(itins))
    for it in itins[:10]:
        log.info("  %7.0f EUR  %s", it["total"], it["label"])
    log.info(
        "done in %.0fs | HTML written to %s (%.0f KB) | fetched=%d cached=%d failed=%d empty=%d",
        time.time() - started,
        args.out,
        len(html_doc) / 1024,
        progress["n"],
        progress["cached"],
        progress["fail"],
        progress.get("empty", 0),
    )


if __name__ == "__main__":
    main()
