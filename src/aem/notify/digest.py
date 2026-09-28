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
from ..fmt import category, local_day, local_stamp, parse_utc, upcoming_sale
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


# a vendor rewriting a ticket link or an end time is not news; rows that carry
# nothing else are counted and summarized rather than listed
NOISE_FIELDS = frozenset({"ticket_url", "ends_at"})

_FIELD_VERBS = {
    "starts_at": "moved",
    "ticket_status": "tickets",
    "title": "retitled",
    "tour": "tour",
    "openers": "openers",
    "performers": "lineup",
    "format": "format",
    "series": "series",
    "special_presentation": "presentation",
    "theater": "theater",
    "status_note": "note",
}


def _pretty(value: object) -> str:
    if value is None or value == "" or value == []:
        return "none"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value).replace("_", " ")


def _describe(field: str, old: object, new: object, tz: str) -> str:
    verb = _FIELD_VERBS.get(field, field.replace("_", " "))
    if field == "starts_at":
        return f"{verb} {local_stamp(parse_utc(old), tz)} \u2192 {local_stamp(parse_utc(new), tz)}"
    return f"{verb} {_pretty(old)} \u2192 {_pretty(new)}"


def _change_details(change: ChangeLog, tz: str) -> list[str]:
    """One "old -> new" phrase per meaningful field. field_changes already holds
    both sides, so the digest can say what moved instead of just naming it."""
    details = []
    for field, pair in (change.field_changes or {}).items():
        if field in NOISE_FIELDS:
            continue
        old, new = pair if isinstance(pair, list) and len(pair) == 2 else (None, None)
        details.append(_describe(field, old, new, tz))
    return details


def _on_sale(event: Event, tz: str) -> str | None:
    """An upcoming public sale time. One already past is clutter -- ticket_status
    is what reports that tickets are on sale now."""
    when = upcoming_sale(event.attrs, utcnow())
    return local_stamp(when, tz) if when else None


def _date_range(first: datetime | None, last: datetime | None, tz: str) -> str:
    """"Thu May 20 – Mon May 31, 2027" -- dates only: the times differ per
    performance and the event page has them."""
    d1, d2 = local_day(first, tz), local_day(last, tz)
    if d1 is None or d2 is None:
        return local_stamp(first, tz, False)
    if d1 == d2:
        return f"{d1:%a %b} {d1.day}, {d1.year}"
    head = f"{d1:%a %b} {d1.day}" + ("" if d1.year == d2.year else f", {d1.year}")
    return f"{head} \u2013 {d2:%a %b} {d2.day}, {d2.year}"


def _group_items(items: list[dict], key, tz: str) -> list[dict]:
    """Collapse the performances of one show into a single row.

    Rows sharing `key(item)` merge into the earliest performance's row, which
    gains `count` and a date-range `when`. A show's 16 performances are one
    announcement; listing them separately buries everything else in the email.
    """
    groups: dict = {}
    for item in sorted(items, key=lambda i: str(i["event"].starts_at or "")):
        groups.setdefault(key(item), []).append(item)
    out = []
    for members in groups.values():
        first = dict(members[0], count=len(members))
        if len(members) > 1:
            first["when"] = _date_range(members[0]["event"].starts_at,
                                        members[-1]["event"].starts_at, tz)
        out.append(first)
    # keep the email's original order: by when each show first appears
    return sorted(out, key=lambda i: i["change"].id)


def _show_key(item: dict) -> tuple:
    return (item["event"].title.strip().lower(), item["venue"])


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
    minor_updates = 0

    for change in changes:
        event = session.get(Event, change.event_id)
        if event is None:
            continue
        venue = session.get(Venue, event.venue_id)
        venue_name = venue.name if venue else "?"
        item = {
            "event": event,
            "venue": venue_name,
            "when": local_stamp(event.starts_at, cfg.timezone),
            "category": category(event),
            "on_sale": _on_sale(event, cfg.timezone),
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
            details = _change_details(change, cfg.timezone)
            if not details:
                minor_updates += 1  # pure ticket_url / ends_at churn
                continue
            item["details"] = details
            updated.append(item)
        elif change.change_type == ChangeType.removed.value:
            removed.append(item)

    tz = cfg.timezone
    for bucket in (movies_added, concerts_added):
        for venue_name in bucket:
            bucket[venue_name] = _group_items(bucket[venue_name], _show_key, tz)
    # only identical changes merge: one performance selling out on its own still
    # gets its own row with its own date
    ticket_changes = _group_items(ticket_changes,
                                  lambda i: (*_show_key(i), i["to_status"]), tz)
    removed = _group_items(removed, _show_key, tz)

    return {
        "movies_added": movies_added,
        "concerts_added": concerts_added,
        "ticket_changes": ticket_changes,
        "updated": updated,
        "removed": removed,
        # every change is stamped, including the ones held back from the body
        "minor_updates": minor_updates,
        "change_ids": [c.id for c in changes],
        "total": len(changes),
    }


def render_digest(data: dict, settings: Settings, date_label: str) -> str:
    # settings stays in the signature for callers; the template links only to
    # public event URLs, so no internal base_url is passed in
    template = _env.get_template("email_digest.html")
    return template.render(date_label=date_label, **data)


def render_digest_text(data: dict, date_label: str) -> str:
    """Plain-text alternative, so the multipart/alternative has a real second
    branch -- better for spam scoring and for clients that show a text preview."""
    template = _text_env.get_template("email_digest.txt")
    return template.render(date_label=date_label, **data)


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
    text = render_digest_text(data, date_label)
    ok = email_sender.send_html(
        settings.gmail_user, settings.gmail_app_password, settings.digest_to,
        f"AEM digest — {date_label} ({data['total']} changes)", html, text=text,
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
