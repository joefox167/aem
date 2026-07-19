"""SQLAlchemy models. All datetimes are stored as naive UTC."""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EventKind(str, enum.Enum):
    movie = "movie"
    concert = "concert"
    comedy = "comedy"
    live_performance = "live_performance"
    special_event = "special_event"


class TicketStatus(str, enum.Enum):
    unknown = "unknown"
    coming_soon = "coming_soon"
    presale = "presale"
    on_sale = "on_sale"
    sold_out = "sold_out"


class ChangeType(str, enum.Enum):
    baseline = "baseline"
    added = "added"
    updated = "updated"
    removed = "removed"
    ticket_status = "ticket_status"


class Base(DeclarativeBase):
    pass


class Venue(Base):
    __tablename__ = "venues"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    tz: Mapped[str] = mapped_column(String(64), default="America/Chicago")

    events: Mapped[list[Event]] = relationship(back_populates="venue")


class CanonicalEvent(Base):
    __tablename__ = "canonical_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    title_norm: Mapped[str] = mapped_column(String(512), index=True)
    display_title: Mapped[str] = mapped_column(String(512))

    events: Mapped[list[Event]] = relationship(back_populates="canonical")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("source", "source_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_key: Mapped[str] = mapped_column(String(512))
    venue_id: Mapped[int] = mapped_column(ForeignKey("venues.id"))
    canonical_id: Mapped[int | None] = mapped_column(ForeignKey("canonical_events.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(512))
    title_norm: Mapped[str] = mapped_column(String(512), index=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    event_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticket_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticket_status: Mapped[str] = mapped_column(String(32), default=TicketStatus.unknown.value)
    attrs: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    missing_polls: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)

    venue: Mapped[Venue] = relationship(back_populates="events")
    canonical: Mapped[CanonicalEvent | None] = relationship(back_populates="events")
    changes: Mapped[list[ChangeLog]] = relationship(back_populates="event", order_by="ChangeLog.id")


class Showtime(Base):
    """Individual showtimes (populated by Phase 3 collectors, e.g. Alamo)."""

    __tablename__ = "showtimes"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    format: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ticket_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="active")


class ChangeLog(Base):
    __tablename__ = "change_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    change_type: Mapped[str] = mapped_column(String(32), index=True)
    field_changes: Mapped[dict] = mapped_column(JSON, default=dict)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    notified_immediate_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    digested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    event: Mapped[Event] = relationship(back_populates="changes")


class NotificationSent(Base):
    __tablename__ = "notifications_sent"

    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    channel: Mapped[str] = mapped_column(String(32))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PollRun(Base):
    __tablename__ = "poll_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    collector: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|success|error
    events_seen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class HttpCache(Base):
    __tablename__ = "http_cache"

    url: Mapped[str] = mapped_column(String(1024), primary_key=True)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
