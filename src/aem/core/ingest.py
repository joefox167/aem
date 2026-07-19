"""Poll orchestration: run collectors, upsert events, classify changes.

Guarantees:
- one broken collector never affects the others,
- failed polls never increment missing_polls (no false removals),
- a collector's very first successful poll writes `baseline` changes that are
  stored for history but never notified.
"""

from __future__ import annotations

import logging
import time
import traceback

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .. import metrics
from ..collectors.base import Collector, FetchContext, NotModified, RawEvent
from ..config import AppConfig
from ..models import ChangeLog, ChangeType, Event, PollRun, TicketStatus, Venue, utcnow
from . import dedup, diff

log = logging.getLogger(__name__)


def _get_or_create_venues(session: Session, collector: Collector) -> dict[str, Venue]:
    out = {}
    for info in collector.venues:
        venue = session.scalar(select(Venue).where(Venue.slug == info.slug))
        if venue is None:
            venue = Venue(source=collector.id, name=info.name, slug=info.slug)
            session.add(venue)
            session.flush()
        out[info.slug] = venue
    return out


def _is_first_run(session: Session, collector_id: str) -> bool:
    count = session.scalar(
        select(func.count()).select_from(PollRun).where(
            PollRun.collector == collector_id, PollRun.status == "success"
        )
    )
    return (count or 0) == 0


def _known_keys(session: Session, collector_id: str) -> set[str]:
    rows = session.scalars(select(Event.source_key).where(Event.source == collector_id)).all()
    return set(rows)


def _stale_status_keys(session: Session, collector_id: str) -> set[str]:
    """Active events whose ticket status we never learned — candidates for a
    detail refresh when the collector has spare fetch budget."""
    rows = session.scalars(
        select(Event.source_key).where(
            Event.source == collector_id,
            Event.status == "active",
            Event.ticket_status == "unknown",
        )
    ).all()
    return set(rows)


def _upsert_batch(session: Session, collector: Collector, batch: list[RawEvent],
                  cfg: AppConfig, first_run: bool) -> dict:
    now = utcnow()
    venues = _get_or_create_venues(session, collector)
    stats = {"added": 0, "updated": 0, "removed": 0, "ticket_status": 0}
    seen_keys: set[str] = set()

    for raw in batch:
        if raw.source_key in seen_keys:
            continue
        seen_keys.add(raw.source_key)
        venue = venues.get(raw.venue_slug)
        if venue is None:
            venue = session.scalar(select(Venue).where(Venue.slug == raw.venue_slug))
            if venue is None:
                venue = Venue(source=collector.id, name=raw.venue_slug, slug=raw.venue_slug)
                session.add(venue)
                session.flush()
        existing = session.scalar(
            select(Event).where(Event.source == collector.id, Event.source_key == raw.source_key)
        )
        resolved = diff.resolve(raw, existing)
        title_norm = dedup.normalize_title(raw.title)
        new_hash = diff.content_hash(title_norm, resolved)

        if existing is None:
            event = Event(
                source=collector.id,
                source_key=raw.source_key,
                venue_id=venue.id,
                kind=raw.kind.value,
                title=raw.title,
                title_norm=title_norm,
                starts_at=resolved["starts_at"],
                ends_at=resolved["ends_at"],
                event_url=resolved["event_url"],
                ticket_url=resolved["ticket_url"],
                ticket_status=resolved["ticket_status"],
                attrs=resolved["attrs"],
                content_hash=new_hash,
                first_seen=now,
                last_seen=now,
            )
            session.add(event)
            session.flush()
            dedup.link_canonical(session, event)
            ctype = ChangeType.baseline if first_run else ChangeType.added
            session.add(ChangeLog(event_id=event.id, change_type=ctype.value,
                                  field_changes={}, detected_at=now))
            if not first_run:
                stats["added"] += 1
                metrics.CHANGES.labels(type="added").inc()
            continue

        # existing event returned by the source again
        changes = diff.field_changes(existing, resolved, title_norm)
        was_removed = existing.status == "removed"
        existing.last_seen = now
        existing.missing_polls = 0
        existing.status = "active"
        existing.venue_id = venue.id
        if existing.content_hash != new_hash or changes:
            old_status = existing.ticket_status
            existing.title = raw.title
            existing.title_norm = title_norm
            existing.starts_at = resolved["starts_at"]
            existing.ends_at = resolved["ends_at"]
            existing.event_url = resolved["event_url"]
            existing.ticket_url = resolved["ticket_url"]
            existing.ticket_status = resolved["ticket_status"]
            existing.attrs = resolved["attrs"]
            existing.content_hash = new_hash
            if changes:
                # learning a status for the first time (unknown -> X) is not a
                # real transition; classifying it as `updated` avoids false
                # "tickets on sale!" alerts while unknowns backfill
                if ("ticket_status" in changes and old_status != resolved["ticket_status"]
                        and old_status != TicketStatus.unknown.value):
                    ctype = ChangeType.ticket_status
                    stats["ticket_status"] += 1
                else:
                    ctype = ChangeType.updated
                    stats["updated"] += 1
                metrics.CHANGES.labels(type=ctype.value).inc()
                session.add(ChangeLog(event_id=existing.id, change_type=ctype.value,
                                      field_changes=changes, detected_at=now))
        elif was_removed:
            # reappeared unchanged after being marked removed
            session.add(ChangeLog(event_id=existing.id, change_type=ChangeType.added.value,
                                  field_changes={"reappeared": [None, True]}, detected_at=now))
            stats["added"] += 1
            metrics.CHANGES.labels(type="added").inc()

    # events of this collector that were absent from the batch
    absent = session.scalars(
        select(Event).where(
            Event.source == collector.id,
            Event.status == "active",
            Event.source_key.notin_(seen_keys) if seen_keys else True,
        )
    ).all()
    for event in absent:
        relevant_date = event.ends_at or event.starts_at
        if relevant_date is not None and relevant_date < now:
            continue  # past events naturally drop off calendars; not a removal
        event.missing_polls += 1
        if event.missing_polls >= cfg.removal_threshold:
            event.status = "removed"
            session.add(ChangeLog(event_id=event.id, change_type=ChangeType.removed.value,
                                  field_changes={}, detected_at=now))
            stats["removed"] += 1
            metrics.CHANGES.labels(type="removed").inc()
    return stats


