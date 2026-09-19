from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta

from .bags import bag_fees_for_legs
from .config import CACHE_VERSION, CITIES, OSA, TYO, VIE, _age_hours, log
from .google import key_of
from .skyscanner import parse_ss_key


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
        "SELECT key, origin, dest, d1, d2, total_results, deals_json, "
        "fetched_at, adults, currency FROM skyscanner_prices WHERE key LIKE ? "
        "ORDER BY fetched_at DESC",
        (prefix,),
    ).fetchall()


def _ss_itineraries(cfg, ss_rows):
    """Synthesize itineraries from Skyscanner deals, merged into the ranking.
    Ranking is independent of Google: deals are kept even without a matching
    Google round-trip (OTA/self-transfer fares often undercut it invisibly).
    Rows older than indicative_after_hours stay visible but are flagged
    'indicative'; rows older than max_age_hours are dropped. Both RT rows
    (round-trip searches) and OJ rows (true multi-city open-jaw searches,
    recognized by the OJ marker in the storage key) are handled."""
    out = []
    s = cfg["search"]
    sk_cfg = cfg.get("skyscanner", {})
    max_hours = cfg.get("ranking", {}).get("max_leg_hours", 0)
    # OTA deals with longer legs than the Google cap are still stored and
    # shown (their durations render red); the browser's max-leg filter
    # (default 24h) hides them so they can be found manually.
    display_hours = cfg.get("ranking", {}).get("max_leg_hours_display", 0)
    leg_cap = max(filter(None, (max_hours, display_hours)), default=0)
    max_age = sk_cfg.get("max_age_hours", 168)
    indicative_after = sk_cfg.get("indicative_after_hours", 24)
    eligible_per_pair = sk_cfg.get("eligible_deals", 5)
    start = date.fromisoformat(s["date_start"])
    end = date.fromisoformat(s["date_end"])
    oj_city_pairs = {(TYO, OSA), (OSA, TYO)}
    for (
        key,
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
        combo = parse_ss_key(key)
        if combo is None:
            continue
        if combo[0] == "OJ":
            _k, out_origin, in_city, out_city, ret_dest = combo[:5]
            if (in_city, out_city) not in oj_city_pairs:
                continue
        else:
            out_origin = ret_dest = combo[1]
            in_city = out_city = combo[2]
        dep_date = date.fromisoformat(d1)
        ret_date = date.fromisoformat(d2)
        duration = (ret_date - dep_date).days
        age = _age_hours(fetched_at)
        if (
            adults != s.get("adults", 2)
            or out_origin not in s["origins"]
            or ret_dest not in s["origins"]
            or not start <= dep_date < ret_date <= end
            or not s["trip_min_days"] <= duration <= s["trip_max_days"]
            or age is None
            or age > max_age
        ):
            continue
        deals = json.loads(deals_json)
        if not deals:
            continue
        # leg 1 must start at the departure city and end inside the inbound
        # Japan city; leg 2 must start inside the outbound Japan city and end
        # at the final return city (same as the departure city for RT rows).
        valid_in = {in_city, *CITIES.get(in_city, set())}
        valid_out = {out_city, *CITIES.get(out_city, set())}
        accepted_keys = set()
        for deal in deals:
            eur = deal.get("eur")
            legs = deal.get("legs") or []
            if (
                eur is None
                or len(legs) != 2
                or legs[0].get("from") != out_origin
                or legs[0].get("to") not in valid_in
                or (legs[0].get("dep") or "")[:10] != d1
                or legs[1].get("from") not in valid_out
                or legs[1].get("to") != ret_dest
                or (legs[1].get("dep") or "")[:10] != d2
                or any((leg.get("stops") or 0) > s["max_stops"] for leg in legs)
                or any(
                    leg_cap and leg.get("dur_min") and leg["dur_min"] / 60 > leg_cap
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
            kind = "OJ" if combo[0] == "OJ" else "RT"
            tr_total, tr_items = transfers_for(kind, out_origin, ret_dest, cfg)
            agent = ", ".join(deal.get("agents", [])[:1])
            identity = json.dumps(
                {
                    "route": [out_origin, in_city, out_city, ret_dest, d1, d2],
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
            if combo[0] == "OJ":
                itinerary_key = (
                    f"SS|OJ|{out_origin}|{in_city}|{out_city}|{ret_dest}"
                    f"|{d1}|{d2}|{digest}"
                )
            else:
                itinerary_key = f"SS|{out_origin}|{in_city}|{d1}|{d2}|{digest}"
            bag_pp, bag_items, bag_inc = bag_fees_for_legs(
                [
                    ("outbound", legs[0].get("carriers") or []),
                    (
                        "return",
                        (legs[1].get("carriers") or []) if len(legs) > 1 else [],
                    ),
                ],
                s.get("adults", 2),
            )
            out.append(
                {
                    "key": itinerary_key,
                    "kind": "SS",
                    "ss_oj": combo[0] == "OJ",
                    "out_origin": out_origin,
                    "ret_dest": ret_dest,
                    "in_city": in_city,
                    "out_city": out_city,
                    "d1": d1,
                    "d2": d2,
                    "airfare": eur + bag_pp,
                    "fare_base": eur,
                    "ota_base_fare": eur,
                    "bag_fee_pp": bag_pp,
                    "bag_items": bag_items,
                    "out_detail": leg_details[0],
                    "ret_detail": leg_details[1],
                    "ret_unavailable": False,
                    "transfers": tr_total,
                    "transfer_items": tr_items,
                    "agent": agent,
                    "link": deal.get("link"),
                    "self_transfer": bool(deal.get("self_transfer")),
                    "protected": bool(deal.get("protected")),
                    "bag_included": bag_inc,
                    "indicative": age > indicative_after,
                    "ss_age_hours": round(age, 1),
                    "fetched_at": fetched_at,
                }
            )
            accepted_keys.add(itinerary_key)
            if len(accepted_keys) == eligible_per_pair:
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
                        # Return carriers unknown -> mirror the outbound's
                        # (same ticket, same airline bag policy).
                        ret_carriers = (ret_detail or rt).get("airlines") or []
                        bag_pp, bag_items, _bag_inc = bag_fees_for_legs(
                            [
                                ("outbound", rt.get("airlines") or []),
                                ("return", ret_carriers),
                            ],
                            s.get("adults", 2),
                        )
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
                                "airfare": rt["price"] + bag_pp,
                                "fare_base": rt["price"],
                                "bag_fee_pp": bag_pp,
                                "bag_items": bag_items,
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
                        bag_pp, bag_items, _bag_inc = bag_fees_for_legs(
                            [
                                ("outbound", out_leg.get("airlines") or []),
                                ("return", ret_leg.get("airlines") or []),
                            ],
                            s.get("adults", 2),
                        )
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
                                "airfare": out_leg["price"] + ret_leg["price"] + bag_pp,
                                "fare_base": out_leg["price"] + ret_leg["price"],
                                "bag_fee_pp": bag_pp,
                                "bag_items": bag_items,
                                "out_detail": out_leg,
                                "ret_detail": ret_leg,
                                "transfers": tr_total,
                                "transfer_items": tr_items,
                            }
                        )
    for it in itins:
        it["total"] = it["airfare"] + it["transfers"]
    if ss_rows:
        itins.extend(_ss_itineraries(cfg, ss_rows))
    keys = [it["key"] for it in itins]
    if len(keys) != len(set(keys)):
        raise RuntimeError("duplicate itinerary keys generated")
    itins.sort(key=lambda x: x["total"])
    return itins


def build_with_optional_skyscanner(cfg, conn, rows):
    google_itins = build_itineraries(cfg, rows)
    try:
        return build_itineraries(cfg, rows, load_ss_rows(conn, cfg))
    except Exception:  # noqa: BLE001 - render Google-only results instead
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


def label_itins(itins):
    labels = {"RT": "Round trip", "OJ": "Open jaw", "SS": "Skyscanner RT"}
    for it in itins:
        direction = f"{it['in_city']}/{it['out_city']}"
        label = labels[it["kind"]]
        if it["kind"] == "SS" and it.get("ss_oj"):
            label = "Skyscanner OJ"
        it["label"] = (
            f"{label} {it['out_origin']}→{it['ret_dest']} "
            f"(via {direction}) {it['d1']} → {it['d2']}"
        )
