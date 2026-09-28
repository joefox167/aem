"""Daily digest: gather undigested changes, render HTML, email, stamp."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import metrics, watch
from ..changes import change_details, group_items, is_news, show_key
from ..config import AppConfig, Settings
from ..fmt import category, local_stamp, local_today, upcoming_sale
from ..models import ChangeLog, ChangeType, Event, NotificationSent, Venue, utcnow
from . import email as email_sender

log = logging.getLogger(__name__)

_env = Environment(
    loader=PackageLoader("aem", "web/templates"),
    autoescape=select_autoescape(["html"]),
)

# the plain-text branch of the multipart: block tags must not leave blank lines
_text_env = Environment(
    loader=PackageLoader("aem", "web/templates"),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _on_sale(event: Event, tz: str, this_year: int | None = None) -> str | None:
    """An upcoming public sale time. One already past is clutter -- ticket_status
    is what reports that tickets are on sale now."""
    when = upcoming_sale(event.attrs, utcnow())
    return local_stamp(when, tz, this_year=this_year) if when else None


def build_digest(session: Session, cfg: AppConfig) -> dict:
    """Collect all undigested, non-baseline changes grouped for rendering."""
    changes = session.scalars(
        select(ChangeLog)
        .where(ChangeLog.digested_at.is_(None),
               ChangeLog.change_type != ChangeType.baseline.value)
        .order_by(ChangeLog.id)
    ).all()

    tz = cfg.timezone
    this_year = local_today(tz).year
    wl = watch.load(session)
    movies_added: dict[str, list] = {}
    concerts_added: dict[str, list] = {}
    ticket_changes: list = []
    updated: list = []
    removed: list = []
    minor_updates = 0

    for change in changes:
        if not is_news(change):
            minor_updates += 1  # link / end-time churn, or a status AEM lost track of
            continue
        event = session.get(Event, change.event_id)
        if event is None:
            continue
        venue = session.get(Venue, event.venue_id)
        venue_name = venue.name if venue else "?"
        item = {
            "event": event,
            "venue": venue_name,
            "when": local_stamp(event.starts_at, tz, this_year=this_year),
            "category": category(event),
            "on_sale": _on_sale(event, tz, this_year),
            "change": change,
            "watched": wl.matches(event),
        }
        if change.change_type == ChangeType.added.value:
            bucket = movies_added if event.kind == "movie" else concerts_added
            bucket.setdefault(venue_name, []).append(item)
        elif change.change_type == ChangeType.ticket_status.value:
            to_status = (change.field_changes or {}).get("ticket_status", [None, "?"])[1]
            item["to_status"] = (to_status or "?").replace("_", " ")
            ticket_changes.append(item)
        elif change.change_type == ChangeType.updated.value:
            details = change_details(change, tz, this_year)
            if not details:
                minor_updates += 1  # pure ticket_url / ends_at churn
                continue
            item["details"] = details
            updated.append(item)
        elif change.change_type == ChangeType.removed.value:
            removed.append(item)

    for bucket in (movies_added, concerts_added):
        for venue_name in bucket:
            bucket[venue_name] = group_items(bucket[venue_name], show_key, tz, this_year)
    # only identical changes merge: one performance selling out on its own still
    # gets its own row with its own date
    ticket_changes = group_items(ticket_changes,
                                 lambda i: (*show_key(i), i["to_status"]), tz, this_year)
    removed = group_items(removed, show_key, tz, this_year)

    # watched shows lead the email in their own section instead of their usual one
    watchlist: list = []
    for bucket in (movies_added, concerts_added):
        for venue_name in list(bucket):
            watchlist += [dict(i, label="Added") for i in bucket[venue_name] if i["watched"]]
            bucket[venue_name] = [i for i in bucket[venue_name] if not i["watched"]]
            if not bucket[venue_name]:
                del bucket[venue_name]
    for label, rows in (("Tickets", ticket_changes), ("Changed", updated), ("Removed", removed)):
        # updated rows aren't grouped, so they carry no count of their own
        watchlist += [dict(i, label=label, count=i.get("count", 1)) for i in rows if i["watched"]]
        rows[:] = [i for i in rows if not i["watched"]]

    listed = (len(watchlist)
              + sum(len(v) for b in (movies_added, concerts_added) for v in b.values())
              + len(ticket_changes) + len(updated) + len(removed))

    return {
        "watchlist": watchlist,
        "movies_added": movies_added,
        "concerts_added": concerts_added,
        "ticket_changes": ticket_changes,
        "updated": updated,
        "removed": removed,
        # every change is stamped, including the ones held back from the body
        "minor_updates": minor_updates,
        "change_ids": [c.id for c in changes],
        "total": len(changes),
        # what the reader sees: a run of 16 performances is one line
        "listed": listed,
    }


def render_digest(data: dict, settings: Settings, date_label: str) -> str:
    # rows link to the public venue/ticket page; base_url (aem.tiocruz.com,
    # behind Cloudflare Access) is only for the footer's way into AEM itself
    template = _env.get_template("email_digest.html")
    return template.render(date_label=date_label, base_url=settings.base_url, **data)


def render_digest_text(data: dict, date_label: str, base_url: str = "") -> str:
    """Plain-text alternative, so the multipart/alternative has a real second
    branch -- better for spam scoring and for clients that show a text preview."""
    template = _text_env.get_template("email_digest.txt")
    return template.render(date_label=date_label, base_url=base_url, **data)


def _stamp(session: Session, change_ids: list[int]) -> None:
    now = utcnow()
    for change_id in change_ids:
        change = session.get(ChangeLog, change_id)
        if change is not None:
            change.digested_at = now


def _updates(n: int) -> str:
    return f"{n} update{'' if n == 1 else 's'}"


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
    if data["listed"] == 0 and not force:
        # only housekeeping (link churn, lost statuses): nothing to email, but
        # stamp it so it doesn't pile into tomorrow's count
        _stamp(session, data["change_ids"])
        session.commit()
        metrics.DIGEST_RUNS.labels(status="skipped").inc()
        return {"sent": False, "reason": "nothing worth listing"}

    html = render_digest(data, settings, date_label)
    text = render_digest_text(data, date_label, settings.base_url)
    ok = email_sender.send_html(
        settings.gmail_user, settings.gmail_app_password, settings.digest_to,
        f"AEM digest — {date_label} ({_updates(data['listed'])})", html, text=text,
    )
    if ok:
        _stamp(session, data["change_ids"])
        if already is None:
            session.add(NotificationSent(dedupe_key=dedupe_key, channel="email"))
        session.commit()
        metrics.NOTIFICATIONS.labels(channel="email").inc()
        metrics.DIGEST_RUNS.labels(status="sent").inc()
        metrics.DIGEST_LAST_SENT.set_to_current_time()
        return {"sent": True, "changes": data["total"]}
    metrics.DIGEST_RUNS.labels(status="failure").inc()
    return {"sent": False, "reason": "smtp send failed"}
