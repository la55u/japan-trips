from __future__ import annotations

import json
import random
import time
from datetime import date, timedelta
from urllib.parse import urlencode as _urlencode

from .config import CACHE_VERSION, OSA, TYO, _age_hours, log, now_iso


def _ss_url(origin, dest, d1, d2, domain, adults=2):
    fmt = lambda d: d.replace("-", "")[2:]
    return (
        f"https://www.{domain}/transport/flights/"
        f"{origin.lower()}/{dest.lower()}a/{fmt(d1)}/{fmt(d2)}/"
        f"?adultsv2={adults}&cabinclass=economy&rtn=1"
    )


def _ss_multicity_url(legs, domain, adults=2):
    """Skyscanner multi-city deep link (official referrals schema:
    origin0/destination0/date0/origin1/... query params, YYYY-MM-DD dates)."""
    params = [("adultsv2", adults), ("cabinclass", "economy")]
    for i, (origin, dest, d) in enumerate(legs):
        params += [(f"origin{i}", origin), (f"destination{i}", dest), (f"date{i}", d)]
    return f"https://www.{domain}/transport/flights/multicity?" + _urlencode(params)


def combo_legs(combo):
    """Combo (tagged tuple) -> [(from, to, date), ...] for the search legs."""
    if combo[0] == "OJ":
        _, oo, ic, oc, h, d1, d2 = combo
        return [(oo, ic, d1), (oc, h, d2)]
    _, o, d, d1, d2 = combo
    return [(o, d, d1), (d, o, d2)]


def combo_key(combo, state_suffix):
    """Storage/attempt key for a tagged combo. RT keys keep the historical
    format `vN_a|origin|dest|d1|d2`; OJ keys add an explicit OJ segment plus
    all four cities: `vN_a|OJ|out_origin|in_city|out_city|home|d1|d2`."""
    if combo[0] == "OJ":
        _, oo, ic, oc, h, d1, d2 = combo
        return f"{state_suffix}|OJ|{oo}|{ic}|{oc}|{h}|{d1}|{d2}"
    _, o, d, d1, d2 = combo
    return f"{state_suffix}|{o}|{d}|{d1}|{d2}"


def parse_ss_key(key):
    """Parse a skyscanner_prices/skyscanner_attempts key into a tagged combo,
    or None if the shape is unknown (legacy/garbage rows). The leading
    version/adults prefix segment is dropped."""
    parts = key.split("|")
    if len(parts) == 5:
        return ("RT", *parts[1:])
    if len(parts) == 8 and parts[1] == "OJ":
        return ("OJ", parts[2], parts[3], parts[4], parts[5], parts[6], parts[7])
    return None


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
  let it = data.itineraries || {};
  let results = it.results || [];
  // Results can stream in across poll snapshots; a complete-but-empty
  // response gets a few extra polls before giving up.
  let extra = 0;
  while (!results.length && extra < 5) {
    await new Promise(res => setTimeout(res, 2000));
    const poll2 = await fetch(
      '/g/radar/api/v2/web-unified-search/' + encodeURIComponent(searchCtx.sessionId),
      { method: 'GET', headers: pollHeaders, credentials: 'include' });
    if (poll2.status !== 200) break;
    data = await poll2.json();
    it = data.itineraries || {};
    results = it.results || [];
    extra++;
  }
  const itCtx = it.context || {};
  const agents = {};
  for (const a of (it.agents || [])) agents[a.id] = a.name || a.id;
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
    """Build the web-unified-search POST body for a round-trip airport pair.
    Place codes are Skyscanner 'a'-suffixed city codes (bud/tyoa) in URLs but
    the API body uses entityIds. Returns None if an entityId is unknown."""
    return _ss_fast_payload_legs([(origin, dest, d1), (dest, origin, d2)], adults)


def _ss_fast_payload_legs(legs, adults=2):
    """Build the web-unified-search POST body for arbitrary 2-leg searches
    (round trip or open jaw: leg routes are independent). Returns None if any
    entityId is unknown."""
    body_legs = []
    for i, (origin, dest, d) in enumerate(legs):
        eo = _SS_ENTITY_IDS.get(origin.upper())
        ed = _SS_ENTITY_IDS.get(dest.upper())
        if not eo or not ed:
            return None
        y, m, dd = d.split("-")
        leg = {
            "legOrigin": {"@type": "entity", "entityId": eo},
            "legDestination": {"@type": "entity", "entityId": ed},
            "dates": {"@type": "date", "year": y, "month": m, "day": dd},
        }
        # Mirror the site's own RT payload: only the first leg carries
        # placeOfStay (unknown whether multi-city legs behave differently).
        if i == 0:
            leg["placeOfStay"] = ed
        body_legs.append(leg)
    return {
        "cabinClass": "ECONOMY",
        "childAges": [],
        "adults": adults,
        "legs": body_legs,
    }


