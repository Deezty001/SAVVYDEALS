"""
Database layer for AU PC Deals Finder (SQLite).
"""
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

DB_PATH = Path(__file__).parent / "deals.db"
SEED_CATALOG_PATH = Path(__file__).parent / "catalog_seed.json"

# Settings whose values must never be sent back to the browser.
SECRET_KEYS = ("discord_webhook_url", "telegram_bot_token", "ingest_token", "ntfy_topic")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@contextmanager
def session():
    """Connection that commits on success and always closes."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def get_setting(key: str, default: str = "") -> str:
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def get_all_settings() -> Dict[str, str]:
    conn = get_conn()
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    finally:
        conn.close()


def _column_names(cur: sqlite3.Cursor, table: str) -> set:
    return {r["name"] for r in cur.execute(f"PRAGMA table_info({table})")}


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reference_parts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_name TEXT UNIQUE NOT NULL,
        category TEXT NOT NULL,
        socket_or_series TEXT NOT NULL,
        avg_used_aud REAL NOT NULL,
        good_deal_aud REAL NOT NULL,
        steal_aud REAL NOT NULL,
        regex_patterns TEXT NOT NULL,
        exclude_patterns TEXT DEFAULT '[]',
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS listings (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        price_aud REAL NOT NULL,
        location TEXT DEFAULT '',
        state TEXT DEFAULT '',
        url TEXT NOT NULL,
        image_url TEXT DEFAULT '',
        is_bundle INTEGER DEFAULT 0,
        matched_parts_json TEXT DEFAULT '[]',
        estimated_value_aud REAL DEFAULT 0.0,
        profit_margin_aud REAL DEFAULT 0.0,
        discount_pct REAL DEFAULT 0.0,
        deal_score REAL DEFAULT 0.0,
        deal_tier TEXT DEFAULT 'FAIR',
        status TEXT DEFAULT 'NEW',
        notified INTEGER DEFAULT 0,
        posted_at TEXT DEFAULT '',
        discovered_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)

    # Lightweight migration: track when a listing was last seen by a scan.
    if "last_seen" not in _column_names(cur, "listings"):
        cur.execute("ALTER TABLE listings ADD COLUMN last_seen TEXT DEFAULT ''")
        cur.execute("UPDATE listings SET last_seen = discovered_at WHERE last_seen = ''")

    rp = _column_names(cur, "reference_parts")
    for col, ddl in (("new_retail_aud", "REAL DEFAULT 0"), ("price_source", "TEXT DEFAULT 'seed'"),
                     ("learned_used_aud", "REAL DEFAULT 0"), ("learned_n", "INTEGER DEFAULT 0"),
                     ("trend_pct", "REAL DEFAULT 0")):
        if col not in rp:
            cur.execute(f"ALTER TABLE reference_parts ADD COLUMN {col} {ddl}")
    lc = _column_names(cur, "listings")
    for col, ddl in (("net_profit_aud", "REAL DEFAULT 0"), ("max_buy_aud", "REAL DEFAULT 0"),
                     ("offer_aud", "REAL DEFAULT 0"), ("roi_pct", "REAL DEFAULT 0"),
                     ("confidence", "REAL DEFAULT 0"), ("kind", "TEXT DEFAULT 'private'"),
                     ("flags", "TEXT DEFAULT ''"), ("strategy", "TEXT DEFAULT ''"),
                     ("suburb", "TEXT DEFAULT ''"), ("drive_min", "REAL DEFAULT 0"),
                     ("travel_cost_aud", "REAL DEFAULT 0"), ("alerted_price", "REAL DEFAULT 0"),
                     ("gone_at", "TEXT DEFAULT ''"), ("fingerprint", "TEXT DEFAULT ''"),
                     ("relist_count", "INTEGER DEFAULT 0"), ("needs_read", "INTEGER DEFAULT 0"),
                     ("offer_message", "TEXT DEFAULT ''"), ("eval_sig", "TEXT DEFAULT ''")):
        if col not in lc:
            cur.execute(f"ALTER TABLE listings ADD COLUMN {col} {ddl}")
    cur.execute("""CREATE TABLE IF NOT EXISTS price_snapshots (
        part_id INTEGER NOT NULL, day TEXT NOT NULL, median_ask REAL NOT NULL, n INTEGER NOT NULL,
        PRIMARY KEY (part_id, day))""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_listings_score ON listings(deal_score DESC)")
    cur.execute("""CREATE TABLE IF NOT EXISTS scrape_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, source TEXT NOT NULL, query TEXT DEFAULT '',
        strategy TEXT DEFAULT '', status TEXT NOT NULL, nodes INTEGER DEFAULT 0, parsed INTEGER DEFAULT 0,
        skipped INTEGER DEFAULT 0, ms INTEGER DEFAULT 0, note TEXT DEFAULT '')""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_source ON scrape_runs(source, id DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_listings_tier ON listings(deal_tier)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_listings_discovered ON listings(discovered_at DESC)")

    default_settings = {
        "state": "NSW",
        "city": "Sydney",
        "radius_km": "50",
        "min_discount_pct": "20",
        "min_profit_aud": "50",
        "discord_webhook_url": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "auto_notify_tier": "GOOD_DEAL",  # OFF, STEAL or GOOD_DEAL
        "min_roi_pct": "25", "gst_registered": "1",
        "fee_pct": "6", "warranty_pct": "3", "used_cap_pct": "85",
        "home_suburb": "Sydney CBD", "hourly_value_aud": "30", "cost_per_km_aud": "0.35",
        "mac_notify": "1", "ntfy_topic": "", "msg_pickup_hint": "I can pick up today or tomorrow with cash",
        "scheduler_enabled": "0",
        "search_queries": json.dumps([
            "rtx 3080", "rtx 4070", "rtx 4080", "rtx 5070", "rtx 5080",
            "rx 7800 xt", "rx 7900 xtx", "rx 9070 xt",
            "ryzen 7 5800x3d", "ryzen 7 7800x3d", "ryzen 7 9800x3d",
            "am5 combo", "am4 bundle", "gaming pc rtx", "lga1700", "lga1851"
        ]),
        "exclude_keywords": json.dumps([
            "faulty", "broken", "for parts", "not working", "box only",
            "empty box", "cooler only", "shroud only", "waterblock only",
            "wtb", "wanted", "swap only"
        ]),
    }
    for k, v in default_settings.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    # Random token that authorises the bookmarklet / scanner to push listings.
    cur.execute("SELECT value FROM settings WHERE key = 'ingest_token'")
    row = cur.fetchone()
    if not row or not row["value"]:
        cur.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('ingest_token', ?)",
            (secrets.token_urlsafe(24),),
        )

    conn.commit()

    cur.execute("SELECT COUNT(*) as cnt FROM reference_parts")
    if cur.fetchone()["cnt"] == 0 and SEED_CATALOG_PATH.exists():
        seed_data = json.loads(SEED_CATALOG_PATH.read_text())
        now = utcnow_iso()
        for item in seed_data:
            cur.execute("""
                INSERT OR IGNORE INTO reference_parts
                (canonical_name, category, socket_or_series, avg_used_aud, good_deal_aud, steal_aud, regex_patterns, exclude_patterns, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item["canonical_name"],
                item["category"],
                item["socket_or_series"],
                item["avg_used_aud"],
                item["good_deal_aud"],
                item["steal_aud"],
                json.dumps(item["regex_patterns"]),
                json.dumps(item.get("exclude_patterns", [])),
                now,
            ))
        conn.commit()

    import catalog_v2
    catalog_v2.migrate(conn, utcnow_iso())
    conn.close()

    # The DB holds webhook / bot tokens: keep it private to this user.
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
