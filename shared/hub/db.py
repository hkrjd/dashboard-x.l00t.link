"""The hub's own SQLite: the one admin, sessions, login attempts, audit log."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS admin (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    username               TEXT NOT NULL,
    password_hash          TEXT NOT NULL,
    totp_secret_enc        TEXT NOT NULL,
    last_totp_step         INTEGER NOT NULL DEFAULT 0,
    credentials_changed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_codes (
    id        INTEGER PRIMARY KEY,
    code_hash TEXT NOT NULL,
    used_at   REAL
);

CREATE TABLE IF NOT EXISTS sessions (
    id_hash    TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('preauth', 'full')),
    csrf       TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen  REAL NOT NULL,
    expires_at REAL NOT NULL,
    ip         TEXT,
    user_agent TEXT,
    failures   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id       INTEGER PRIMARY KEY,
    at       REAL NOT NULL,
    ip       TEXT NOT NULL,
    username TEXT,
    step     TEXT NOT NULL,
    ok       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempts_ip_at ON login_attempts (ip, at);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY,
    at         REAL NOT NULL,
    ip         TEXT,
    action     TEXT NOT NULL,
    bot        TEXT,
    request_id TEXT,
    result     TEXT NOT NULL,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    """Opens a short-lived connection per unit of work.

    The hub handles one person's clicks, so a connection per call is cheap and
    keeps SQLite's thread rules out of the way of FastAPI's thread pool.
    """

    def __init__(self, path: Path):
        self.path = path

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get_kv(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
