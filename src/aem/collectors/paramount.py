"""Paramount Theatre / Austin Theatre Alliance collector.

austintheatre.org runs WordPress with an `event` custom post type exposed over
the public REST API, so this is a structured read rather than a scrape — the
site's own /events/ page renders its list client-side and contains no event
markup at all.

    https://www.austintheatre.org/wp-json/wp/v2/event?per_page=100&page=N

Roughly 260 events across three pages, about half of them films: the Paramount's
classic-film program is AEM's single largest source of movie coverage.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ..models import EventKind, TicketStatus, utcnow
from .base import Collector, FetchContext, ParseDriftError, RawEvent, VenueInfo

log = logging.getLogger(__name__)

BASE_URL = "https://www.austintheatre.org/wp-json/wp/v2/event"
TICKET_URL = "https://tickets.austintheatre.org/{season_id}"
PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 5
VENUE_TZ = ZoneInfo("America/Chicago")

# The API returns past events; keep an in-progress run visible for a day after
# its last performance, then drop it rather than letting ingest count it missing.
PAST_GRACE = timedelta(hours=24)

KIND_BY_TYPE = {
    "film": EventKind.movie,
    "comedy": EventKind.comedy,
    "moontower-festival": EventKind.comedy,
    "performance": EventKind.live_performance,
}

# `event_tags` is one flat vocabulary mixing sale state, film format and
# marketing copy; each is pulled out by slug and the remainder kept as notes.
STATUS_BY_TAG = {
    "on-sale-now": TicketStatus.on_sale,
    "sold-out": TicketStatus.sold_out,
}
FORMAT_BY_TAG = {
    "35mm": "35mm",
    "in-35mm": "35mm",
    "70mm": "70mm",
    "in-imax": "IMAX",
    "double-feature": "Double Feature",
    "35mm-double-feature": "35mm Double Feature",
}
# "On Sale Fri", "On Sale Thu, 10am", ... — announced but not yet purchasable
COMING_SOON_PREFIX = "on-sale-"

# Events the Alliance presents in someone else's room. Mapped onto the slugs
# AEM already uses so they land on one venue row per room.
VENUE_BY_TAG = {
    "the-paramount-theatre": ("paramount-theatre", "The Paramount Theatre"),
    "the-state-theatre": ("stateside-theatre", "The Stateside Theatre"),
    "bass-concert-hall": ("bass-concert-hall", "Bass Concert Hall"),
    "riverbend-centre": ("riverbend-centre", "Riverbend Centre"),
    "long-center": ("michael-susan-dell-hall", "Michael & Susan Dell Hall"),
    "moody-center": ("moody-center-atx", "Moody Center ATX"),
    "h-e-b-center-at-cedar-park": ("h-e-b-center-at-cedar-park", "H-E-B Center at Cedar Park"),
    "acl-live-at-moody-theater": ("moody-theater", "ACL Live at The Moody Theater"),
}
DEFAULT_VENUE = ("paramount-theatre", "The Paramount Theatre")


def _local_to_utc(value: str) -> datetime | None:
    """'2026-10-12 19:00:00' in venue-local time to naive UTC."""
    try:
        naive = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return naive.replace(tzinfo=VENUE_TZ).astimezone(UTC).replace(tzinfo=None)


def _performances(item: dict) -> list[datetime]:
    out = []
    for perf in (item.get("acf") or {}).get("event_performances") or []:
        dt = _local_to_utc(perf.get("event_performance_time"))
        if dt is not None:
            out.append(dt)
    return sorted(out)


def _tag_slugs(item: dict) -> list[dict]:
    return [t for t in ((item.get("acf") or {}).get("event_tags") or []) if t.get("slug")]


def _from_tags(tags: list[dict]) -> tuple[TicketStatus, str | None, tuple[str, str] | None, list[str]]:
    """Split the flat tag vocabulary into (status, format, venue, leftover notes)."""
    status = TicketStatus.unknown
    fmt: str | None = None
    venue: tuple[str, str] | None = None
    notes: list[str] = []
    for tag in tags:
        slug, name = tag["slug"], tag.get("name") or tag["slug"]
        if slug in STATUS_BY_TAG:
            # sold_out wins over on_sale if a source ever carries both
            if status is TicketStatus.unknown or STATUS_BY_TAG[slug] is TicketStatus.sold_out:
                status = STATUS_BY_TAG[slug]
        elif slug in FORMAT_BY_TAG:
            fmt = fmt or FORMAT_BY_TAG[slug]
        elif slug in VENUE_BY_TAG:
            venue = venue or VENUE_BY_TAG[slug]
        elif slug.startswith(COMING_SOON_PREFIX):
            if status is TicketStatus.unknown:
                status = TicketStatus.coming_soon
        else:
            # unrecognized tags are kept, not dropped: a new vocabulary entry
            # shows up in attrs instead of vanishing
            notes.append(html.unescape(name))
    return status, fmt, venue, notes


def _film_attrs(item: dict) -> dict:
    data = (item.get("acf") or {}).get("event_film_data") or {}
    keep = {k.replace("event_film_", ""): v for k, v in data.items() if v}
    keep.pop("type", None)  # "color"/"bw" on every record; not worth diffing
    return keep


def _map_event(item: dict, now: datetime) -> RawEvent | None:
    event_id = item.get("id")
    title = html.unescape(((item.get("title") or {}).get("rendered") or "").strip())
    if not event_id or not title:
        return None

    performances = _performances(item)
    if not performances:
        return None
    if performances[-1] < now - PAST_GRACE:
        return None

    acf = item.get("acf") or {}
    tags = _tag_slugs(item)
    status, fmt, venue_tag, notes = _from_tags(tags)
    venue_slug, venue_name = venue_tag or DEFAULT_VENUE

    type_slug = ((acf.get("event_type") or {}) or {}).get("slug") or ""
    season_id = ((acf.get("event_tessitura_data") or {}) or {}).get("event_production_season_id")

    attrs: dict = {"event_type": type_slug}
    if fmt:
        attrs["format"] = fmt
    if season_id:
        attrs["production_season_id"] = str(season_id)
    if len(performances) > 1:
        attrs["performances"] = [dt.isoformat() for dt in performances]
    film = _film_attrs(item)
    if film:
        attrs["film"] = film
    if notes:
        attrs["notes"] = notes
    custom = (acf.get("event_custom_text") or "").strip()
    if custom:
        attrs["custom_text"] = html.unescape(custom)

    return RawEvent(
        source_key=str(event_id),
        kind=KIND_BY_TYPE.get(type_slug, EventKind.special_event),
        title=title,
        venue_slug=venue_slug,
        venue_name=venue_name,
        starts_at=performances[0],
        ends_at=performances[-1] if len(performances) > 1 else None,
        event_url=item.get("link"),
        ticket_url=TICKET_URL.format(season_id=season_id) if season_id else None,
        ticket_status=status,
        attrs=attrs,
    )


class ParamountCollector(Collector):
    id = "paramount"
    venues: ClassVar[list[VenueInfo]] = [
        VenueInfo("paramount-theatre", "The Paramount Theatre"),
        VenueInfo("stateside-theatre", "The Stateside Theatre"),
    ]

    def __init__(self, options: dict | None = None):
        super().__init__(options)
        self.max_pages = int(self.options.get("max_pages", DEFAULT_MAX_PAGES))

    def _url(self, page: int) -> str:
        return f"{BASE_URL}?{urlencode({'per_page': PAGE_SIZE, 'page': page})}"

    async def fetch(self, ctx: FetchContext) -> list[RawEvent]:
        now = utcnow()
        events: dict[str, RawEvent] = {}
        received = 0
        total_pages = 1
        page = 1
        while page <= min(total_pages, self.max_pages):
            resp = await ctx.get(self._url(page))
            if page == 1:
                try:
                    total_pages = int(resp.headers.get("x-wp-totalpages") or 1)
                except ValueError:
                    total_pages = 1
                if total_pages > self.max_pages:
                    log.warning("paramount: %d pages available, capped at %d",
                                total_pages, self.max_pages)
            items = resp.json()
            if not isinstance(items, list):
                raise ParseDriftError("paramount: expected a JSON array of events")
            received += len(items)
            for item in items:
                event = _map_event(item, now)
                if event is not None:
                    events[event.source_key] = event
            page += 1
        # zero events *returned* is drift; zero kept just means everything listed
        # has already happened
        if not received:
            raise ParseDriftError("paramount: API returned no events across all pages")
        return list(events.values())
