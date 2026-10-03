"""The hub's audit log: who did what, to which bot, with what result."""

from __future__ import annotations

import time

from .db import Store


class Audit:
    def __init__(self, store: Store):
        self.store = store

    def record(
        self,
        action: str,
        result: str,
        *,
        ip: str | None = None,
        bot: str | None = None,
        request_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO audit (at, ip, action, bot, request_id, result, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), ip, action, bot, request_id, result, (detail or "")[:1000] or None),
            )

    def recent(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [dict(row) for row in rows]
