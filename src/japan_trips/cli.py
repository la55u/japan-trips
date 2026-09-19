from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import load_config, log, now_iso, parse_args, setup_logging
from .db import init_db
from .google import run_scan
from .ranking import (
    build_itineraries,
    build_with_optional_skyscanner,
    label_itins,
    load_rows,
    prev_totals,
)
from .render import render_html
from .skyscanner import run_skyscanner_if_due


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
    except Exception:  # noqa: BLE001 - secondary source cannot stop the scan
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
            datetime.now(UTC)
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
        "done in %.0fs | HTML written to %s (%.0f KB) | fetched=%d cached=%d failed=%d empty=%d too_long=%d",
        time.time() - started,
        args.out,
        len(html_doc) / 1024,
        progress["n"],
        progress["cached"],
        progress["fail"],
        progress.get("empty", 0),
        progress.get("too_long", 0),
    )
