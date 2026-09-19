from __future__ import annotations

import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

from fast_flights import FlightQuery, Passengers, create_query
from primp import Client

from .config import (
    CACHE_VERSION,
    CITIES,
    HTTP_TIMEOUT_SECONDS,
    SOCS_COOKIE,
    log,
    now_iso,
)


class ReturnValidationError(RuntimeError):
    pass


class TooLongError(RuntimeError):
    """Every returned itinerary exceeds the configured max leg duration —
    a valid (confirmed) Google response that just has no bookable option."""


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
    dep_dt = datetime(*segs[0]["dep_d"], *segs[0]["dep_t"], tzinfo=UTC)
    arr_dt = datetime(*segs[-1]["arr_d"], *segs[-1]["arr_t"], tzinfo=UTC)
    total_min = sum(s["dur_min"] for s in segs)
    for a, b in pairwise(segs):
        lay = (
            datetime(*b["dep_d"], *b["dep_t"], tzinfo=UTC)
            - datetime(*a["arr_d"], *a["arr_t"], tzinfo=UTC)
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
    try:
        start = js.index("data:") + len("data:")
    except ValueError:
        raise RuntimeError("no data payload in ds:1 script") from None
    # raw_decode stops at the end of the first JSON value; some page variants
    # append metadata (sideFreebird, errorHasStatus, ...) after it, which
    # breaks a plain json.loads with "Extra data".
    payload, end = json.JSONDecoder().raw_decode(js, start)
    if "errorHasStatus" in js[end:]:
        log.debug("google error-status page (treated as no-exact-results)")
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
        raise TooLongError(f"all {len(itins)} itineraries exceed {max_hours}h")
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
            confirmed = row[2] is not None and (
                "no_exact_results" in row[2] or "too_long" in row[2]
            )
            age = (
                now - datetime.fromisoformat(row[1]).timestamp()
            )
            if age > ttl or (row[0] is None and not confirmed):
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
        "too_long": 0,
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
            too_long = False
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
                except TooLongError as e:
                    # Confirmed Google response: every returned itinerary is
                    # longer than the ranking limit. Record it so the row is
                    # TTL-refreshed like a confirmed-empty one instead of
                    # being retried every run, and never count it toward the
                    # throttling cooldown.
                    err = None
                    too_long = True
                    price = None
                    n_results = 0
                    detail = json.dumps({"too_long": True, "note": str(e)})
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
            elif too_long:
                consec_fail = 0
                with lock:
                    progress["too_long"] += 1
                max_hours = cfg.get("ranking", {}).get("max_leg_hours", 0)
                log.info(
                    "[%d/%d] %s: all returned itineraries exceed %dh (recorded), %.1fs",
                    n,
                    len(todo),
                    tag,
                    max_hours,
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
        "scan finished in %.0fs: fetched=%d failed=%d empty=%d too_long=%d "
        "fresh=%d deferred=%d",
        time.time() - scan_start,
        progress["n"],
        progress["fail"],
        progress["empty"],
        progress["too_long"],
        progress["cached"],
        progress["deferred"],
    )
    return progress
