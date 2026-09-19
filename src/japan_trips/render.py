from __future__ import annotations

import html
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .config import CACHE_VERSION
from .google import build_query, plan_queries
from .ranking import (
    build_with_optional_skyscanner,
    label_itins,
    load_rows,
    prev_totals,
)
from .skyscanner import _ss_multicity_url, _ss_oj_universe, _ss_universe, _ss_url


def fmt_date(iso):
    # The scan window never crosses years — keep table dates compact.
    return date.fromisoformat(iso).strftime("%a %d %b")


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
    best_ss = next((i for i in itins if i["kind"] == "SS"), None)

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

    def leg_cell(d, unavailable=False, label=None):
        lbl = f' data-label="{label}"' if label else ""
        if not d:
            return f'<td{lbl} data-sort-number="999999">—</td>'
        if unavailable:
            return (
                f'<td{lbl} data-sort-number="999999"><small>return leg details not exposed by API</small><br>'
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
            f'<td{lbl} data-sort-number="{d.get("dur_h") or 999999}">{route_html}{stops_txt}<br>'
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
        is_ss_oj = it["kind"] == "SS" and it.get("ss_oj")
        kind_label = {
            "RT": "Round trip",
            "OJ": "Open jaw",
            "SS": "Open jaw · OTA" if is_ss_oj else "Round trip · OTA",
        }[it["kind"]]
        route_txt = f"{it['out_origin']} → {it['in_city']} · {it['out_city']} → {it['ret_dest']}"
        delta_html = ""
        if it["key"] in prev and prev[it["key"]] != it["total"]:
            diff = it["total"] - prev[it["key"]]
            cls = "down" if diff < 0 else "up"
            arrow = "▼" if diff < 0 else "▲"
            delta_html = f' <span class="{cls}" title="vs previous run">{arrow} {abs(diff):.0f}</span>'
        gf_links = []
        is_gf_oj = it["kind"] == "OJ" or is_ss_oj
        q1 = build_query(
            "OW" if is_gf_oj else "RT",
            it["out_origin"],
            it["in_city"],
            it["d1"],
            None if is_gf_oj else it["d2"],
            cfg,
        )
        gf_links.append(("Google", q1.url()))
        if is_gf_oj:
            q2 = build_query("OW", it["out_city"], it["ret_dest"], it["d2"], None, cfg)
            gf_links.append(("Google", q2.url()))
        if it["kind"] == "SS":
            if is_ss_oj:
                gf_links.append(
                    (
                        "Skyscanner",
                        _ss_multicity_url(
                            [
                                (it["out_origin"], it["in_city"], it["d1"]),
                                (it["out_city"], it["ret_dest"], it["d2"]),
                            ],
                            ss_domain,
                            cfg["search"].get("adults", 2),
                        ),
                    )
                )
            else:
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
        badge_cls = (
            "badge"
            if it["kind"] == "RT" or (it["kind"] == "SS" and not is_ss_oj)
            else "badge oj"
        )
        agent_html = ""
        if it["kind"] == "SS":
            extras = []
            if it.get("self_transfer"):
                extras.append("self-transfer")
            if it.get("protected"):
                extras.append("protected")
            if it.get("indicative"):
                extras.append(
                    f"<b>indicative</b>, checked {it.get('ss_age_hours', 0):.0f}h ago"
                )
            suffix = (" · " + " · ".join(extras)) if extras else ""
            agent_html = (
                f'<br><small class="muted">via {html.escape(it.get("agent", ""))}'
                f"{suffix}</small>"
            )
        bag_pp = it.get("bag_fee_pp") or 0
        fare_base = it.get("fare_base")
        if fare_base is None:
            fare_base = it["airfare"] - bag_pp
        if bag_pp > 0:
            airfare_cell = (
                f'<td class="num" data-label="Fare" data-sort-number="{it["airfare"]:.2f}"'
                f' data-base-fare="{fare_base:.2f}">'
                f'<span data-eur="{it["airfare"]:.0f}">{it["airfare"]:.0f}</span><br>'
                f'<small title="base fare plus airline bag fees: 1 shared checked bag '
                f'+ 1 carry-on per person, per airline published rates">'
                f'<span data-eur="{fare_base:.0f}">{fare_base:.0f}</span> fare + '
                f'<span data-eur="{bag_pp:.0f}">{bag_pp:.0f}</span> bags</small></td>'
            )
        else:
            airfare_cell = (
                f'<td class="num" data-label="Fare" data-eur="{it["airfare"]:.0f}" '
                f'data-sort-number="{it["airfare"]:.2f}" '
                f'data-base-fare="{fare_base:.2f}">{it["airfare"]:.0f}</td>'
            )
        rows_html.append(
            f'<tr data-eur-total="{it["total"]:.2f}" data-origin="{html.escape(it["out_origin"])}" '
            f'data-days="{trip_days}" data-kind="{it["kind"]}" data-i="{idx}" title="click for details"'
            f' data-bag="{bag_pp:.2f}"'
            f' data-out-dur="{o.get("dur_h", "") if isinstance(o, dict) else ""}"'
            f' data-ret-dur="{r.get("dur_h", "") if isinstance(r, dict) else ""}">'
            f'<td class="rank" data-label="#">{i}</td>'
            f'<td><span class="{badge_cls}">{kind_label}</span>{agent_html}</td>'
            f'<td data-label="Route">{route_txt}</td>'
            f'<td data-label="Depart" data-sort="{it["d1"]}">{fmt_date(it["d1"])}</td>'
            f'<td data-label="Return" data-sort="{it["d2"]}">{fmt_date(it["d2"])}</td>'
            f'<td class="num" data-label="Days" data-sort-number="{trip_days}">{trip_days}</td>'
            f"{leg_cell(o, label='Outbound')}"
            f"{leg_cell(r, unavailable=it.get('ret_unavailable', False), label='Return')}"
            f"{airfare_cell}"
            f'<td class="num" data-label="Transfers" data-eur="{it["transfers"]:.0f}" data-sort-number="{it["transfers"]:.2f}" title="{html.escape(tr_items)}">{it["transfers"]:.0f}</td>'
            f'<td class="num total" data-sort-number="{it["total"]:.2f}" data-transfers="{it["transfers"]:.2f}"><span data-eur="{it["total"]:.0f}">{it["total"]:.0f}</span>{delta_html}</td>'
            f'<td data-label="Book">{links}</td></tr>'
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
                "indicative": it.get("indicative", False),
                "ss_age_hours": it.get("ss_age_hours"),
                "bag_included": it.get("bag_included", True),
                "ota_base_fare": it.get("ota_base_fare"),
                "fare_base": it.get("fare_base", it["airfare"]),
                "bag_fee_pp": bag_pp,
                "bag_items": it.get("bag_items") or [],
                "fetched_at": it.get("fetched_at"),
            }
        )

    hist_top = shown[: cfg["scan"]["history_top_n"]]
    chart_labels = []
    chart_series = {it["key"]: [] for it in hist_top}
    series_meta = {it["key"]: it for it in hist_top}
    history_cutoff = (
        datetime.now(UTC)
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
        d = datetime.fromisoformat(ts)
        return d.strftime("%d %b %Y, %H:%M UTC")

    def _stats_age(ts):
        if not ts:
            return None
        dt = datetime.fromisoformat(ts)
        return (datetime.now(UTC) - dt).total_seconds() / 3600

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
            (
                f"{progress.get('empty', 0)} flexible-only, "
                f"{progress.get('too_long', 0)} over max leg, {progress['fail']} failed"
            ),
        ),
        (
            f"{progress.get('deferred', 0)}",
            "stale queries deferred",
            None,
        ),
    ]
    ss_prefix = f"v{CACHE_VERSION}_{cfg['search'].get('adults', 2)}|%"
    ss_checked, ss_oldest = conn.execute(
        "SELECT COUNT(*), MIN(last_success_at) FROM skyscanner_attempts "
        "WHERE key LIKE ? AND last_success_at IS NOT NULL",
        (ss_prefix,),
    ).fetchone()
    sk_cfg_stats = cfg.get("skyscanner", {})
    ss_total = len(_ss_universe(cfg))
    if sk_cfg_stats.get("oj_enabled", True):
        ss_total += len(_ss_oj_universe(cfg))
    ss_stored = conn.execute(
        "SELECT COUNT(*) FROM skyscanner_prices WHERE key LIKE ?", (ss_prefix,)
    ).fetchone()[0]
    if ss_total:
        sk_cfg = sk_cfg_stats
        n_explore = max(1, sk_cfg_stats.get("explore_combos", 25))
        if sk_cfg_stats.get("oj_enabled", True):
            n_explore += sk_cfg_stats.get("explore_oj_combos", 10)
        remaining = max(0, ss_total - ss_checked)
        runs_needed = (remaining + n_explore - 1) // n_explore
        sweep_days = runs_needed * sk_cfg.get("min_age_hours", 3) / 24
        oldest_txt = f"{_stats_age(ss_oldest):.0f}h" if ss_oldest else "–"
        stat_items.append(
            (
                f"{ss_checked}/{ss_total}",
                "Skyscanner pairs checked",
                (
                    f"{ss_stored} stored with deals · oldest {oldest_txt}"
                    f" · full sweep ≈ {sweep_days:.0f}d"
                ),
            )
        )
    stats_html = ""
    for value, label, sub in stat_items:
        sub_html = f"<small>{html.escape(str(sub))}</small>" if sub else ""
        stats_html += (
            f'<div class="srow"><span class="slabel">{html.escape(label)}</span>'
            f'<span class="sval">{html.escape(str(value))}{sub_html}</span></div>'
        )

    # cheapest deal per route over runs (from itinerary_history keys)
    route_best = {}
    for ts, k, total in hist_rows:
        parts = k.split("|")
        if parts[0] == "SS" and len(parts) >= 6 and parts[1] == "OJ":
            route = f"{parts[2]}→{parts[3]}+{parts[4]}→{parts[5]}"
        elif len(parts) >= 3 and parts[0] in ("RT", "OJ", "SS"):
            route = f"{parts[1]}→{parts[2]}"
        else:
            continue
        per_run = route_best.setdefault(ts, {})
        if route not in per_run or total < per_run[route]:
            per_run[route] = total
    route_names = sorted({r for d in route_best.values() for r in d})
    route_datasets = []
    for idx, route in enumerate(route_names):
        route_datasets.append(
            {
                "label": route,
                "data": [route_best.get(ts, {}).get(route) for ts in all_run_ts],
                "borderColor": palette[idx % len(palette)],
                "backgroundColor": palette[idx % len(palette)],
                "spanGaps": True,
                "tension": 0.25,
                "pointRadius": 2,
            }
        )
    route_chart = {"labels": chart_labels, "datasets": route_datasets}

    html_doc = TEMPLATE
    html_doc = html_doc.replace("__RUN_TS__", run_ts)
    html_doc = html_doc.replace("__HUF__", str(huf))
    html_doc = html_doc.replace("__ADULTS__", str(cfg["search"].get("adults", 2)))
    html_doc = html_doc.replace("__MAX_STOPS__", str(cfg["search"]["max_stops"]))
    html_doc = html_doc.replace("__TABLE_LIMIT__", str(top_n))
    html_doc = html_doc.replace("__MIN_DAYS__", str(cfg["search"]["trip_min_days"]))
    html_doc = html_doc.replace("__MAX_DAYS__", str(cfg["search"]["trip_max_days"]))
    html_doc = html_doc.replace(
        "__MAX_LEG_FILTER__",
        str(cfg.get("ranking", {}).get("max_leg_filter_hours", 24)),
    )
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
        + card("Cheapest open jaw", best_oj)
        + card("Cheapest OTA (Skyscanner)", best_ss),
    )
    html_doc = html_doc.replace("__STATS__", stats_html)
    html_doc = html_doc.replace("__ROWS__", "\n".join(rows_html))

    def script_json(value):
        return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")

    html_doc = html_doc.replace("__ROUTE_CHART__", script_json(route_chart))

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
<link rel="stylesheet" href="assets/results.css">
</head>
<body>
<div class="wrap">
 <div class="top">
  <h1>BUD/VIE &harr; Tokyo/Osaka deals &mdash; 12&ndash;16 days, <span id="window">__WINDOW__</span></h1>
  <div class="meta">__META__</div>
 </div>
 <div class="cards">__CARDS__</div>
 <div class="toolbar">
  <div class="seg">
   <button id="btn-eur" class="active" onclick="setCur('EUR')">EUR</button>
   <button id="btn-huf" onclick="setCur('HUF')">HUF</button>
  </div>
   <div class="filters" aria-label="Table filters">
    <label class="filter">Departure city
     <select id="filter-origin">__ORIGIN_OPTIONS__</select>
    </label>
    <label class="filter">Source
     <select id="filter-source">
      <option value="">All sources</option>
      <option value="google">Google (RT / open jaw)</option>
      <option value="ota">OTA (Skyscanner)</option>
     </select>
    </label>
    <label class="filter">Max leg h
     <input id="filter-leg-max" type="number" min="1" step="1" value="__MAX_LEG_FILTER__" aria-label="Maximum single-leg duration in hours">
    </label>
    <label class="filter check">Bag fees
     <input id="filter-bags" type="checkbox" checked aria-label="Include airline bag fees (1 shared checked bag, 1 carry-on pp)">
    </label>
    <label class="filter check">Transfers
     <input id="filter-transfers" type="checkbox" checked aria-label="Include transfer costs (shinkansen, domestic flight, FlixBus)">
    </label>
    <label class="filter">Trip days
     <span class="day-range">
      <input id="filter-days-min" type="number" min="__MIN_DAYS__" max="__MAX_DAYS__" value="__MIN_DAYS__" aria-label="Minimum trip days">
      <span>to</span>
      <input id="filter-days-max" type="number" min="__MIN_DAYS__" max="__MAX_DAYS__" value="__MAX_DAYS__" aria-label="Maximum trip days">
     </span>
    </label>
   </div>
  </div>
  <div class="panel table-wrap">
  <table id="tbl">
  <thead><tr>
   <th data-k="0">#</th><th data-k="1">Type</th><th data-k="2">Route</th>
   <th data-k="3">Outbound</th><th data-k="4">Return</th><th data-k="5" class="num">Days</th>
   <th data-k="6">Outbound leg</th><th data-k="7">Return leg</th>
   <th data-k="8" class="num">Airfare</th><th data-k="9" class="num">Transfers</th><th data-k="10" class="num">Total</th>
   <th>Links</th>
  </tr></thead>
  <tbody>
