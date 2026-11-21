"""SQLite storage: anemometers, measurements, settings."""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY,            -- node ID (16 bit, from the chip serial number)
    name        TEXT NOT NULL DEFAULT '',
    k_enc       BLOB,
    k_mac       BLOB,
    factor      REAL NOT NULL DEFAULT 2.4,      -- km/h per 1 pulse/s (= rev_ms * 3.6 / ppr)
    rev_ms      REAL NOT NULL DEFAULT 0.667,    -- calibration: wind [m/s] at 1 revolution per second
    ppr         INTEGER NOT NULL DEFAULT 1,     -- reed pulses per revolution
    clock       REAL NOT NULL DEFAULT 1.0,      -- real seconds per node second (RC clock)
    last_ctr    INTEGER NOT NULL DEFAULT -1,
    unpair      INTEGER NOT NULL DEFAULT 0,     -- waiting for the node to confirm unpairing
    paired_at   INTEGER NOT NULL DEFAULT 0,
    position    INTEGER NOT NULL DEFAULT 0,
    fw          INTEGER,
    diag        INTEGER,
    sag         INTEGER
);
CREATE TABLE IF NOT EXISTS samples (
    node_id     INTEGER NOT NULL,
    ts          INTEGER NOT NULL,               -- arrival (= end of the window), unix s
    secs        REAL NOT NULL,                  -- window length in real seconds
    pulses      INTEGER NOT NULL,
    gust_pulses INTEGER NOT NULL,
    gust_secs   REAL NOT NULL,                  -- the gust span in real seconds (3 node s)
    vbat_mv     INTEGER,
    temp_c      INTEGER,
    rssi        REAL,
    flags       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS samples_node_ts ON samples(node_id, ts);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(nodes)")}
        for col, ddl in (("rev_ms", "REAL NOT NULL DEFAULT 0.667"), ("ppr", "INTEGER NOT NULL DEFAULT 1")):
            if col not in cols:                    # database of an older version
                self.conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {ddl}")

    def execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    # ------------------------------------------------------------ settings
    def get(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set(self, key: str, value: str) -> None:
        self.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def close(self) -> None:
        with self.lock:
            self.conn.close()
