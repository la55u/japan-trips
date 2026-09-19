from __future__ import annotations

import sqlite3


def _ensure_column(conn, table, definition):
    name = definition.split()[0]
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


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
