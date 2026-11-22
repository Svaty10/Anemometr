"""SQLite storage: anemometers, measurements, settings, users, sessions, audit."""

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
-- accounts, sessions and the event history: the same model as the tank hub
CREATE TABLE IF NOT EXISTS users (
    username     TEXT PRIMARY KEY COLLATE NOCASE,
    pw_hash      TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    created      INTEGER NOT NULL,
    pw_changed   INTEGER NOT NULL,
    last_login   INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash   TEXT PRIMARY KEY,                 -- SHA-256 of the cookie token
    username  TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE ON UPDATE CASCADE,
    csrf      TEXT NOT NULL,
    created   INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    ip        TEXT,
    ua        TEXT
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username);
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY,
    ts       INTEGER NOT NULL,
    username TEXT,
    ip       TEXT,
    action   TEXT NOT NULL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


class Database:
    def __init__(self, path: str):
        self.lock = threading.RLock()
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(nodes)")}
        for col, ddl in (("rev_ms", "REAL NOT NULL DEFAULT 0.667"), ("ppr", "INTEGER NOT NULL DEFAULT 1")):
            if col not in cols:                    # database of an older version
                self.conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {ddl}")
        self.conn.execute("DELETE FROM settings WHERE key='password'")   # single password of 1.0 (pre-accounts)

    def execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def scalar(self, sql: str, args: tuple = (), default: Any = None) -> Any:
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return r[0] if r and r[0] is not None else default

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
