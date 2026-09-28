"""Turning change-log rows into something a person reads.

Shared by the daily digest email, the /changes page and each event's history,
so all three describe a change the same way and group a show's performances
the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .fmt import category, local_day, local_stamp, parse_utc
from .models import ChangeLog, ChangeType, Event, Venue

# a vendor rewriting a ticket link or an end time is not news; rows that carry
# nothing else are counted and summarized rather than listed
NOISE_FIELDS = frozenset({"ticket_url", "ends_at"})
DATE_FIELDS = frozenset({"starts_at", "ends_at"})

FIELD_VERBS = {
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


def pretty(value: object) -> str:
    if value is None or value == "" or value == []:
        return "none"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value).replace("_", " ")


def describe(field: str, old: object, new: object, tz: str) -> str:
    verb = FIELD_VERBS.get(field, field.replace("_", " "))
    if field == "starts_at":
        return f"{verb} {local_stamp(parse_utc(old), tz)} \u2192 {local_stamp(parse_utc(new), tz)}"
    return f"{verb} {pretty(old)} \u2192 {pretty(new)}"


def change_details(change: ChangeLog, tz: str) -> list[str]:
    """One "old -> new" phrase per meaningful field. field_changes already holds
    both sides, so the digest can say what moved instead of just naming it."""
    details = []
    for field, pair in (change.field_changes or {}).items():
        if field in NOISE_FIELDS:
            continue
        old, new = pair if isinstance(pair, list) and len(pair) == 2 else (None, None)
        if field in DATE_FIELDS and (old is None or new is None):
            # a date vanishing or reappearing is a source hiccup, not a reschedule;
            # ingest now keeps the stored date, and older rows like this are hidden
            continue
        details.append(describe(field, old, new, tz))
    return details


def date_range(first: datetime | None, last: datetime | None, tz: str) -> str:
    """"Thu May 20 – Mon May 31, 2027" -- dates only: the times differ per
    performance and the event page has them."""
    d1, d2 = local_day(first, tz), local_day(last, tz)
    if d1 is None or d2 is None:
        return local_stamp(first, tz, False)
    if d1 == d2:
        return f"{d1:%a %b} {d1.day}, {d1.year}"
    head = f"{d1:%a %b} {d1.day}" + ("" if d1.year == d2.year else f", {d1.year}")
    return f"{head} \u2013 {d2:%a %b} {d2.day}, {d2.year}"


def group_items(items: list[dict], key, tz: str) -> list[dict]:
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
            first["when"] = date_range(members[0]["event"].starts_at,
                                        members[-1]["event"].starts_at, tz)
        out.append(first)
    # keep the email's original order: by when each show first appears
    return sorted(out, key=lambda i: i["change"].id)


def show_key(item: dict) -> tuple:
    return (item["event"].title.strip().lower(), item["venue"])


def is_news(change: ChangeLog) -> bool:
    """Whether a change is worth showing anyone.

    A ticket status moving *to* unknown is AEM losing information (a source
    stopped saying, or AEM corrected its own mislabel), not something that
    happened to the show. Updates that touch only NOISE_FIELDS are churn.
    """
    if change.change_type == ChangeType.ticket_status.value:
        pair = (change.field_changes or {}).get("ticket_status") or [None, None]
        return pair[-1] not in (None, "unknown")
    if change.change_type == ChangeType.updated.value:
        return bool(change_details(change, "UTC"))
    return change.change_type != ChangeType.baseline.value


_LABELS = {
    ChangeType.added.value: "Added",
    ChangeType.ticket_status.value: "Tickets",
    ChangeType.updated.value: "Changed",
    ChangeType.removed.value: "Removed",
}


def describe_change(change: ChangeLog, tz: str) -> tuple[str, str]:
    """(label, plain-English detail) for one change, e.g. ("Changed", "moved
    Sat Oct 03 → Sun Oct 04") -- never raw field names or ISO timestamps."""
    label = _LABELS.get(change.change_type, change.change_type.replace("_", " ").title())
    if change.change_type == ChangeType.ticket_status.value:
        pair = (change.field_changes or {}).get("ticket_status") or [None, None]
        return label, pretty(pair[-1]).capitalize()
    if change.change_type == ChangeType.updated.value:
        return label, "; ".join(change_details(change, tz)) or "minor update"
    if change.change_type == ChangeType.added.value:
        return label, "First listed"
    if change.change_type == ChangeType.removed.value:
        return label, "No longer listed"
    return label, ""


def feed(session, changes: list[ChangeLog], tz: str, today) -> list[tuple[str, list[dict]]]:
    """Recent changes as a day-by-day feed. Within a day, a show's performances
    that changed the same way are one entry ("8 performances · Apr 6 – 11"), in
    the order added, tickets, changed, removed. Non-news changes are dropped."""
    days: dict = {}
    for change in changes:
        if not is_news(change):
            continue
        event = session.get(Event, change.event_id)
        if event is None:
            continue
        venue = session.get(Venue, event.venue_id)
        label, detail = describe_change(change, tz)
        item = {"event": event, "venue": venue.name if venue else "?",
                "when": local_stamp(event.starts_at, tz), "category": category(event),
                "change": change, "label": label, "detail": detail,
                "to_status": detail if label == "Tickets" else None}
        # detected_at is always an instant, never a bare date, so convert it outright
        day = change.detected_at.replace(tzinfo=UTC).astimezone(ZoneInfo(tz)).date()
        days.setdefault(day, []).append(item)

    out = []
    for day in sorted(days, reverse=True):
        items = days[day]
        entries = []
        for label in ("Added", "Tickets", "Changed", "Removed"):
            of_kind = [i for i in items if i["label"] == label]
            if label == "Changed":
                entries += [dict(i, count=1) for i in of_kind]  # details are per performance
            elif label == "Tickets":
                entries += group_items(of_kind, lambda i: (*show_key(i), i["detail"]), tz)
            else:
                entries += group_items(of_kind, show_key, tz)
        if day == today:
            title = "Today"
        elif day == today - timedelta(days=1):
            title = "Yesterday"
        else:
            title = f"{day:%A, %b} {day.day}"
        out.append((title, entries))
    return out
