from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import AppConfig, Settings
from .core import ingest
from .notify import digest, dispatch

log = logging.getLogger(__name__)

BACKUP_KEEP = 14

poll_lock = asyncio.Lock()


async def run_poll(session_factory, collectors, settings: Settings, cfg: AppConfig,
                   only: str | None = None) -> dict:
    async with poll_lock:
        selected = [c for c in collectors if only is None or c.id == only]
        results = await ingest.poll_collectors(session_factory, selected, cfg)
        with session_factory() as session:
            for cid, stats in results.items():
                if "error" in stats:
                    dispatch.notify_collector_failure(session, settings, cid, stats["error"])
            dispatch.process_pending(session, settings, cfg)
        return results


def backup_db(db_path: str) -> None:
    src = Path(db_path)
    if not src.exists():
        return
    backups = src.parent / "backups"
    backups.mkdir(exist_ok=True)
    target = backups / f"aem-{datetime.now():%Y%m%d}.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()
    old = sorted(backups.glob("aem-*.db"))[:-BACKUP_KEEP]
    for f in old:
        f.unlink()
    log.info("db backup written: %s", target)


def build_scheduler(session_factory, collectors, settings: Settings,
                    cfg: AppConfig) -> AsyncIOScheduler:
    tz = ZoneInfo(cfg.timezone)
    sched = AsyncIOScheduler(timezone=tz)

    sched.add_job(
        run_poll, "cron", minute=cfg.poll_minute, jitter=120,
        args=[session_factory, collectors, settings, cfg],
        id="poll", max_instances=1, coalesce=True, misfire_grace_time=600,
    )

    def _digest_job():
        with session_factory() as session:
            result = digest.send_digest(session, settings, cfg)
            log.info("digest job: %s", result)

    sched.add_job(
        _digest_job, "cron", hour=cfg.digest_hour, minute=0,
        id="digest", max_instances=1, coalesce=True, misfire_grace_time=3600,
    )

    def _flush_job():
        with session_factory() as session:
            dispatch.process_pending(session, settings, cfg)

    sched.add_job(_flush_job, "interval", minutes=10, id="flush",
                  max_instances=1, coalesce=True)

    sched.add_job(backup_db, "cron", hour=3, minute=0, args=[settings.db_path],
                  id="backup", max_instances=1, coalesce=True, misfire_grace_time=3600)
    return sched
