"""Daily copy of hub.db (the only admin login and the audit log)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

KEEP = 7
CHECK_EVERY = 60 * 60


def backup_now(db_path: Path, backups_dir: Path, keep: int = KEEP) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / time.strftime("hub-%Y%m%d.db", time.gmtime())
    tmp = target.with_suffix(".tmp")
    source = sqlite3.connect(db_path)
    dest = sqlite3.connect(tmp)
    try:
        source.backup(dest)  # SQLite's online backup: safe while the hub writes
    finally:
        dest.close()
        source.close()
    tmp.replace(target)
    for old in sorted(backups_dir.glob("hub-*.db"))[:-keep]:
        old.unlink()
    return target


def backed_up_today(backups_dir: Path) -> bool:
    return (backups_dir / time.strftime("hub-%Y%m%d.db", time.gmtime())).exists()


async def backup_loop(db_path: Path, backups_dir: Path) -> None:
    while True:
        try:
            if not backed_up_today(backups_dir):
                path = await asyncio.to_thread(backup_now, db_path, backups_dir)
                log.info("Backed up hub.db to %s", path)
        except Exception:  # a failed backup must not stop the hub
            log.exception("hub.db backup failed")
        await asyncio.sleep(CHECK_EVERY)
