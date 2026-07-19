"""Cross-source deduplication: link events for the same movie/performer to a
single canonical event so the UI shows one card with venue chips."""

from __future__ import annotations

import re
from datetime import timedelta

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CanonicalEvent, Event

STRIP_PREFIXES = [
    "imax doc |", "imax |", "texas spirit theater |", "member imax screening:",
]
ARTICLES_RE = re.compile(r"^(the|a|an)\s+")
PUNCT_RE = re.compile(r"[^\w\s]")
WS_RE = re.compile(r"\s+")

FUZZ_LINK_THRESHOLD = 92
DATE_WINDOW = timedelta(days=14)


def normalize_title(title: str) -> str:
    t = title.lower().strip()
    for prefix in STRIP_PREFIXES:
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
    t = PUNCT_RE.sub(" ", t)
    t = ARTICLES_RE.sub("", t.strip())
    return WS_RE.sub(" ", t).strip()


def _dates_compatible(a: Event, b: Event) -> bool:
    if a.starts_at is None or b.starts_at is None:
        return True  # date-less engagements (e.g. store-first films) can match
    a_start, a_end = a.starts_at, a.ends_at or a.starts_at
    b_start, b_end = b.starts_at, b.ends_at or b.starts_at
    return a_start <= b_end + DATE_WINDOW and b_start <= a_end + DATE_WINDOW


def link_canonical(session: Session, event: Event) -> None:
    """Attach event to an existing canonical event or create a new one."""
    exact = session.scalar(
        select(CanonicalEvent).where(
            CanonicalEvent.kind == event.kind,
            CanonicalEvent.title_norm == event.title_norm,
        )
    )
    canonical = exact
    if canonical is None:
        candidates = session.scalars(
            select(CanonicalEvent).where(CanonicalEvent.kind == event.kind)
        ).all()
        for cand in candidates:
            score = fuzz.token_set_ratio(event.title_norm, cand.title_norm)
            if score >= FUZZ_LINK_THRESHOLD:
                sibling = session.scalar(
                    select(Event).where(Event.canonical_id == cand.id).limit(1)
                )
                if sibling is None or _dates_compatible(event, sibling):
                    canonical = cand
                    break
    if canonical is None:
        canonical = CanonicalEvent(
            kind=event.kind, title_norm=event.title_norm, display_title=event.title
        )
        session.add(canonical)
        session.flush()
    event.canonical_id = canonical.id
