from __future__ import annotations

import argparse
import html
import json
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


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args():
    p = argparse.ArgumentParser(
        description="Scan BUD/VIE <-> Tokyo/Osaka flight deals."
    )
    p.add_argument("--config", default=str(BASE / "config.toml"))
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
    p.add_argument("--out", default=str(BASE / "results.html"))
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
    return resp.text


def _clock(pair):
    a = pair or []
    padded = [*(a or []), None, None]
    return (padded[0] or 0, padded[1] or 0)


def parse_payload(html_text):
    m = re.search(r"<script[^>]*ds:1[^>]*>(.*?)</script>", html_text, re.S)
    if not m:
        raise RuntimeError("no data script found in page")
    js = m.group(1)
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise RuntimeError("google returned an error status")
    payload = json.loads(data)
    if payload[3][0] is None:
        return []
    itins = []
    for k in payload[3][0]:
        flight = k[0]
        p = k[1][0] if k[1] else None
        if not isinstance(p, list) or len(p) < 2 or p[1] is None:
            continue
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
            continue
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
        itins.append(
            {
                "price": price,
                "airlines": airlines,
                "route": " -> ".join(codes),
                "stops": len(segs) - 1,
                "dep": dep_dt.strftime("%H:%M"),
                "arr": arr_dt.strftime("%H:%M"),
                "plus": plus,
                "dur_h": round(total_min / 60, 1),
                "airports": airports,
            }
        )
    return itins


def fetch_with_retry(q, attempts=3):
    last = None
    for i in range(attempts):
        try:
            res = parse_payload(fetch_html(q))
            if res:
                return res, None
            last = "no results"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(2 + i * 3 + random.random() * 2)
    return None, last


def summarize(itins):
    best = min(itins, key=lambda f: f["price"])
    detail = dict(best)
    detail["n_results"] = len(itins)
    return float(best["price"]), json.dumps(detail, ensure_ascii=False)


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


def run_scan(cfg, conn, args):
    scan = cfg["scan"]
    ttl = cfg["cache"]["ttl_hours"] * 3600
    workers = args.workers or scan["workers"]
    if args.step:
        cfg["search"]["step_days"] = args.step
    all_queries = plan_queries(cfg)
    todo = []
    if args.rank_only:
        todo = []
    else:
        now = time.time()
        for spec in all_queries:
            kind, origin, dest, d1, d2 = spec
            key = key_of(kind, origin, dest, d1, d2, cfg)
            row = conn.execute(
                "SELECT price, fetched_at FROM price_cache WHERE key=?", (key,)
            ).fetchone()
            if args.force or row is None or row[1] is None:
                todo.append((spec, key))
            else:
                age = (
                    now
                    - datetime.fromisoformat(row[1].replace("Z", "+00:00")).timestamp()
                )
                if age > ttl or row[0] is None:
                    todo.append((spec, key))
    if args.limit and len(todo) > args.limit:
        todo = todo[: args.limit]

    print(
        f"{len(all_queries)} total queries, {len(todo)} to fetch, cache hit {len(all_queries) - len(todo)}"
    )

    lock = threading.Lock()
    progress = {"n": 0, "fail": 0, "cached": len(all_queries) - len(todo)}

    def work(item):
        spec, key = item
        kind, origin, dest, d1, d2 = spec
        q = build_query(kind, origin, dest, d1, d2, cfg)
        out = fetch_with_retry(q)
        return spec, key, q, out

    futures = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for item in todo:
            futures.append(ex.submit(work, item))
        for fut in as_completed(futures):
            spec, key, q, out = fut.result()
            fetched = now_iso()
            price = None
            detail = None
            n_results = 0
            if out[0] is not None:
                price, detail = summarize(out[0])
                n_results = len(out[0])
            else:
                detail = json.dumps({"error": out[1]})
            with lock:
                progress["n"] += 1
                if price is None:
                    progress["fail"] += 1
                n, f_, c = progress["n"], progress["fail"], progress["cached"]
                done = n + c
            tag = f"{spec[0]} {spec[1]}->{spec[2]} {spec[3]}..{spec[4] or ''}"
            print(f"[{done}/{len(all_queries)}] {tag}: {price if price else out[1]}")
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


