"""The watchlist: keywords and venues someone wants surfaced first.

A keyword matches, case-insensitively, anywhere in an event's title,
performers or openers ("mcalpine" finds "Lizzy McAlpine: The Over Country
Tour"). A venue matches its events.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Event, Venue, Watch

MAX_KEYWORD = 100


@dataclass(frozen=True)
class Watchlist:
    keywords: tuple[str, ...] = ()
    venue_ids: frozenset[int] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.keywords or self.venue_ids)

    def matches(self, event: Event) -> bool:
        if event.venue_id in self.venue_ids:
            return True
        if not self.keywords:
            return False
        attrs = event.attrs or {}
        haystack = " ".join([event.title, *(attrs.get("performers") or []),
                             *(attrs.get("openers") or [])]).lower()
        return any(k in haystack for k in self.keywords)


def load(session: Session) -> Watchlist:
    watches = session.scalars(select(Watch)).all()
    return Watchlist(
        keywords=tuple(w.value for w in watches if w.kind == "keyword"),
        venue_ids=frozenset(int(w.value) for w in watches if w.kind == "venue"),
    )


def add_keyword(session: Session, text: str) -> Watch | None:
    label = " ".join(text.split())[:MAX_KEYWORD]
    if len(label) < 2:  # one letter would match nearly everything
        return None
    return _add(session, "keyword", label.lower(), label)


def add_venue(session: Session, venue_id: int) -> Watch | None:
    venue = session.get(Venue, venue_id)
    if venue is None:
        return None
    return _add(session, "venue", str(venue.id), venue.name)


def _add(session: Session, kind: str, value: str, label: str) -> Watch:
    existing = session.scalar(select(Watch).where(Watch.kind == kind, Watch.value == value))
    if existing is not None:
        return existing
    watch = Watch(kind=kind, value=value, label=label)
    session.add(watch)
    session.commit()
    return watch


def remove(session: Session, watch_id: int) -> None:
    watch = session.get(Watch, watch_id)
    if watch is not None:
        session.delete(watch)
        session.commit()