def skyscanner_spotcheck(cfg, conn, combos):
    """Spot-check Skyscanner (OTA / self-transfer prices) for selected
    route/date combos via a camoufox browser. Returns rows to store.

    Combos are tagged tuples: ("RT", origin, dest, d1, d2) round trips and
    ("OJ", out_origin, in_city, out_city, home, d1, d2) open jaws (true
    multi-city searches against the same web-unified-search API).

    Two fetch paths per combo:
    - fast: reuse the live page session and POST the unified-search API from
      inside the page (fetch) — ~1-4s per combo; needs known entityIds and a
      captured bootstrap request (for headers). Re-bootstraps the page every
      `fast_rebootstrap` fast queries; on failure falls back to navigation.
    - slow: navigate the SPA to the combo's search URL and capture the
      response XHR (original behavior). RT only — the multi-city SPA flow has
      no reliable deep-link URL, so OJ combos are fast-path only.
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

        def _bootstrap_url(combo):
            """A plain RT search URL used to bootstrap/re-bootstrap the SPA.
            The captured request headers are generic across search kinds, so
            OJ combos bootstrap on their outbound leg's RT route."""
            (o, d, d1), (_o2, _d2c, d2) = combo_legs(combo)
            return _ss_url(o, d, d1, d2, domain, adults)

        boot_req = _bootstrap(_bootstrap_url(combos[0]))
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
                boot_req = _bootstrap(_bootstrap_url(combo))
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

        for combo in combos:
            kind = combo[0]
            d1, d2 = combo[-2], combo[-1]
            desc = (
                f"{combo[1]}->{combo[2]}+{combo[3]}->{combo[4]}"
                if kind == "OJ"
                else f"{combo[1]}->{combo[2]}"
            )
            oj_retries = 0
            while True:
                if needs_reboot:
                    _rebootstrap(combo)
                done = False
                payload = None
                # --- fast path: in-page API fetch ---
                if fast_mode:
                    payload = _ss_fast_payload_legs(combo_legs(combo), adults)
                    if payload:
                        try:
                            out = pg.evaluate(
                                _SS_FETCH_JS,
                                [payload, fast_headers, top_deals, currency],
                            )
                            if out.get("http") == 200 and out.get("deals"):
                                total, deals = _parse_fast(out)
                                rows.append((kind, *combo[1:], total, deals))
                                log.info(
                                    "skyscanner(fast) %s %s %s..%s: %d results, top %s",
                                    kind,
                                    desc,
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
                                    "skyscanner(fast) %s %s %s..%s: http=%s err=%s",
                                    kind,
                                    desc,
                                    d1,
                                    d2,
                                    out.get("http"),
                                    str(out.get("err"))[:40],
                                )
                        except Exception as e:  # noqa: BLE001 - fall back to navigation
                            log.warning(
                                "skyscanner(fast) %s %s failed: %s",
                                kind,
                                desc,
                                str(e)[:120],
                            )
                        since_boot += 1
                        if done and since_boot >= rebootstrap_every:
                            _rebootstrap(combo)
                if done:
                    time.sleep(random.uniform(1, 2))
                    break
                # a fast attempt was made and failed: schedule a re-bootstrap
                # so the next combo starts from a fresh session
                if fast_mode and payload:
                    needs_reboot = True
                if kind == "OJ":
                    # No reliable multi-city deep-link URL for the SPA slow
                    # path; OJ combos are fast-path only. Fast POSTs fired
                    # right after a bootstrap are sometimes 403'd, so retry
                    # once from a fresh session before giving up.
                    if oj_retries < 1:
                        oj_retries += 1
                        time.sleep(random.uniform(2, 4))
                        continue
                    log.warning(
                        "skyscanner OJ %s %s..%s: fast path failed after "
                        "retry, skipped (stays due)",
                        desc,
                        d1,
                        d2,
                    )
                    break
                origin, dest, _d1, _d2 = combo[1:]
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
                        rows.append(("RT", origin, dest, d1, d2, total, deals))
                        log.info(
                            "skyscanner(slow) RT %s->%s %s..%s: %d results, top %s",
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
                            "skyscanner(slow) RT %s->%s %s..%s: no results captured",
                            origin,
                            dest,
                            d1,
                            d2,
                        )
                except Exception as e:  # noqa: BLE001 - isolate each source query
                    log.warning(
                        "skyscanner(slow) RT %s->%s failed: %s",
                        origin,
                        dest,
                        str(e)[:120],
                    )
                time.sleep(random.uniform(5, 10))
                break
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