def _touch_all(session: Session, collector_id: str) -> None:
    """Source signalled 304 — everything it listed last time is still there."""
    now = utcnow()
    for event in session.scalars(
        select(Event).where(Event.source == collector_id, Event.status == "active")
    ):
        event.last_seen = now
        event.missing_polls = 0


async def poll_collectors(session_factory: sessionmaker, collectors: list[Collector],
                          cfg: AppConfig) -> dict[str, dict]:
    """Run each collector in isolation; returns per-collector stats."""
    results: dict[str, dict] = {}
    for collector in collectors:
        started = time.monotonic()
        with session_factory() as session:
            run = PollRun(collector=collector.id)
            session.add(run)
            session.commit()
            ctx = FetchContext(
                session,
                known_keys=_known_keys(session, collector.id),
                refresh_keys=_stale_status_keys(session, collector.id),
            )
            try:
                first_run = _is_first_run(session, collector.id)
                try:
                    batch = await collector.fetch(ctx)
                    stats = _upsert_batch(session, collector, batch, cfg, first_run)
                    run.events_seen = len(batch)
                except NotModified:
                    _touch_all(session, collector.id)
                    stats = {"not_modified": True}
                run.status = "success"
                run.finished_at = utcnow()
                session.commit()
                metrics.COLLECTOR_LAST_SUCCESS.labels(collector=collector.id).set_to_current_time()
                results[collector.id] = stats
                log.info("poll %s ok in %.1fs: %s", collector.id,
                         time.monotonic() - started, stats)
            except Exception as exc:
                session.rollback()
                run.status = "error"
                run.finished_at = utcnow()
                run.error = f"{exc}\n{traceback.format_exc(limit=5)}"
                session.commit()
                metrics.COLLECTOR_ERRORS.labels(collector=collector.id).inc()
                if type(exc).__name__ == "ParseDriftError":
                    metrics.PARSE_DRIFT.labels(collector=collector.id).inc()
                results[collector.id] = {"error": str(exc)}
                log.error("poll %s failed: %s", collector.id, exc)
            finally:
                metrics.POLL_DURATION.labels(collector=collector.id).observe(
                    time.monotonic() - started)
                await ctx.close()

    with session_factory() as session:
        _refresh_active_gauge(session)
    return results


def _refresh_active_gauge(session: Session) -> None:
    metrics.EVENTS_ACTIVE.clear()
    rows = session.execute(
        select(Event.kind, Venue.slug, func.count())
        .join(Venue, Event.venue_id == Venue.id)
        .where(Event.status == "active")
        .group_by(Event.kind, Venue.slug)
    ).all()
    for kind, venue, count in rows:
        metrics.EVENTS_ACTIVE.labels(kind=kind, venue=venue).set(count)
