"""SQLite storage. One connection per unit of work; WAL lets the web UI read while a sweep writes."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS wants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spec TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL DEFAULT (datetime('now')),
    updated TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS listings (
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    want_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    usd REAL NOT NULL,
    ship REAL,
    star TEXT NOT NULL,
    auction INTEGER NOT NULL DEFAULT 0,
    bids TEXT NOT NULL DEFAULT '',
    time_left TEXT NOT NULL DEFAULT '',
    obo INTEGER NOT NULL DEFAULT 0,
    img TEXT NOT NULL DEFAULT '',
    cap REAL,
    deal INTEGER NOT NULL DEFAULT 0,
    art TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    first_run INTEGER,
    last_run INTEGER,
    low_usd REAL,
    active INTEGER NOT NULL DEFAULT 1,
    ends TEXT,
    PRIMARY KEY (item_id, kind)
);
CREATE INDEX IF NOT EXISTS listings_want ON listings (want_id);
CREATE TABLE IF NOT EXISTS sold (
    item_id TEXT PRIMARY KEY,
    want_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    star TEXT NOT NULL,
    usd REAL NOT NULL,
    date TEXT NOT NULL,
    title TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sold_want ON sold (want_id, kind, star);
CREATE TABLE IF NOT EXISTS outcomes (
    item_id TEXT PRIMARY KEY,
    final_usd REAL,
    currency TEXT NOT NULL DEFAULT '',
    bids INTEGER,
    sold_qty INTEGER,
    status TEXT NOT NULL,  -- sold | unsold (final), on_sale (still listed), error (eBay gave no answer)
    ended_at TEXT,
    checked_at TEXT NOT NULL,
    first_checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pop (
    id INTEGER PRIMARY KEY,
    set_name TEXT NOT NULL,
    term TEXT,
    descr TEXT NOT NULL,
    gems INTEGER NOT NULL,
    total INTEGER NOT NULL,
    fetched TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started TEXT NOT NULL DEFAULT (datetime('now')),
    finished TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    trigger TEXT NOT NULL DEFAULT 'schedule',
    searches INTEGER NOT NULL DEFAULT 0,
    failed TEXT NOT NULL DEFAULT '[]',
    listings INTEGER NOT NULL DEFAULT 0,
    deals INTEGER NOT NULL DEFAULT 0,
    new_deals INTEGER NOT NULL DEFAULT 0,
    alerts TEXT NOT NULL DEFAULT '',
    error TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    usd REAL NOT NULL,
    run_id INTEGER,
    sent TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_id, kind)
);
CREATE TABLE IF NOT EXISTS run_alerts (
    run_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (run_id, item_id, kind)
);
CREATE TABLE IF NOT EXISTS blocked (
    item_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

DEFAULTS = {"psa_1star_max": 80, "raw_1star_max": 20}
# PSA 10 share of all PSA grades per set (GemRate, 2026-11-04); a card's own pop wins when it has one.
GEM_RATES = {"FMA": 0.77, "EVA": 0.89, "CSM": 0.82, "AOT": 0.77}


class DB:
    def __init__(self, path: Path):
        self.path = path
        with self.tx() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(listings)")}
            if "ends" not in cols:  # auction end time, added 2026-11-05
                c.execute("ALTER TABLE listings ADD COLUMN ends TEXT")
            if "cur" not in cols:  # the listing's own currency (API searches only), 2026-11-06
                c.execute("ALTER TABLE listings ADD COLUMN cur TEXT NOT NULL DEFAULT ''")
            if "slab" not in cols:  # the slab label as read from the photo (JSON), added 2026-11-06
                c.execute("ALTER TABLE listings ADD COLUMN slab TEXT")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self.tx() as c:
            return c.execute(sql, args).fetchall()

    def one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        with self.tx() as c:
            return c.execute(sql, args).fetchone()

    def run(self, sql: str, *args: Any) -> int:
        with self.tx() as c:
            cur = c.execute(sql, args)
            return int(cur.lastrowid or 0)

    # --- settings ---
    def get(self, key: str, default: Any) -> Any:
        row = self.one("SELECT value FROM kv WHERE key = ?", key)
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value: Any) -> None:
        self.run(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            key,
            json.dumps(value),
        )

    def defaults(self) -> dict:
        return {**DEFAULTS, **self.get("defaults", {})}

    def gem_rates(self) -> dict:
        return {**GEM_RATES, **self.get("gem_rates", {})}

    # --- wants ---
    def wants(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT id, spec, enabled FROM wants"
        if enabled_only:
            sql += " WHERE enabled = 1"
        out = []
        for r in self.all(sql + " ORDER BY id"):
            w = json.loads(r["spec"])
            w["_id"], w["_enabled"] = r["id"], bool(r["enabled"])
            out.append(w)
        return out

    def want(self, want_id: int) -> dict | None:
        r = self.one("SELECT id, spec, enabled FROM wants WHERE id = ?", want_id)
        if not r:
            return None
        w = json.loads(r["spec"])
        w["_id"], w["_enabled"] = r["id"], bool(r["enabled"])
        return w

    def save_want(self, spec: dict, want_id: int | None = None, enabled: bool = True) -> int:
        clean = {k: v for k, v in spec.items() if not k.startswith("_")}
        body = json.dumps(clean, ensure_ascii=False)
        if want_id is None:
            return self.run("INSERT INTO wants (spec, enabled) VALUES (?, ?)", body, int(enabled))
        self.run(
            "UPDATE wants SET spec = ?, enabled = ?, updated = datetime('now') WHERE id = ?",
            body,
            int(enabled),
            want_id,
        )
        return want_id

    def blocked(self) -> set[str]:
        return {r["item_id"] for r in self.all("SELECT item_id FROM blocked")}
