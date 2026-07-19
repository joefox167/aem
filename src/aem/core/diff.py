"""Content hashing and field-level diffing.

The hash covers only fields whose changes are meaningful to a user; cosmetic
site edits (descriptions, images, markup) never reach these fields, so they
can never produce a change entry.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from ..collectors.base import RawEvent
from ..models import Event, TicketStatus

MEANINGFUL_ATTRS = ("openers", "format", "tour", "series", "special_presentation", "theater")


def resolve(raw: RawEvent, existing: Event | None) -> dict:
    """Merge a RawEvent with the stored event, honoring None-means-preserve
    semantics for ticket_url/ticket_status (e.g. detail page not re-fetched)."""
    ticket_status = raw.ticket_status.value if raw.ticket_status is not None else (
        existing.ticket_status if existing else TicketStatus.unknown.value
    )
    ticket_url = raw.ticket_url if raw.ticket_url is not None else (
        existing.ticket_url if existing else None
    )
    attrs = dict(existing.attrs) if existing else {}
    attrs.update(raw.attrs)
    return {
        "title": raw.title,
        "starts_at": raw.starts_at,
        "ends_at": raw.ends_at,
        "event_url": raw.event_url,
        "ticket_url": ticket_url,
        "ticket_status": ticket_status,
        "attrs": attrs,
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def content_hash(title_norm: str, resolved: dict) -> str:
    payload = {
        "title_norm": title_norm,
        "starts_at": _iso(resolved["starts_at"]),
        "ends_at": _iso(resolved["ends_at"]),
        "ticket_status": resolved["ticket_status"],
        "ticket_url": resolved["ticket_url"],
        "attrs": {k: resolved["attrs"].get(k) for k in MEANINGFUL_ATTRS
                  if resolved["attrs"].get(k) is not None},
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def field_changes(existing: Event, resolved: dict, new_title_norm: str) -> dict:
    """Return {field: [old, new]} for meaningful differences."""
    changes: dict = {}
    if existing.title_norm != new_title_norm:
        changes["title"] = [existing.title, resolved["title"]]
    if existing.starts_at != resolved["starts_at"]:
        changes["starts_at"] = [_iso(existing.starts_at), _iso(resolved["starts_at"])]
    if existing.ends_at != resolved["ends_at"]:
        changes["ends_at"] = [_iso(existing.ends_at), _iso(resolved["ends_at"])]
    if existing.ticket_status != resolved["ticket_status"]:
        changes["ticket_status"] = [existing.ticket_status, resolved["ticket_status"]]
    if (existing.ticket_url or None) != (resolved["ticket_url"] or None):
        changes["ticket_url"] = [existing.ticket_url, resolved["ticket_url"]]
    for key in MEANINGFUL_ATTRS:
        old, new = existing.attrs.get(key), resolved["attrs"].get(key)
        if old != new:
            changes[key] = [old, new]
    return changes