def _ss_universe(cfg):
    """Every RT route/date combination Skyscanner could ever check — the same
    enumeration as the RT part of plan_queries. Returns untagged
    (origin, dest, d1, d2) tuples (callers tag them)."""
    s = cfg["search"]
    start = date.fromisoformat(s["date_start"])
    end = date.fromisoformat(s["date_end"])
    step = s.get("step_days", 1)
    out = []
    d = start
    while d <= end - timedelta(days=s["trip_min_days"]):
        for origin in s["origins"]:
            for dest in s["destinations"]:
                for dur in range(s["trip_min_days"], s["trip_max_days"] + 1):
                    d2 = d + timedelta(days=dur)
                    if d2 > end:
                        continue
                    out.append((origin, dest, d.isoformat(), d2.isoformat()))
        d += timedelta(days=step)
    return out


def _ss_oj_universe(cfg):
    """Every open-jaw route/date combination: outbound home1→in_city on d1,
    return out_city→home2 on d2 with 12–16 day spacing. Both Japan directions
    (TYO in/OSA out and OSA in/TYO out), both origins independently as
    departure and return city (mixed VIE/BUD trips included). Pure RT shapes
    are impossible here because in_city != out_city always. Returns tagged
    ("OJ", out_origin, in_city, out_city, home, d1, d2) tuples."""
    s = cfg["search"]
    start = date.fromisoformat(s["date_start"])
    end = date.fromisoformat(s["date_end"])
    step = s.get("step_days", 1)
    out = []
    d = start
    while d <= end - timedelta(days=s["trip_min_days"]):
        for oo in s["origins"]:
            for h in s["origins"]:
                for ic, oc in ((TYO, OSA), (OSA, TYO)):
                    for dur in range(s["trip_min_days"], s["trip_max_days"] + 1):
                        d2 = d + timedelta(days=dur)
                        if d2 > end:
                            continue
                        out.append(("OJ", oo, ic, oc, h, d.isoformat(), d2.isoformat()))
        d += timedelta(days=step)
    return out


def _travelpayouts_cheap_pairs(cfg):
    """Optional discovery booster: query the Travelpayouts Data API
    (aviasales v3 prices_for_dates, i.e. cached calendar prices) and return
    {(origin, dest, d1, d2): price_eur} for pairs inside the scan window.
    Cached data is NOT reliable as a final live price — it is only used to
    prioritize Skyscanner exploration checks. Never raises."""
    tp = cfg.get("travelpayouts", {})
    if not tp.get("enabled") or not tp.get("token"):
        return {}
    s = cfg["search"]
    start = date.fromisoformat(s["date_start"])
    end = date.fromisoformat(s["date_end"])
    result = {}
    import urllib.parse
    import urllib.request

    for origin in s["origins"]:
        for dest in s["destinations"]:
            params = {
                "origin": origin,
                "destination": dest,
                "currency": "eur",
                "sorting": "price",
                "direct": "false",
                "limit": 30,
                "one_way": "false",
                "token": tp["token"],
            }
            url = (
                "https://api.travelpayouts.com/aviasales/v3/prices_for_dates?"
                + urllib.parse.urlencode(params)
            )
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    data = json.load(resp)
                for entry in data.get("data", []):
                    d1 = str(entry.get("depart_date", ""))[:10]
                    d2 = str(entry.get("return_date") or "")[:10]
                    price = entry.get("price")
                    if not d1 or not d2 or not price:
                        continue
                    try:
                        dep = date.fromisoformat(d1)
                        ret = date.fromisoformat(d2)
                    except ValueError:
                        continue
                    if not (
                        start <= dep < ret <= end
                        and s["trip_min_days"] <= (ret - dep).days <= s["trip_max_days"]
                    ):
                        continue
                    key = (origin, dest, d1, d2)
                    prev = result.get(key)
                    if prev is None or price < prev:
                        result[key] = float(price)
            except Exception as e:  # noqa: BLE001 - discovery source is best effort
                log.warning(
                    "travelpayouts %s->%s failed: %s", origin, dest, str(e)[:120]
                )
    if result:
        log.info(
            "travelpayouts calendar shortlisted %d promising date pairs", len(result)
        )
    return result