__ROWS__
</tbody>
  </table>
  </div>
  <div class="insights">
   <div class="panel stats-panel">
    <h3>Scan status</h3>
    <div class="stat-grid">__STATS__</div>
   </div>
   <div class="chart-box"><h3>Cheapest per route &mdash; over runs</h3><canvas id="c3"></canvas></div>
  </div>
  <details class="foot-details">
   <summary>About the data &amp; how to read the table</summary>
   <p class="foot">Prices per person from __ADULTS__-adult queries, max __MAX_STOPS__ stops.
   Google and OTA fares are <b>base fares without baggage</b> (Google returns the same
   prices with or without the checked-bag filter — verified). Bag fees are therefore added
   per airline, from each carrier's published online rates, for the actual need:
   <b>1 checked bag shared between the 2 travellers + 1 carry-on per person</b>, charged
   per direction (e.g. Scoot €45, Finnair Light €75, Lufthansa-group Light €70, Condor
   Zero €60 + €30 cabin pp; full-service Asian carriers include both bags). Rates checked
   Sep 2026 — verify the exact amount at booking. Rows marked <i>indicative</i> were
   last checked more than a day ago — re-verify via the Skyscanner link before booking.
   The <i>Max leg h</i> filter (default 24) hides rows where any single leg is longer —
   raise it to uncover cheaper but slower OTA itineraries (durations turn red above 24h).
    Untick <i>Bag fees</i> to compare base fares without the airline bag fees —
    the detail dialog itemizes the fees per airline and direction.
   Open jaw = sum of two one-ways (verify the true multi-city price via the GF links).
   <i>Open jaw · OTA</i> rows come from true Skyscanner multi-city searches (single
   OTA booking for both legs) — still OTA fares with the same caveats.
  Times are local; (+n) = arrival n days after departure; duration includes layovers.
  For round trips the return leg is the actual flight paired with the shown outbound when
  available (fetched via Google's selection API), otherwise a <i>reference</i> (the best
  one-way on the same date, marked &asymp;) &mdash; verify via the GF links.
   Transfers are config estimates, added per person: open jaw = shinkansen;
   round trip = shinkansen + domestic flight; FlixBus per Vienna leg
   (shinkansen €90, domestic flight €65, FlixBus €15/direction) — untick
   <i>Transfers</i> to compare fares without them. &Delta; vs previous run. Airport codes carry full names
  on hover &mdash; or click any row for a detail card.</p>
  </details>
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
  const source = document.getElementById('filter-source').value;
  const rawMin = Number(document.getElementById('filter-days-min').value);
  const rawMax = Number(document.getElementById('filter-days-max').value);
  const minDays = Math.min(rawMin, rawMax), maxDays = Math.max(rawMin, rawMax);
  const maxLeg = Number(document.getElementById('filter-leg-max').value);
  const rows = [...document.querySelectorAll('#tbl tbody tr')];
  let matching = 0, visible = 0;
  rows.forEach(row => {
    const kind = row.dataset.kind || 'RT';
    const sourceMatch = !source ||
      (source === 'ota' && kind === 'SS') ||
      (source === 'google' && (kind === 'RT' || kind === 'OJ'));
    const outH = parseFloat(row.dataset.outDur), retH = parseFloat(row.dataset.retDur);
    const legMatch = !maxLeg || ((isNaN(outH) || outH <= maxLeg) &&
                                 (isNaN(retH) || retH <= maxLeg));
    const match = sourceMatch && legMatch &&
      (!origin || row.dataset.origin === origin) &&
      Number(row.dataset.days) >= minDays && Number(row.dataset.days) <= maxDays;
    if (match) matching++;
    const show = match && visible < TABLE_LIMIT;
    row.hidden = !show;
    if (show) {
      visible++;
      row.querySelector('.rank').textContent = visible;
    }
  });
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
let includeBags = true, includeTransfers = true;
function recalc() {
  includeBags = document.getElementById('filter-bags').checked;
  includeTransfers = document.getElementById('filter-transfers').checked;
  const tbl = document.getElementById('tbl');
  tbl.classList.toggle('no-bags', !includeBags);
  tbl.classList.toggle('no-transfers', !includeTransfers);
  document.querySelectorAll('#tbl tbody tr').forEach(tr => {
    const fareTd = tr.children[8], totalTd = tr.children[10];
    if (!fareTd || !totalTd) return;
    const base = parseFloat(fareTd.dataset.baseFare || '0') || 0;
    const bag = parseFloat(tr.dataset.bag || '0') || 0;
    const transfers = parseFloat(totalTd.dataset.transfers || '0') || 0;
    const air = base + (includeBags ? bag : 0);
    const total = air + (includeTransfers ? transfers : 0);
    const fareSpan = fareTd.querySelector('span[data-eur]');
    if (fareSpan) {
      fareSpan.dataset.eur = air.toFixed(0);
      fareTd.dataset.sortNumber = air.toFixed(2);
    }
    const totalSpan = totalTd.querySelector('span[data-eur]');
    if (totalSpan) totalSpan.dataset.eur = total.toFixed(0);
    tr.dataset.eurTotal = total.toFixed(2);
    totalTd.dataset.sortNumber = total.toFixed(2);
  });
  apply();
}
document.getElementById('filter-bags').addEventListener('change', recalc);
document.getElementById('filter-transfers').addEventListener('change', recalc);
document.getElementById('filter-origin').addEventListener('change', applyFilters);
document.getElementById('filter-source').addEventListener('change', applyFilters);
document.getElementById('filter-days-min').addEventListener('input', applyFilters);
document.getElementById('filter-days-max').addEventListener('input', applyFilters);
document.getElementById('filter-leg-max').addEventListener('input', applyFilters);
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
  const bagOff = !includeBags && (it.bag_fee_pp || 0) > 0;
  const trOff = !includeTransfers && (it.transfers || 0) > 0;
  const bagLines = (it.bag_items || []).map(([n, v]) => [esc(n), fmt(v)]);
  const rows = [
    ...(it.ota_base_fare != null
      ? [['OTA base fare', fmt(it.ota_base_fare)]]
      : [['Airfare (base fare)', fmt(it.fare_base != null ? it.fare_base : it.airfare)]]),
    ...(bagOff ? [['Bag fees (excluded by toggle)', '—']] : bagLines),
    ...(trOff ? [['Transfers (excluded by toggle)', '—']]
              : it.transfer_items.map(([n, v]) => [esc(n), fmt(v)])),
  ];
  const grand = it.total - (bagOff ? it.bag_fee_pp || 0 : 0)
                          - (trOff ? it.transfers || 0 : 0);
  const costs = rows.map(([n, v]) =>
    `<tr><td>${n}</td><td>${v}</td></tr>`).join('');
  const links = it.gf_links
    .map(([name, u]) => `<a href="${u}" target="_blank" rel="noopener">${esc(name)}</a>`)
    .join('');
  const bagNote = (it.bag_items || []).length
    ? `<div class="dlg-leg"><b>Bag fees:</b> computed from each airline's published online rates
       for 1 shared checked bag + 1 carry-on per person &mdash; not a verified quote; verify at booking.</div>`
    : '';
  const otaNote = it.ota_base_fare != null
    ? `<div class="dlg-leg"><b>OTA caveat:</b> this is an agent fare (separate tickets / self-transfer).` +
      `${it.self_transfer ? ' This is a self-transfer itinerary.' : ''}` +
      `${it.protected ? ' The provider marks the transfer as protected.' : ''}` +
      `${it.indicative ? ' <b>Indicative:</b> this pair was last checked ' +
        Math.round(it.ss_age_hours || 0) + 'h ago &mdash; re-verify via the Skyscanner link before booking.' : ''}` +
      `${it.fetched_at ? ' Fetched ' + esc(it.fetched_at.replace('T', ' ').replace('Z', ' UTC')) + '.' : ''}</div>`
    : '';
  document.getElementById('dlg-body').innerHTML = `
    <div class="dlg-dates">${fdate(it.d1)} → ${fdate(it.d2)} · ${it.days} days · price per person</div>
    ${legHtml(it.out, 'Outbound')}
    ${legHtml(it.ret, 'Return', it.ret_unavailable)}
    ${bagNote}
    ${otaNote}
    <table class="dlg-costs">
      ${costs}
      <tr class="grand"><td>Total per person</td><td class="grand">${fmt(grand)}</td></tr>
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
const routeChart = __ROUTE_CHART__;
new Chart(document.getElementById('c3'), {
  type: 'line', data: routeChart,
  options: { scales: { y: { title: { display: true, text: 'EUR/person' } } },
             plugins: { legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 10 } } } } }
});
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


def refresh_html(cfg, conn, args, run_ts, progress):
    rows = load_rows(conn, cfg)
    itins = build_with_optional_skyscanner(cfg, conn, rows)
    label_itins(itins)
    prev, prev_ts = prev_totals(conn)
    html_doc = render_html(cfg, itins, prev, prev_ts, run_ts, conn, args, progress)
    Path(args.out).write_text(html_doc, encoding="utf-8")
    return itins, prev, prev_ts
