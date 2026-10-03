"""Server-side sessions and the login throttle."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

from .db import Store

PREAUTH_TTL = 5 * 60
FULL_IDLE = 12 * 60 * 60
FULL_MAX = 7 * 24 * 60 * 60
TOUCH_EVERY = 60
PREAUTH_MAX_FAILURES = 5

IP_WINDOW = 15 * 60
IP_MAX_FAILURES = 10
USERNAME_DELAY_CAP = 30
ALERT_AFTER_FAILURES = 5


def _hash(raw: str) -> str:
    # Only the hash is stored, so a copy of hub.db does not hold live session ids.
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class Session:
    id_hash: str
    kind: str
    csrf: str
    created_at: float
    ip: str | None
    user_agent: str | None
    failures: int


class Sessions:
    def __init__(self, store: Store):
        self.store = store

    def create(
        self, kind: str, ip: str | None, user_agent: str | None, now: float | None = None
    ) -> tuple[str, Session]:
        now = time.time() if now is None else now
        raw = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expires = now + (PREAUTH_TTL if kind == "preauth" else FULL_MAX)
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (id_hash, kind, csrf, created_at, last_seen, expires_at, ip, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_hash(raw), kind, csrf, now, now, expires, ip, (user_agent or "")[:300]),
            )
        return raw, Session(_hash(raw), kind, csrf, now, ip, user_agent, 0)

    def get(self, raw: str | None, kind: str, now: float | None = None) -> Session | None:
        if not raw:
            return None
        now = time.time() if now is None else now
        id_hash = _hash(raw)
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id_hash = ?", (id_hash,)).fetchone()
            if row is None or row["kind"] != kind:
                return None
            idle_limit = PREAUTH_TTL if kind == "preauth" else FULL_IDLE
            if now >= row["expires_at"] or now - row["last_seen"] >= idle_limit:
                conn.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))
                return None
            if kind == "full" and now - row["last_seen"] >= TOUCH_EVERY:
                conn.execute("UPDATE sessions SET last_seen = ? WHERE id_hash = ?", (now, id_hash))
        return Session(
            row["id_hash"],
            row["kind"],
            row["csrf"],
            row["created_at"],
            row["ip"],
            row["user_agent"],
            row["failures"],
        )

    def add_failure(self, session: Session) -> int:
        """Count a wrong second-factor code; the pre-auth session dies at the limit."""
        with self.store.connect() as conn:
            conn.execute("UPDATE sessions SET failures = failures + 1 WHERE id_hash = ?", (session.id_hash,))
            row = conn.execute("SELECT failures FROM sessions WHERE id_hash = ?", (session.id_hash,)).fetchone()
            failures = row["failures"] if row else PREAUTH_MAX_FAILURES
            if failures >= PREAUTH_MAX_FAILURES:
                conn.execute("DELETE FROM sessions WHERE id_hash = ?", (session.id_hash,))
        return failures

    def delete(self, session: Session) -> None:
        with self.store.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id_hash = ?", (session.id_hash,))

    def delete_all(self) -> int:
        with self.store.connect() as conn:
            return conn.execute("DELETE FROM sessions").rowcount

    def list_full(self) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT id_hash, created_at, last_seen, ip, user_agent FROM sessions "
                "WHERE kind = 'full' ORDER BY last_seen DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def purge_expired(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.store.connect() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ? OR "
                "(kind = 'preauth' AND last_seen <= ?) OR (kind = 'full' AND last_seen <= ?)",
                (now, now - PREAUTH_TTL, now - FULL_IDLE),
            )


class Throttle:
    """Brute-force limits that never lock the owner out by username.

    Per IP: too many failures in the window and that address is refused for a
    while. Per username: each failure in a row adds a growing wait before the
    answer, but every attempt is still checked, so the owner's correct
    password always gets in, however long an attacker keeps trying.
    """

    def __init__(self, store: Store):
        self.store = store

    def record(self, ip: str, username: str | None, step: str, ok: bool, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO login_attempts (at, ip, username, step, ok) VALUES (?, ?, ?, ?, ?)",
                (now, ip, username, step, int(ok)),
            )
            conn.execute("DELETE FROM login_attempts WHERE at < ?", (now - 90 * 24 * 60 * 60,))

    def ip_blocked(self, ip: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM login_attempts WHERE ip = ? AND ok = 0 AND at > ?",
                (ip, now - IP_WINDOW),
            ).fetchone()
        return row["n"] >= IP_MAX_FAILURES

    def failures_in_a_row(self) -> int:
        """Password failures since the last successful password, any IP."""
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT ok FROM login_attempts WHERE step = 'password' ORDER BY id DESC LIMIT 50"
            ).fetchall()
        count = 0
        for row in rows:
            if row["ok"]:
                break
            count += 1
        return count

    def delay_seconds(self) -> float:
        failures = self.failures_in_a_row()
        if failures == 0:
            return 0.0
        return float(min(2 ** (failures - 1), USERNAME_DELAY_CAP))

    def recent(self, limit: int = 30) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT at, ip, username, step, ok FROM login_attempts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