def _select_exploration(due, n):
    """Pick `n` exploration combos oldest-successful-first with round-robin
    quotas per route and per trip duration. `due` is a list of
    (sort_key, combo) already ordered so that earlier entries are preferred.
    Combos are tagged tuples; route = all city segments, duration from the
    trailing date pair."""
    by_route_dur = {}
    for sort_key, combo in due:
        route = tuple(combo[1:-2])
        dur = (date.fromisoformat(combo[-1]) - date.fromisoformat(combo[-2])).days
        by_route_dur.setdefault((route, dur), []).append((sort_key, combo))
    for lst in by_route_dur.values():
        lst.sort()
    picked = []
    cells = sorted(by_route_dur)
    idx = 0
    while len(picked) < n and cells:
        route, dur = cells[idx % len(cells)]
        bucket = by_route_dur[(route, dur)]
        if bucket:
            picked.append(bucket.pop(0)[1])
        if not bucket:
            cells.pop(idx % len(cells))
            if not cells:
                break
            continue
        idx += 1
    return picked


def run_skyscanner_if_due(cfg, conn, args, itins):
    """Run the Skyscanner spot-check when due; store results in the DB.

    Tiered scheduling with per-combination refresh times (skyscanner_attempts
    last_success_at), run at most every min_age_hours:
    - hot: refresh stored winners (cheapest deals) older than hot_refresh_hours
    - neighbours: ±1..3-day shifts around stored deals that beat Google by >100
    - exploration: unseen pairs first, then oldest successful check, balanced
      across routes and trip durations; Travelpayouts calendar pairs (if
      configured) are prioritized within each bucket."""
    sk_cfg = cfg.get("skyscanner", {})
    if not sk_cfg.get("enabled", False):
        return
    if args.no_skyscanner or getattr(args, "rank_only", False):
        return
    s = cfg["search"]
    adults = s.get("adults", 2)
    state_suffix = f"v{CACHE_VERSION}_{adults}"
    last_run_key = f"skyscanner_last_run_{state_suffix}"
    last = conn.execute(
        "SELECT value FROM state WHERE key=?", (last_run_key,)
    ).fetchone()
    if last:
        age = _age_hours(last[0])
        if age is not None and age < sk_cfg.get("min_age_hours", 3):
            log.info(
                "skyscanner spot-check skipped (last run %.1fh ago, min %dh)",
                age,
                sk_cfg.get("min_age_hours", 3),
            )
            return

    n_hot = sk_cfg.get("hot_combos", 15)
    n_explore = sk_cfg.get("explore_combos", 25)
    n_neighbour = sk_cfg.get("neighbour_combos", 10)
    hot_refresh = sk_cfg.get("hot_refresh_hours", 18)
    explore_refresh = sk_cfg.get("explore_refresh_hours", 120)

    attempts = {}
    for key, last_success, last_attempt in conn.execute(
        "SELECT key, last_success_at, last_attempt_at FROM skyscanner_attempts "
        "WHERE key LIKE ?",
        (f"{state_suffix}|%",),
    ):
        combo = parse_ss_key(key)
        if combo is not None:
            attempts[combo] = (last_success, last_attempt)

    def _combo(it):
        return (it["out_origin"], it["in_city"], it["d1"], it["d2"])

    rt_map = {}
    for it in itins:
        key = _combo(it)
        if it["kind"] == "RT":
            rt_map.setdefault(key, it["airfare"])

    valid_rt_routes = {(o, d) for o in s["origins"] for d in s["destinations"]}
    oj_city_pairs = {(TYO, OSA), (OSA, TYO)}

    def _valid_stored(combo):
        if combo[0] == "OJ":
            _, oo, ic, oc, h, _d1, _d2 = combo
            return (
                oo in s["origins"] and h in s["origins"] and (ic, oc) in oj_city_pairs
            )
        _, o, d, _d1, _d2 = combo
        return (o, d) in valid_rt_routes

    stored = []
    for row in conn.execute(
        "SELECT key, deals_json, fetched_at FROM skyscanner_prices "
        "WHERE adults=? AND deals_json IS NOT NULL",
        (adults,),
    ):
        key, deals_json, fetched_at = row
        combo = parse_ss_key(key)
        if combo is None or not _valid_stored(combo):
            continue
        try:
            deals = json.loads(deals_json)
        except (TypeError, ValueError):
            continue
        eurs = [d.get("eur") for d in deals if isinstance(d, dict) and d.get("eur")]
        if eurs:
            stored.append(
                {
                    "combo": combo,
                    "kind": combo[0],
                    "best_eur": min(eurs),
                    "age": _age_hours(fetched_at),
                }
            )

    combos = []
    seen = set()

    # --- hot tier: keep current winners and displayed deals fresh ---
    hot_due = [r for r in stored if r["age"] is None or r["age"] >= hot_refresh]
    hot_due.sort(key=lambda r: (r["best_eur"], r["age"] or 1e9))
    for r in hot_due[:n_hot]:
        combos.append(r["combo"])
        seen.add(r["combo"])

    # --- neighbour tier: ±1..3 days around deals that strongly beat Google
    # (RT only: the gap is defined against the Google RT fare map) ---
    strong = []
    for r in stored:
        if r["kind"] != "RT":
            continue
        gap = rt_map.get(r["combo"][1:])
        if gap is not None and gap - r["best_eur"] > 100:
            strong.append((gap - r["best_eur"], r))
    strong.sort(key=lambda pair: pair[0], reverse=True)
    n_neighbour_taken = 0
    for _gap, r in strong:
        if n_neighbour_taken >= n_neighbour:
            break
        d1d = date.fromisoformat(r["combo"][3])
        d2d = date.fromisoformat(r["combo"][4])
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
                "RT",
                r["combo"][1],
                r["combo"][2],
                shifted_d1.isoformat(),
                shifted_d2.isoformat(),
            )
            if candidate in seen:
                continue
            prev_attempt = attempts.get(candidate, (None, None))[1]
            if prev_attempt and (_age_hours(prev_attempt) or 0) < explore_refresh:
                continue
            seen.add(candidate)
            combos.append(candidate)
            n_neighbour_taken += 1
            break

    # --- exploration tier: unseen first, then oldest successful check.
    # RT and OJ universes get separate quotas so OJ coverage cannot starve
    # the (smaller) RT universe or vice versa. ---
    tp_pairs = _travelpayouts_cheap_pairs(cfg)
    n_oj_explore = (
        sk_cfg.get("explore_oj_combos", 10) if sk_cfg.get("oj_enabled", True) else 0
    )

    def _due_for(universe):
        out = []
        for combo in universe:
            if combo in seen:
                continue
            last_success, _last_attempt = attempts.get(combo, (None, None))
            if last_success:
                age = _age_hours(last_success)
                if age is not None and age < explore_refresh:
                    continue
                sort_key = (1, last_success, combo)
            else:
                sort_key = (0, "", combo)
            if combo[0] == "RT" and combo[1:] in tp_pairs:
                sort_key = (0, f"{tp_pairs[combo[1:]]:010.2f}", combo)
            out.append((sort_key, combo))
        return out

    universe = [("RT", *c) for c in _ss_universe(cfg)]
    combos.extend(_select_exploration(_due_for(universe), n_explore))
    if n_oj_explore:
        oj_universe = _ss_oj_universe(cfg)
        combos.extend(_select_exploration(_due_for(oj_universe), n_oj_explore))

    n_hot_taken = min(len(hot_due), n_hot)
    n_explore_taken = max(0, len(combos) - n_hot_taken - n_neighbour_taken)
    if not combos:
        log.info("skyscanner: nothing due (hot/neighbours/exploration all fresh)")
        return
    log.info(
        "skyscanner combos: %d hot + %d neighbour + %d exploration"
        " (per-combo refresh: hot %dh, exploration %dh; TP boost: %d)",
        n_hot_taken,
        n_neighbour_taken,
        n_explore_taken,
        hot_refresh,
        explore_refresh,
        sum(1 for c in combos if c[0] == "RT" and c[1:] in tp_pairs),
    )
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
    for kind, *cities, total, deals in rows:
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
            normalized_rows.append((kind, *cities, total, slim, row_currency))
        else:
            log.warning(
                "skyscanner %s %s %s..%s had no valid currency/price rows",
                kind,
                "+".join(cities[:-2]),
                cities[-2],
                cities[-1],
            )
    rows = normalized_rows
    successful = {tuple(r[: len(r) - 3]) for r in rows}
    for combo in combos:
        attempt_key = combo_key(combo, state_suffix)
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

    for row in rows:
        kind = row[0]
        if kind == "OJ":
            _kind, oo, ic, oc, h, d1, d2, total, slim, row_currency = row
            combo = ("OJ", oo, ic, oc, h, d1, d2)
            col_origin, col_dest = oo, h
        else:
            _kind, o, d, d1, d2, total, slim, row_currency = row
            combo = ("RT", o, d, d1, d2)
            col_origin, col_dest = o, d
        key = combo_key(combo, state_suffix)
        conn.execute(
            "INSERT INTO skyscanner_prices (key, origin, dest, d1, d2, total_results, deals_json, fetched_at, adults, currency)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET total_results=excluded.total_results,"
            " deals_json=excluded.deals_json, fetched_at=excluded.fetched_at,"
            " adults=excluded.adults, currency=excluded.currency",
            (
                key,
                col_origin,
                col_dest,
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
    conn.commit()
    log.info("skyscanner spot-check stored %d combos", len(rows))
