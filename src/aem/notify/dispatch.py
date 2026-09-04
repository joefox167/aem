"""Immediate-notification dispatch with idempotency and quiet hours.

Runs after every poll and on a periodic flush job. Change rows stay pending
(notified_immediate_at NULL) until either sent or claimed by the digest, so a
restart or quiet-hours window never loses an alert; the notifications_sent
dedupe table guarantees at-most-once delivery per channel.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import metrics
from ..config import AppConfig, Settings
from ..models import ChangeLog, ChangeType, Event, NotificationSent, Venue, utcnow
from . import ntfy, rules

log = logging.getLogger(__name__)

KIND_TAGS = {
    "movie": "clapper",
    "concert": "guitar",
    "comedy": "laughing",
    "live_performance": "performing_arts",
    "special_event": "star",
}
STATUS_LABEL = {
    "on_sale": "Tickets on sale",
    "presale": "Presale open",
    "sold_out": "Sold out",
    "coming_soon": "Coming soon",
}


def in_quiet_hours(cfg: AppConfig, now_local: datetime | None = None) -> bool:
    now = now_local or datetime.now(ZoneInfo(cfg.timezone))
    start = dtime.fromisoformat(cfg.quiet_hours.start)
    end = dtime.fromisoformat(cfg.quiet_hours.end)
    t = dtime(now.hour, now.minute)
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _already_sent(session: Session, key: str) -> bool:
    return session.scalar(
        select(NotificationSent).where(NotificationSent.dedupe_key == key)
    ) is not None


def _format_alert(change: ChangeLog, event: Event, venue: Venue) -> tuple[str, str, str, str]:
    """Returns (title, message, priority, tags)."""
    kind_label = event.kind.replace("_", " ")
    tags = KIND_TAGS.get(event.kind, "loudspeaker")
    when = event.starts_at.strftime("%a %b %-d, %Y") if event.starts_at else "date TBA"
    priority = "default"

    if change.change_type == ChangeType.ticket_status.value:
        to_status = (change.field_changes or {}).get("ticket_status", [None, None])[1]
        label = STATUS_LABEL.get(to_status, f"Ticket status: {to_status}")
        title = f"{label}: {event.title}"
        tags = "ticket"
        if to_status in ("on_sale", "presale"):
            priority = "high"
    elif change.change_type == ChangeType.removed.value:
        title = f"Removed: {event.title}"
        tags = "wastebasket"
    elif change.change_type == ChangeType.updated.value:
        changed = ", ".join((change.field_changes or {}).keys())
        title = f"Updated: {event.title}"
        tags = "pencil2"
        kind_label += f" — changed: {changed}" if changed else ""
    else:  # added
        title = f"New {kind_label}: {event.title}"

    lines = [f"{venue.name} — {when}"]
    openers = (event.attrs or {}).get("openers")
    if openers:
        lines.append("with " + ", ".join(openers))
    fmt = (event.attrs or {}).get("format")
    if fmt and fmt not in ("Standard",):
        lines.append(f"Format: {fmt}")
    tour = (event.attrs or {}).get("tour")
    if tour:
        lines.append(tour)
    if event.ticket_url:
        lines.append(event.ticket_url)
    return title, "\n".join(lines), priority, tags


def process_pending(session: Session, settings: Settings, cfg: AppConfig) -> int:
    """Send immediate alerts for pending changes. Returns number sent."""
    cutoff = utcnow() - timedelta(hours=48)
    pending = session.scalars(
        select(ChangeLog)
        .where(
            ChangeLog.notified_immediate_at.is_(None),
            ChangeLog.digested_at.is_(None),
            ChangeLog.change_type != ChangeType.baseline.value,
            ChangeLog.detected_at >= cutoff,
        )
        .order_by(ChangeLog.id)
    ).all()
    if not pending:
        return 0

    quiet = in_quiet_hours(cfg)
    sent = 0
    for change in pending:
        event = session.get(Event, change.event_id)
        if event is None:
            change.notified_immediate_at = utcnow()
            continue
        venue = session.get(Venue, event.venue_id)
        action, always = rules.evaluate(change, event, venue.slug if venue else "", cfg)
        if action != "immediate":
            continue  # digest/ignore rows are picked up by the daily digest
        if quiet and not always:
            continue  # stays pending; flushed when quiet hours end
        key = f"change:{change.id}:ntfy"
        if _already_sent(session, key):
            change.notified_immediate_at = utcnow()
            continue
        title, message, priority, tags = _format_alert(change, event, venue)
        click = f"{settings.base_url}/event/{event.id}"
        if ntfy.send(settings.ntfy_url, title, message, click=click,
                     priority=priority, tags=tags):
            session.add(NotificationSent(dedupe_key=key, channel="ntfy"))
            change.notified_immediate_at = utcnow()
            metrics.NOTIFICATIONS.labels(channel="ntfy").inc()
            sent += 1
    session.commit()
    return sent


def notify_collector_failure(session: Session, settings: Settings, collector_id: str,
                             error: str) -> None:
    """One ops warning per collector per day."""
    key = f"ops:{collector_id}:{utcnow():%Y-%m-%d}"
    if _already_sent(session, key):
        return
    if ntfy.send(settings.ntfy_ops_url or settings.ntfy_url,
                 f"AEM collector failing: {collector_id}",
                 error[:500], priority="default", tags="warning"):
        session.add(NotificationSent(dedupe_key=key, channel="ntfy-ops"))
        session.commit()