def build_itineraries(cfg, rows):
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
                                "ret_detail": rt,
                                "transfers": tr_total,
                                "transfer_items": tr_items,
                            }
                        )
                    for in_city, out_city in [(OSA, TYO), (TYO, OSA)]:
                        out_leg = rows.get(("OW", origin, in_city, d1s, ""))
                        ret_leg = rows.get(("OW", out_city, dest, d2s, ""))
                        if not (out_leg and ret_leg):
                            continue
                        tr_total, tr_items = transfers_for("OJ", origin, dest, cfg)
                        itins.append(
                            {
                                "key": f"OJ|{origin}|{dest}|{in_city}|{out_city}|{d1s}|{d2s}",
                                "kind": "OJ",
                                "out_origin": origin,
                                "ret_dest": dest,
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

    def card(label, it):
        if not it:
            return f'<div class="card"><div class="card-label">{label}</div><div class="card-value">no data yet</div></div>'
        return (
            f'<div class="card"><div class="card-label">{label}</div>'
            f'<div class="card-value" data-eur="{it["total"]:.0f}">{it["total"]:.0f} EUR</div>'
            f'<div class="card-sub">{html.escape(it["label"])}</div></div>'
        )

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
        route_html = " → ".join(
            f'<span title="{html.escape(names.get(c, c))}">{html.escape(c)}</span>'
            for c in route.split(" -> ")
        )
        times = ""
        if d.get("dep"):
            times = f"{d['dep']} → {d['arr']}"
            if d.get("plus"):
                times += f" (+{d['plus']})"
            if d.get("dur_h"):
                times += f" · {d['dur_h']}h"
        stops = d.get("stops")
        stops_txt = f" ({stops} stop{'s' if stops != 1 else ''})" if stops else ""
        return (
            f"<td>{route_html}{stops_txt}<br>"
            f"<small>{html.escape(times)}</small><br>"
            f"<small>{', '.join(html.escape(a) for a in d.get('airlines', []))}</small></td>"
        )

    rows_html = []
    for i, it in enumerate(shown, 1):
        o, r = it["out_detail"], it["ret_detail"]
        kind_label = "Round trip" if it["kind"] == "RT" else "Open jaw"
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
        gf_links.append(q1.url())
        if it["kind"] == "OJ":
            q2 = build_query("OW", it["out_city"], it["ret_dest"], it["d2"], None, cfg)
            gf_links.append(q2.url())
        links = " ".join(
            f'<a href="{u}" target="_blank" rel="noopener">GF{i + 1}</a>'
            for i, u in enumerate(gf_links)
        )
        tr_items = "; ".join(f"{n} ({v:.0f})" for n, v in it["transfer_items"])
        rows_html.append(
            f'<tr data-eur-total="{it["total"]:.2f}">'
            f"<td>{i}</td><td>{kind_label}</td><td>{route_txt}</td>"
            f"<td>{fmt_date(it['d1'])}</td><td>{fmt_date(it['d2'])}</td>"
            f"<td>{(date.fromisoformat(it['d2']) - date.fromisoformat(it['d1'])).days}</td>"
            f"{leg_cell(o)}"
            f"{leg_cell(r, unavailable=(it['kind'] == 'RT'))}"
            f'<td data-eur="{it["airfare"]:.0f}">{it["airfare"]:.0f}</td>'
            f'<td data-eur="{it["transfers"]:.0f}" title="{html.escape(tr_items)}">{it["transfers"]:.0f}</td>'
            f'<td data-eur="{it["total"]:.0f}" class="total">{it["total"]:.0f}</td>'
            f"<td>{delta}</td><td>{links}</td></tr>"
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

    html_doc = TEMPLATE
    html_doc = html_doc.replace("__RUN_TS__", run_ts)
    html_doc = html_doc.replace("__HUF__", str(huf))
    html_doc = html_doc.replace(
        "__CARDS__",
        card("Cheapest overall", itins[0] if itins else None)
        + card("Cheapest round trip", best_rt)
        + card("Cheapest open jaw", best_oj),
    )
    html_doc = html_doc.replace("__ROWS__", "\n".join(rows_html))
    html_doc = html_doc.replace("__CHART_DATA__", json.dumps(chart_data))
    html_doc = html_doc.replace("__BEST_CHART__", json.dumps(best_chart))
    html_doc = html_doc.replace(
        "__META__",
        f"Generated {run_ts} · per-person prices · prev run: {prev_ts or 'none'} · "
        f"transfers: shinkansen {cfg['costs']['shinkansen_eur']:.0f} + domestic "
        f"{cfg['costs']['domestic_flight_eur']:.0f} + FlixBus {cfg['costs']['flixbus_eur']:.0f} EUR/direction",
    )
    return html_doc


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BUD/VIE -&gt; Japan flight deals</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
 body { font-family: system-ui, sans-serif; margin: 24px; background: #fafafa; color: #222; }
 h1 { font-size: 1.4rem; } .meta { color: #666; font-size: .85rem; }
 .cards { display: flex; gap: 16px; margin: 16px 0; flex-wrap: wrap; }
 .card { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 12px 20px; }
 .card-label { font-size: .8rem; color: #666; }
 .card-value { font-size: 1.6rem; font-weight: 600; }
 .card-sub { font-size: .8rem; color: #444; max-width: 260px; }
 table { border-collapse: collapse; background: #fff; font-size: .85rem; width: 100%; }
 th, td { border: 1px solid #e0e0e0; padding: 6px 8px; text-align: left; vertical-align: top; }
 th { cursor: pointer; background: #f0f0f0; user-select: none; white-space: nowrap; }
 td.total { font-weight: 700; }
 .down { color: #1a7f37; font-weight: 600; } .up { color: #c62828; font-weight: 600; }
 .ind { color: #1a56db; font-size: .7rem; }
 .toggle button { margin-right: 6px; padding: 4px 14px; cursor: pointer; }
 .toggle button.active { background: #222; color: #fff; border-color: #222; }
 .charts { display: flex; gap: 24px; flex-wrap: wrap; margin-top: 24px; }
 .chart-box { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 12px; width: 560px; max-width: 100%; }
 small { color: #666; }
 a { color: #1a56db; }
</style>
</head>
<body>
<h1>BUD/VIE &harr; Tokyo/Osaka deals &mdash; 12&ndash;16 days, 2027-03-25 &rarr; 2027-05-31</h1>
<div class="meta">__META__</div>
<div class="toggle" style="margin:12px 0">
 <button id="btn-eur" class="active" onclick="setCur('EUR')">EUR</button>
 <button id="btn-huf" onclick="setCur('HUF')">HUF</button>
</div>
<div class="cards">__CARDS__</div>
<table id="tbl">
<thead><tr>
 <th data-k="0">#</th><th data-k="1">Type</th><th data-k="2">Route</th>
 <th data-k="3">Outbound</th><th data-k="4">Return</th><th data-k="5">Days</th>
 <th data-k="6">Outbound leg</th><th data-k="7">Return leg</th>
 <th data-k="8">Airfare</th><th data-k="9">Transfers</th><th data-k="10">Total</th>
 <th data-k="11">&Delta;</th><th>Links</th>
</tr></thead>
<tbody>
__ROWS__
</tbody>
</table>
<p><small>Prices per person in EUR, 1 adult query, checked bag included, max 2 stops.
Open jaw = sum of two one-ways (verify multi-city on Google Flights via links).
Times are local; (+n) = arrival n days after departure; duration includes layovers.
For round trips the API only exposes outbound leg details (times/duration), not the return leg.
Transfers are config estimates: open jaw = shinkansen; round trip = shinkansen + domestic flight;
FlixBus added per Vienna leg. &Delta; vs previous run. Hover airport codes for full names.</small></p>
<div class="charts">
 <div class="chart-box"><h3>Top itineraries &mdash; total price over runs</h3><canvas id="c1"></canvas></div>
 <div class="chart-box"><h3>Cheapest overall over runs</h3><canvas id="c2"></canvas></div>
</div>
<script>
const HUF = __HUF__;
let cur = 'EUR';
function fmt(v) {
  return cur === 'EUR' ? Math.round(v).toLocaleString('en') + ' €'
    : Math.round(v * HUF).toLocaleString('hu') + ' Ft';
}
function apply() {
  document.querySelectorAll('[data-eur]').forEach(el => { el.textContent = fmt(parseFloat(el.dataset.eur)); });
  document.querySelectorAll('[data-eur-total]').forEach(el => { el.dataset.v = el.dataset.eurTotal; });
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


def main():
    args = parse_args()
    cfg = load_config(args.config)
    conn = init_db(cfg["cache"]["db"])
    run_ts = now_iso()

    progress = run_scan(cfg, conn, args)
    rows = load_rows(conn)
    itins = build_itineraries(cfg, rows)
    prev, prev_ts = prev_totals(conn)

    labels = {
        "RT": "Round trip",
        "OJ": "Open jaw",
    }
    for it in itins:
        direction = f"{it['in_city']}/{it['out_city']}"
        it["label"] = (
            f"{labels[it['kind']]} {it['out_origin']}→{it['ret_dest']} "
            f"(via {direction}) {it['d1']} → {it['d2']}"
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

    html_doc = render_html(cfg, itins, prev, prev_ts, run_ts, conn, args, progress)
    Path(args.out).write_text(html_doc, encoding="utf-8")

    print(f"\n{len(itins)} itineraries ranked. Top 10 (per person):")
    for it in itins[:10]:
        print(f"  {it['total']:7.0f} EUR  {it['label']}")
    print(f"\nHTML written to {args.out}")
    print(
        f"fetched={progress['n']} cached={progress['cached']} failed={progress['fail']}"
    )


if __name__ == "__main__":
    main()
