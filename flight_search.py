from __future__ import annotations

import argparse
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
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from primp import Client
from fast_flights import FlightQuery, Passengers, create_query

BASE = Path(__file__).resolve().parent
SOCS_COOKIE = "SOCS=CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzEaAmVuIAEaBgiA_LyaBg"
TYO, OSA = "TYO", "OSA"
VIE = "VIE"

log = logging.getLogger("flight_search")


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
            fetched_at TEXT
        );
        """
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
        passengers=Passengers(adults=1),
        language="en",
    )


def fetch_html(q):
    client = Client(impersonate="chrome_145", impersonate_os="macos", cookie_store=True)
    resp = client.get(
        "https://www.google.com/travel/flights",
        params=q.params(),
        headers={"Cookie": SOCS_COOKIE},
    )
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
    dep_dt = datetime(*segs[0]["dep_d"], *segs[0]["dep_t"])
    arr_dt = datetime(*segs[-1]["arr_d"], *segs[-1]["arr_t"])
    total_min = sum(s["dur_min"] for s in segs)
    for a, b in zip(segs, segs[1:]):
        lay = (
            datetime(*b["dep_d"], *b["dep_t"]) - datetime(*a["arr_d"], *a["arr_t"])
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


def _rpc_post(client, fsid, bl, inner_req):
    url = (
        f"{RPC_PATH}?f.sid={fsid}&bl={bl}"
        "&hl=en&soc-app=162&soc-platform=1&soc-device=1&_reqid=100001&rt=c"
        "&curr=EUR"
    )
    body = "f.req=" + json.dumps(
        [None, json.dumps(inner_req, separators=(",", ":"))], separators=(",", ":")
    )
    return client.post(
        url,
        headers={
            "Cookie": SOCS_COOKIE,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
        data=body,
    )


def _parse_rpc_itins(rpc_text):
    itins = []
    seen = set()
    for line in rpc_text.split("\n"):
        if not line.startswith("[["):
            continue
        try:
            arr = json.loads(line)
            inner = json.loads(arr[0][2])
        except (json.JSONDecodeError, IndexError, KeyError, TypeError):
            continue
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
    return itins


def fetch_rpc_itins(client, page_html):
    """For the null-variant pages (ds:1 empty, suggestions instead of results),
    replay the GetShoppingResults RPC the browser would issue, using the request
    template Google embedded in the page itself."""
    extras = _rpc_extras(page_html)
    if not extras:
        return []
    token, fsid, bl, req_json = extras
    inner_req = [[None, None, None, token], req_json[1], 0, 0, 0, 1]
    resp = _rpc_post(client, fsid, bl, inner_req)
    if resp.status_code != 200:
        return []
    return _parse_rpc_itins(resp.text)


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


def _ss_url(origin, dest, d1, d2, domain):
    fmt = lambda d: d.replace("-", "")[2:]
    return (
        f"https://www.{domain}/transport/flights/"
        f"{origin.lower()}/{dest.lower()}a/{fmt(d1)}/{fmt(d2)}/"
        "?adultsv2=1&cabinclass=economy&rtn=1"
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


def _parse_skyscanner_payload(body, domain):
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


def skyscanner_spotcheck(cfg, conn, combos):
    """Spot-check Skyscanner (OTA / self-transfer prices) for selected
    route/date combos via a camoufox browser. Returns rows to store."""
    from camoufox.sync_api import Camoufox

    sk_cfg = cfg.get("skyscanner", {})
    domain = sk_cfg.get("domain", "skyscanner.hu")
    rows = []
    with Camoufox(
        headless=True, humanize=True, geoip=True, locale=["hu-HU"]
    ) as browser:
        pg = browser.new_page()
        captured = []

        def _capture(r):
            try:
                if "web-unified-search" in r.url and r.status == 200:
                    body = r.text()
                    if len(body) > 100000:
                        captured.append(body)
            except Exception:
                pass

        pg.on("response", _capture)
        for origin, dest, d1, d2 in combos:
            url = _ss_url(origin, dest, d1, d2, domain)
            captured.clear()
            try:
                pg.goto(url, timeout=90000, wait_until="domcontentloaded")
                pg.wait_for_timeout(8000)
                if pg.locator("#px-captcha").count():
                    _solve_px(pg)
                    pg.goto(url, timeout=90000, wait_until="domcontentloaded")
                for attempt in range(12):
                    pg.wait_for_timeout(5000)
                    if captured:
                        break
                if captured:
                    best_body = max(captured, key=len)
                    total, deals = _parse_skyscanner_payload(best_body, domain)
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
            except Exception as e:
                log.warning("skyscanner %s->%s failed: %s", origin, dest, str(e)[:120])
            time.sleep(random.uniform(5, 10))
    return rows


def _ss_eur(price_raw, price_fmt, huf_per_eur, gbp_per_eur):
    if "Ft" in price_fmt:
        return price_raw / huf_per_eur
    if "£" in price_fmt:
        return price_raw / gbp_per_eur
    return price_raw


def run_skyscanner_if_due(cfg, conn, args, itins):
    """Run the Skyscanner spot-check when due; store results in the DB."""
    sk_cfg = cfg.get("skyscanner", {})
    if not sk_cfg.get("enabled", False):
        return
    if args.no_skyscanner or getattr(args, "rank_only", False):
        return
    s = cfg["search"]
    last = conn.execute(
        "SELECT value FROM state WHERE key='skyscanner_last_run'"
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

    if n_discovery:
        existing = {
            (r[0], r[1], r[2], r[3])
            for r in conn.execute("SELECT origin, dest, d1, d2 FROM skyscanner_prices")
        }

        winner_adjacent = []
        for r in conn.execute(
            "SELECT origin, dest, d1, d2, deals_json FROM skyscanner_prices"
        ):
            deals = json.loads(r[4])
            if not deals:
                continue
            best = min(d["eur"] for d in deals)
            g = conn.execute(
                "SELECT MIN(price) FROM price_cache WHERE kind='RT' AND origin=? AND dest=? AND d1=? AND d2=?",
                (r[0], r[1], r[2], r[3]),
            ).fetchone()[0]
            if g is not None and g - best > 100:
                from datetime import datetime as _dt

                d1d = date.fromisoformat(r[2])
                d2d = date.fromisoformat(r[3])
                for k in (1, 2, 3):
                    candidate = (
                        r[0],
                        r[1],
                        (d1d + timedelta(days=k)).isoformat(),
                        (d2d + timedelta(days=k)).isoformat(),
                    )
                    if candidate not in seen and candidate not in existing:
                        winner_adjacent.append(candidate)
        combos.extend(winner_adjacent[:2])

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
        gaps = sorted(
            ((rt_map[k] - v, k) for k, v in oj_map.items() if k in rt_map),
            reverse=True,
        )
        cursor = 0
        cur_row = conn.execute(
            "SELECT value FROM state WHERE key='skyscanner_discover_cursor'"
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
                if gap <= 50:
                    break
                seen.add(key)
                combos.append(key)
        conn.execute(
            "INSERT INTO state (key, value) VALUES ('skyscanner_discover_cursor', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(cursor + n_discovery),),
        )
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
    except Exception as e:
        log.warning("skyscanner spot-check failed entirely: %s", str(e)[:200])
        return
    now = now_iso()
    top_deals = sk_cfg.get("top_deals", 10)
    huf = cfg["currency"]["huf_per_eur"]
    gbp = cfg["currency"].get("gbp_per_eur", 1.17)
    for origin, dest, d1, d2, total, deals in rows:
        slim = []
        for d in deals[: sk_cfg.get("top_deals", 10)]:
            d = dict(d)
            d["eur"] = round(_ss_eur(d["price_raw"], d["price_fmt"], huf, gbp), 0)
            slim.append(d)
        key = f"{origin}|{dest}|{d1}|{d2}"
        conn.execute(
            "INSERT INTO skyscanner_prices (key, origin, dest, d1, d2, total_results, deals_json, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET total_results=excluded.total_results,"
            " deals_json=excluded.deals_json, fetched_at=excluded.fetched_at",
            (
                key,
                origin,
                dest,
                d1,
                d2,
                total,
                json.dumps(slim, ensure_ascii=False),
                now_iso(),
            ),
        )
    conn.execute(
        "INSERT INTO state (key, value) VALUES ('skyscanner_last_run', ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (now_iso(),),
    )
    conn.commit()
    log.info("skyscanner spot-check stored %d combos", len(rows))


def fetch_return_legs(client, page_html, best):
    """Replay the 'Select flight' RPC: returns the actual return itineraries
    bookable in combination with the given (selected) outbound."""
    extras = _rpc_extras(page_html)
    if not extras or not best.get("blob") or not best.get("legs"):
        return []
    token, fsid, bl, req_json = extras
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
    resp = _rpc_post(client, fsid, bl, inner_req)
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
    m = re.search(r"<script[^>]*ds:1[^>]*>(.*?)</script>", html_text, re.S)
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


def fetch_with_retry(q, attempts=3):
    last = None
    for i in range(attempts):
        client, page = fetch_html(q)
        try:
            itins, suggestions = parse_payload(page)
            if not itins and suggestions:
                rpc = fetch_rpc_itins(client, page)
                if rpc:
                    return rpc, None, suggestions, client, page
                return [], None, suggestions, client, page
            if itins:
                return itins, None, [], client, page
            return [], None, [], client, page
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
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


def summarize(itins):
    best = min(itins, key=lambda f: f["price"])
    detail = dict(best)
    detail["n_results"] = len(itins)
    return float(best["price"]), detail


def key_of(kind, origin, dest, d1, d2, cfg):
    s = cfg["search"]
    tail = f"{d1}|{d2 or ''}"
    return f"{kind}|{s['max_stops']}|{s['checked_bags']}|{origin}|{dest}|{tail}"


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
    todo = []
    if args.rank_only:
        log.info("--rank-only: skipping all fetching, rebuilding from cache")
    else:
        now = time.time()
        for spec in all_queries:
            kind, origin, dest, d1, d2 = spec
            key = key_of(kind, origin, dest, d1, d2, cfg)
            row = conn.execute(
                "SELECT price, fetched_at, detail FROM price_cache WHERE key=?", (key,)
            ).fetchone()
            if args.force or row is None or row[1] is None:
                todo.append((spec, key))
            else:
                no_exact = row[2] is not None and "no_exact_results" in row[2]
                age = (
                    now
                    - datetime.fromisoformat(row[1].replace("Z", "+00:00")).timestamp()
                )
                if age > ttl or (row[0] is None and not no_exact):
                    todo.append((spec, key))
    if args.limit and len(todo) > args.limit:
        cursor = 0
        cur_row = conn.execute(
            "SELECT value FROM state WHERE key='scan_cursor'"
        ).fetchone()
        if cur_row:
            try:
                cursor = int(cur_row[0])
            except ValueError:
                cursor = 0
        off = cursor % len(todo)
        stale_total = len(todo)
        todo = todo[off:] + todo[:off]
        todo = todo[: args.limit]
        conn.execute(
            "INSERT INTO state (key, value) VALUES ('scan_cursor', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(cursor + args.limit),),
        )
        conn.commit()
        log.info(
            "rotated scan window: offset %d of %d stale queries (cursor=%d)",
            off,
            stale_total,
            cursor + args.limit,
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
        "%d planned queries: %d to fetch, %d served from cache",
        len(all_queries),
        len(todo),
        len(all_queries) - len(todo),
    )

    lock = threading.Lock()
    progress = {"n": 0, "fail": 0, "empty": 0, "cached": len(all_queries) - len(todo)}
    scan_start = time.time()

    def work(item):
        spec, key = item
        kind, origin, dest, d1, d2 = spec
        q = build_query(kind, origin, dest, d1, d2, cfg)
        t0 = time.time()
        out = fetch_with_retry(q)
        return spec, key, q, out, time.time() - t0

    consec_fail = 0
    futures = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for item in todo:
            futures.append(ex.submit(work, item))
        for fut in as_completed(futures):
            spec, key, q, out, elapsed = fut.result()
            itins, err, suggestions, client, page = out
            fetched = now_iso()
            price = None
            detail = None
            n_results = 0
            if itins:
                price, detail = summarize(itins)
                n_results = len(itins)
                if spec[0] == "RT" and client is not None:
                    try:
                        rets = fetch_return_legs(client, page, detail)
                        if rets:
                            best_ret = min(rets, key=lambda r: r["price"])
                            detail["ret"] = {
                                k: v
                                for k, v in best_ret.items()
                                if k not in ("blob", "legs")
                            }
                    except Exception as e:
                        log.warning("return-leg fetch failed for %s: %s", key, e)
                detail = json.dumps(detail, ensure_ascii=False)
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
                n, f_, c = progress["n"], progress["fail"], progress["cached"]
                done = n + c
            tag = f"{spec[0]} {spec[1]}->{spec[2]} {spec[3]}..{spec[4] or ''}"
            if price is not None:
                consec_fail = 0
                log.info(
                    "[%d/%d] %s: %.0f EUR, %d itineraries, best %s, %.1fs",
                    done,
                    len(all_queries),
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
                    done,
                    len(all_queries),
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
                    time.sleep(pause)
                    consec_fail = 0
                log.warning(
                    "[%d/%d] %s: FAILED after retries (%s), %.1fs",
                    done,
                    len(all_queries),
                    tag,
                    err,
                    elapsed,
                )
            prev = conn.execute(
                "SELECT price, fetched_at FROM price_cache WHERE key=?", (key,)
            ).fetchone()
            conn.execute(
                """INSERT INTO price_cache
                   (key, kind, origin, dest, d1, d2, price, n_results, detail,
                    prev_price, prev_fetched_at, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     price=excluded.price, n_results=excluded.n_results,
                     detail=excluded.detail,
                     prev_price=excluded.prev_price,
                     prev_fetched_at=excluded.prev_fetched_at,
                     fetched_at=excluded.fetched_at""",
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
                ),
            )
            if price is not None:
                conn.execute(
                    "INSERT INTO price_history (key, price, fetched_at) VALUES (?,?,?)",
                    (key, price, fetched),
                )
            conn.commit()

    log.info(
        "scan finished in %.0fs: fetched=%d failed=%d empty=%d cached=%d",
        time.time() - scan_start,
        progress["n"],
        progress["fail"],
        progress["empty"],
        progress["cached"],
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


def load_rows(conn):
    rows = {}
    for r in conn.execute(
        "SELECT kind, origin, dest, d1, d2, price, detail, fetched_at FROM price_cache WHERE price IS NOT NULL"
    ):
        kind, origin, dest, d1, d2, price, detail, fetched_at = r
        d = json.loads(detail) if detail else {}
        d.setdefault("airlines", [])
        d.setdefault("route", "")
        d.setdefault("stops", 0)
        d["price"] = float(price)
        rows[(kind, origin, dest, d1, d2 or "")] = {**d, "fetched_at": fetched_at}
    return rows


def _ss_itineraries(cfg, ss_rows, rt_prices):
    """Synthesize itineraries from Skyscanner deals, merged into the ranking.
    Only deals cheaper than the Google round-trip for the same pair are kept."""
    out = []
    for origin, dest, d1, d2, total_results, deals_json in ss_rows:
        deals = json.loads(deals_json)
        if not deals:
            continue
        g = rt_prices.get((origin, dest, d1, d2))
        for deal in deals[:2]:
            eur = deal.get("eur")
            if eur is None or (g is not None and eur >= g):
                continue
            leg_details = []
            for leg in deal["legs"]:
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
                    }
                )
            while len(leg_details) < 2:
                leg_details.append(None)
            tr_total, tr_items = transfers_for("RT", origin, origin, cfg)
            agent = ", ".join(deal.get("agents", [])[:1])
            out.append(
                {
                    "key": f"SS|{origin}|{dest}|{d1}|{d2}|{deal['price_fmt']}",
                    "kind": "SS",
                    "out_origin": origin,
                    "ret_dest": origin,
                    "in_city": dest,
                    "out_city": dest,
                    "d1": d1,
                    "d2": d2,
                    "airfare": eur,
                    "out_detail": leg_details[0],
                    "ret_detail": leg_details[1],
                    "ret_unavailable": False,
                    "transfers": tr_total,
                    "transfer_items": tr_items,
                    "agent": agent,
                    "link": deal.get("link"),
                }
            )
    for it in out:
        it["total"] = it["airfare"] + it["transfers"]
    return out


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
    for origin in s["origins"]:
        for dest in s["destinations"]:
            for d1 in dep_dates:
                for dur in range(s["trip_min_days"], s["trip_max_days"] + 1):
                    d2 = d1 + timedelta(days=dur)
                    if d2 > end:
                        continue
                    d1s, d2s = d1.isoformat(), d2.isoformat()
                    rt = rows.get(("RT", origin, dest, d1s, d2s))
                    if rt:
                        tr_total, tr_items = transfers_for("RT", origin, dest, cfg)
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
                                "ret_dest": dest,
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
                    for in_city, out_city in [(OSA, TYO), (TYO, OSA)]:
                        for home in s["origins"]:
                            out_leg = rows.get(("OW", origin, in_city, d1s, ""))
                            ret_leg = rows.get(("OW", out_city, home, d2s, ""))
                            if not (out_leg and ret_leg):
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
    itins.sort(key=lambda x: x["total"])
    return itins


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
    shown = itins[:top_n]

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
            return "<td>—</td>"
        if unavailable:
            return (
                "<td><small>return leg details not exposed by API</small><br>"
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
            f"<td>{route_html}{stops_txt}<br>"
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
                        it["out_origin"], it["in_city"], it["d1"], it["d2"], ss_domain
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
            f'<br><small class="muted">via {html.escape(it.get("agent", ""))}</small>'
            if it["kind"] == "SS"
            else ""
        )
        rows_html.append(
            f'<tr data-eur-total="{it["total"]:.2f}" data-i="{idx}" title="click for details">'
            f'<td class="rank">{i}</td><td><span class="{badge_cls}">{kind_label}</span>{agent_html}</td><td>{route_txt}</td>'
            f"<td>{fmt_date(it['d1'])}</td><td>{fmt_date(it['d2'])}</td>"
            f'<td class="num">{(date.fromisoformat(it["d2"]) - date.fromisoformat(it["d1"])).days}</td>'
            f"{leg_cell(o)}"
            f"{leg_cell(r, unavailable=it.get('ret_unavailable', False))}"
            f'<td class="num" data-eur="{it["airfare"]:.0f}">{it["airfare"]:.0f}</td>'
            f'<td class="num" data-eur="{it["transfers"]:.0f}" title="{html.escape(tr_items)}">{it["transfers"]:.0f}</td>'
            f'<td class="num total" data-eur="{it["total"]:.0f}">{it["total"]:.0f}</td>'
            f'<td class="num">{delta}</td><td>{links}</td></tr>'
        )
        prev_total = prev.get(it["key"])
        shown_json.append(
            {
                "kind_label": kind_label,
                "route_txt": route_txt,
                "d1": it["d1"],
                "d2": it["d2"],
                "days": (
                    date.fromisoformat(it["d2"]) - date.fromisoformat(it["d1"])
                ).days,
                "airfare": it["airfare"],
                "transfers": it["transfers"],
                "total": it["total"],
                "prev_total": prev.get(it["key"]),
                "transfer_items": it["transfer_items"],
                "gf_links": gf_links,
                "out": slim(o),
                "ret": slim(r),
                "ret_unavailable": it.get("ret_unavailable", False),
            }
        )

    hist_top = shown[: cfg["scan"]["history_top_n"]]
    chart_labels = []
    chart_series = {it["key"]: [] for it in hist_top}
    series_meta = {it["key"]: it for it in hist_top}
    hist_rows = conn.execute(
        "SELECT run_ts, itin_key, total FROM itinerary_history ORDER BY run_ts"
    ).fetchall()
    by_run = {}
    for ts, k, total in hist_rows:
        by_run.setdefault(ts, {})[k] = total
    all_run_ts = sorted(set([ts for ts, _, _ in hist_rows] + [run_ts]))
    for ts in all_run_ts:
        short = ts[5:16].replace("T", " ")
        chart_labels.append(short)
        for k in chart_series:
            chart_series[k].append(by_run.get(ts, {}).get(k))

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
        d = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        return d.strftime("%d %b %Y, %H:%M UTC")

    def _stats_age(ts):
        if not ts:
            return None
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600

    stats_q = {
        "priced": "SELECT COUNT(*) FROM price_cache WHERE price IS NOT NULL",
        "empty": "SELECT COUNT(*) FROM price_cache WHERE detail LIKE '%no_exact_results%'",
        "by_kind": (
            "SELECT kind, COUNT(*) FROM price_cache WHERE price IS NOT NULL"
            " GROUP BY kind"
        ),
        "hist": "SELECT COUNT(*) FROM price_history",
        "runs": "SELECT COUNT(*) FROM runs",
        "oldest": "SELECT MIN(fetched_at) FROM price_cache WHERE price IS NOT NULL",
        "newest": "SELECT MAX(fetched_at) FROM price_cache WHERE price IS NOT NULL",
    }
    priced = conn.execute(stats_q["priced"]).fetchone()[0]
    empty = conn.execute(stats_q["empty"]).fetchone()[0]
    kinds = dict(conn.execute(stats_q["by_kind"]).fetchall())
    hist = conn.execute(stats_q["hist"]).fetchone()[0]
    runs_n = conn.execute(stats_q["runs"]).fetchone()[0]
    oldest = conn.execute(stats_q["oldest"]).fetchone()[0]
    newest = conn.execute(stats_q["newest"]).fetchone()[0]
    planned = len(plan_queries(cfg))

    kind_txt = " · ".join(f"{kinds[k]:,} {k}" for k in ("RT", "OW") if kinds.get(k))
    newest_age = _stats_age(newest)
    oldest_age = _stats_age(oldest)
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
            f"refreshed within {cfg['cache']['ttl_hours']}h",
        ),
        (f"{hist:,}", "history points", None),
        (f"{runs_n}", "runs recorded", None),
        (
            f"{progress['n']}",
            "fetched this run",
            f"{progress.get('empty', 0)} flexible-only, {progress['fail']} failed",
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
    html_doc = html_doc.replace(
        "__CARDS__",
        card("Cheapest overall", itins[0] if itins else None, best=True)
        + card("Cheapest round trip", best_rt)
        + card("Cheapest open jaw", best_oj),
    )
    html_doc = html_doc.replace("__STATS__", stats_html)
    html_doc = html_doc.replace("__ROWS__", "\n".join(rows_html))
    html_doc = html_doc.replace("__ITINS__", json.dumps(shown_json, ensure_ascii=False))
    html_doc = html_doc.replace("__CHART_DATA__", json.dumps(chart_data))
    html_doc = html_doc.replace("__BEST_CHART__", json.dumps(best_chart))
    html_doc = html_doc.replace(
        "__META__",
        f"Generated {fmt_run_ts(run_ts)} · per-person prices · "
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
 <p class="foot">Prices per person, 1-adult query, checked bag included, max 2 stops.
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
function redrawSort() {
  const tb = document.querySelector('#tbl tbody');
  const rows = [...tb.querySelectorAll('tr')];
  rows.sort((a, b) => {
    let va = a.children[sortKey].innerText.trim(), vb = b.children[sortKey].innerText.trim();
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
}
function sortBy(th) {
  const k = parseInt(th.dataset.k);
  if (sortKey === k) sortAsc = !sortAsc; else { sortKey = k; sortAsc = k === 0; }
  redrawSort();
}
function setCur(c) { cur = c; apply(); }
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
    ['Airfare', fmt(it.airfare)],
    ...it.transfer_items.map(([n, v]) => [esc(n), fmt(v)]),
  ];
  const costs = rows.map(([n, v]) =>
    `<tr><td>${n}</td><td>${v}</td></tr>`).join('');
  const links = it.gf_links
    .map(([name, u]) => `<a href="${u}" target="_blank" rel="noopener">${esc(name)}</a>`)
    .join('');
  document.getElementById('dlg-body').innerHTML = `
    <div class="dlg-dates">${fdate(it.d1)} → ${fdate(it.d2)} · ${it.days} days · price per person</div>
    ${legHtml(it.out, 'Outbound')}
    ${legHtml(it.ret, 'Return', it.ret_unavailable)}
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
    rows = load_rows(conn)
    ss_rows = conn.execute(
        "SELECT origin, dest, d1, d2, total_results, deals_json FROM skyscanner_prices"
        " ORDER BY fetched_at DESC"
    ).fetchall()
    itins = build_itineraries(cfg, rows, ss_rows)
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
    rows = load_rows(conn)
    itins = build_itineraries(cfg, rows)
    label_itins(itins)
    run_skyscanner_if_due(cfg, conn, args, itins)
    ss_rows = conn.execute(
        "SELECT origin, dest, d1, d2, total_results, deals_json FROM skyscanner_prices"
        " ORDER BY fetched_at DESC"
    ).fetchall()
    rows = load_rows(conn)
    itins = build_itineraries(cfg, rows, ss_rows)
    label_itins(itins)
    prev, prev_ts = prev_totals(conn)
    log.info(
        "ranked %d itineraries (prev run for deltas: %s)",
        len(itins),
        prev_ts or "none",
    )

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
