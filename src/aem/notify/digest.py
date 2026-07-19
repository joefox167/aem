"""Daily digest: gather undigested changes, render HTML, email, stamp."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import metrics
from ..config import AppConfig, Settings
from ..models import ChangeLog, ChangeType, Event, NotificationSent, Venue, utcnow
from . import email as email_sender

log = logging.getLogger(__name__)

_env = Environment(
    loader=PackageLoader("aem", "web/templates"),
    autoescape=select_autoescape(["html"]),
)


def _local(dt: datetime | None, tz: str) -> str:
    if dt is None:
        return "TBA"
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz)).strftime("%a %b %d, %Y")


def build_digest(session: Session, cfg: AppConfig) -> dict:
    """Collect all undigested, non-baseline changes grouped for rendering."""
    changes = session.scalars(
        select(ChangeLog)
        .where(ChangeLog.digested_at.is_(None),
               ChangeLog.change_type != ChangeType.baseline.value)
        .order_by(ChangeLog.id)
    ).all()

    movies_added: dict[str, list] = {}
    concerts_added: dict[str, list] = {}
    ticket_changes: list = []
    updated: list = []
    removed: list = []

    for change in changes:
        event = session.get(Event, change.event_id)
        if event is None:
            continue
        venue = session.get(Venue, event.venue_id)
        venue_name = venue.name if venue else "?"
        item = {
            "event": event,
            "venue": venue_name,
            "when": _local(event.starts_at, cfg.timezone),
            "change": change,
        }
        if change.change_type == ChangeType.added.value:
            bucket = movies_added if event.kind == "movie" else concerts_added
            bucket.setdefault(venue_name, []).append(item)
        elif change.change_type == ChangeType.ticket_status.value:
            to_status = (change.field_changes or {}).get("ticket_status", [None, "?"])[1]
            item["to_status"] = (to_status or "?").replace("_", " ")
            ticket_changes.append(item)
        elif change.change_type == ChangeType.updated.value:
            item["fields"] = ", ".join((change.field_changes or {}).keys())
            updated.append(item)
        elif change.change_type == ChangeType.removed.value:
            removed.append(item)

    return {
        "movies_added": movies_added,
        "concerts_added": concerts_added,
        "ticket_changes": ticket_changes,
        "updated": updated,
        "removed": removed,
        "change_ids": [c.id for c in changes],
        "total": len(changes),
    }


def render_digest(data: dict, settings: Settings, date_label: str) -> str:
    template = _env.get_template("email_digest.html")
    return template.render(date_label=date_label, base_url=settings.base_url, **data)


def send_digest(session: Session, settings: Settings, cfg: AppConfig,
                force: bool = False) -> dict:
    tz = ZoneInfo(cfg.timezone)
    date_label = datetime.now(tz).strftime("%A, %B %d, %Y")
    dedupe_key = f"digest:{datetime.now(tz):%Y-%m-%d}:email"

    already = session.scalar(
        select(NotificationSent).where(NotificationSent.dedupe_key == dedupe_key)
    )
    if already is not None and not force:
        metrics.DIGEST_RUNS.labels(status="skipped").inc()
        return {"sent": False, "reason": "already sent today"}

    data = build_digest(session, cfg)
    if data["total"] == 0 and not force:
        metrics.DIGEST_RUNS.labels(status="skipped").inc()
        return {"sent": False, "reason": "no changes to digest"}

    html = render_digest(data, settings, date_label)
    ok = email_sender.send_html(
        settings.gmail_user, settings.gmail_app_password, settings.digest_to,
        f"AEM digest — {date_label} ({data['total']} changes)", html,
    )
    if ok:
        now = utcnow()
        for change_id in data["change_ids"]:
            change = session.get(ChangeLog, change_id)
            if change is not None:
                change.digested_at = now
        if already is None:
            session.add(NotificationSent(dedupe_key=dedupe_key, channel="email"))
        session.commit()
        metrics.NOTIFICATIONS.labels(channel="email").inc()
        metrics.DIGEST_RUNS.labels(status="sent").inc()
        metrics.DIGEST_LAST_SENT.set_to_current_time()
        return {"sent": True, "changes": data["total"]}
    metrics.DIGEST_RUNS.labels(status="failure").inc()
    return {"sent": False, "reason": "smtp send failed"}
