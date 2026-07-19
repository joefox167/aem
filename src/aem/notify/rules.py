"""Rule evaluation: map a change to immediate / digest / ignore."""

from __future__ import annotations

from ..config import AppConfig, Rule
from ..models import ChangeLog, Event


def _matches(value: str | None, wanted: str | list[str] | None) -> bool:
    if wanted is None:
        return True
    if value is None:
        return False
    if isinstance(wanted, str):
        return value == wanted
    return value in wanted


def evaluate(change: ChangeLog, event: Event, venue_slug: str, cfg: AppConfig) -> tuple[str, bool]:
    """Returns (action, bypass_quiet_hours). First matching rule wins."""
    status_to = None
    if "ticket_status" in (change.field_changes or {}):
        status_to = change.field_changes["ticket_status"][1]
    fmt = (event.attrs or {}).get("format")

    action, always = "digest", False
    for rule in cfg.rules:
        m = rule.match
        if (_matches(change.change_type, m.change_type)
                and _matches(event.kind, m.kind)
                and _matches(fmt, m.format)
                and _matches(status_to, m.ticket_status_to)
                and _matches(venue_slug, m.venue)):
            action, always = rule.action, rule.always
            break
    if action != "ignore" and venue_slug in cfg.favorite_venues:
        action = "immediate"
    return action, always


def default_rules() -> list[Rule]:
    from ..config import DEFAULT_RULES
    return list(DEFAULT_RULES)
