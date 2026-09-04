"""Ticketmaster Discovery API v2 collector.

One platform collector covering every Ticketmaster / Live Nation / Front Gate
ticketed room in the Austin DMA, replacing a stack of per-venue scrapers.

Discovery caps deep paging at 1000 results per query, so a single "all Austin
events" request silently truncates. Instead the collector fans out over
classification segments and date windows, keeping every individual query well
under the cap, and reports it as a metric when one still hits it.

Requires an API key (https://developer.ticketmaster.com), read from
`AEM_TICKETMASTER_API_KEY` and injected as the `api_key` option at startup.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from .. import metrics
from ..models import EventKind, TicketStatus, utcnow
from .base import Collector, FetchContext, ParseDriftError, RawEvent, VenueInfo

log = logging.getLogger(__name__)

BASE_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
# Downtown Austin (30.2672, -97.7431) as a precision-7 geohash. Discovery's
# Austin DMA (222) is really Austin *and San Antonio* — a live probe returned
# 170 San Antonio events in the first 600 — so the geo search is the default.
AUSTIN_GEOHASH = "9v6kpvc"
DEFAULT_RADIUS_MILES = 40
AUSTIN_DMA = 222
PAGE_SIZE = 200
DEEP_PAGING_CAP = 1000  # Discovery refuses page * size beyond this
DEFAULT_SEGMENTS = ["Music", "Arts & Theatre", "Film"]
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_BACKOFF = 3.0

# Rooms AEM already tracks under a hand-written slug: keep TM listings on the
# same venue row so favorite_venues and venue pages stay coherent.
VENUE_SLUG_OVERRIDES = {
    "acl live at the moody theater": "moody-theater",
    "austin city limits live at the moody theater": "moody-theater",
    "3ten acl live": "acl-3ten",
    "3ten austin city limits live": "acl-3ten",
    "bullock texas state history museum": "bullock-imax",
}

SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
KEY_RE = re.compile(r"(apikey=)[^&]*")


def _redact(text: str) -> str:
    """Never let the API key reach a log line, a PollRun.error row or ntfy."""
    return KEY_RE.sub(r"\1***", text)


def slugify(name: str) -> str:
    slug = SLUG_STRIP_RE.sub("-", name.lower()).strip("-")
    return slug[:128] or "unknown-venue"


def _venue_slug(name: str) -> str:
    return VENUE_SLUG_OVERRIDES.get(name.lower().strip(), slugify(name))


def _parse_dt(value: str | None) -> datetime | None:
    """Discovery UTC timestamps ('2026-09-12T01:00:00Z') to naive UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def _local_start(dates: dict) -> datetime | None:
    """Fallback for events published with a local date but no UTC dateTime."""
    start = dates.get("start") or {}
    local_date, local_time = start.get("localDate"), start.get("localTime")
    if not local_date:
        return None
    try:
        naive = datetime.fromisoformat(f"{local_date}T{local_time or '00:00:00'}")
        tz = ZoneInfo(dates.get("timezone") or "America/Chicago")
    except (ValueError, KeyError):
        return None
    return naive.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None)


def _classify(segment: str, genre: str, subgenre: str) -> EventKind:
    seg, gen, sub = segment.lower(), genre.lower(), subgenre.lower()
    if seg == "film":
        return EventKind.movie
    if seg == "music":
        return EventKind.concert
    if "comedy" in gen or "comedy" in sub:
        return EventKind.comedy
    if seg.startswith("arts"):
        return EventKind.live_performance
    return EventKind.special_event


def _in_window(now: datetime, start: str | None, end: str | None) -> bool:
    s, e = _parse_dt(start), _parse_dt(end)
    if s is None:
        return False
    return s <= now and (e is None or e >= now)


def _ticket_status(item: dict, now: datetime) -> tuple[TicketStatus | None, str | None]:
    """Derive sale state. Returns (status, abnormal_status_note).

    Discovery exposes no sold-out flag, so `offsale` on a future event is the
    closest honest signal that tickets can no longer be bought. Cancelled and
    postponed events keep their previous status — they are schedule news, not
    sale news, and are surfaced through `status_note` instead.
    """
    dates = item.get("dates") or {}
    code = ((dates.get("status") or {}).get("code") or "").lower()
    if code in ("cancelled", "canceled", "postponed", "rescheduled"):
        return None, code.replace("canceled", "cancelled")

    sales = item.get("sales") or {}
    public = sales.get("public") or {}
    if code == "offsale":
        return TicketStatus.sold_out, None
    if _in_window(now, public.get("startDateTime"), public.get("endDateTime")):
        return TicketStatus.on_sale, None
    for presale in sales.get("presales") or []:
        if _in_window(now, presale.get("startDateTime"), presale.get("endDateTime")):
            return TicketStatus.presale, None
    public_start = _parse_dt(public.get("startDateTime"))
    if public_start is not None and public_start > now:
        return TicketStatus.coming_soon, None
    if code == "onsale":
        return TicketStatus.on_sale, None
    return TicketStatus.unknown, None


def _classification(item: dict) -> tuple[str, str, str]:
    for cls in item.get("classifications") or []:
        if cls.get("primary") is False:
            continue
        return (
            (cls.get("segment") or {}).get("name") or "",
            (cls.get("genre") or {}).get("name") or "",
            (cls.get("subGenre") or {}).get("name") or "",
        )
    return "", "", ""


def _map_event(item: dict, now: datetime) -> RawEvent | None:
    event_id = item.get("id")
    title = (item.get("name") or "").strip()
    venues = (item.get("_embedded") or {}).get("venues") or []
    if not event_id or not title or not venues:
        return None
    venue_name = (venues[0].get("name") or "").strip()
    if not venue_name:
        return None

    dates = item.get("dates") or {}
    segment, genre, subgenre = _classification(item)
    status, status_note = _ticket_status(item, now)
    performers = [a.get("name") for a in (item.get("_embedded") or {}).get("attractions") or []
                  if a.get("name")]

    attrs: dict = {"tm_id": event_id, "tm_venue_id": venues[0].get("id")}
    city = (venues[0].get("city") or {}).get("name")
    if city:
        attrs["city"] = city
    if segment:
        attrs["segment"] = segment
    if genre:
        attrs["genre"] = genre
    if subgenre and subgenre.lower() != "undefined":
        attrs["subgenre"] = subgenre
    if performers:
        attrs["performers"] = performers
    if status_note:
        attrs["status_note"] = status_note
    promoter = (item.get("promoter") or {}).get("name")
    if promoter:
        attrs["promoter"] = promoter
    prices = item.get("priceRanges") or []
    if prices:
        # informational only: price is deliberately outside diff.MEANINGFUL_ATTRS
        # so routine repricing cannot generate change entries
        attrs["price_min"] = prices[0].get("min")
        attrs["price_max"] = prices[0].get("max")
        attrs["price_currency"] = prices[0].get("currency")
    public_start = ((item.get("sales") or {}).get("public") or {}).get("startDateTime")
    if public_start:
        attrs["public_sale_start"] = public_start

    return RawEvent(
        source_key=event_id,
        kind=_classify(segment, genre, subgenre),
        title=title,
        venue_slug=_venue_slug(venue_name),
        venue_name=venue_name,
        starts_at=_parse_dt((dates.get("start") or {}).get("dateTime")) or _local_start(dates),
        ends_at=_parse_dt((dates.get("end") or {}).get("dateTime")),
        event_url=item.get("url"),
        ticket_url=item.get("url"),
        ticket_status=status,
        attrs=attrs,
    )


class TicketmasterCollector(Collector):
    id = "ticketmaster"
    # venues are discovered per poll (whatever the DMA is selling), so none are
    # declared up front; ingest creates them from RawEvent.venue_name
    venues: ClassVar[list[VenueInfo]] = []

    def __init__(self, options: dict | None = None):
        super().__init__(options)
        o = self.options
        self.api_key: str = o.get("api_key") or ""
        self.segments: list[str] = list(o.get("segments") or DEFAULT_SEGMENTS)
        self.horizon_days: int = int(o.get("horizon_days", 180))
        self.window_days: int = int(o.get("window_days", 60))
        self.max_pages: int = int(o.get("max_pages", 5))
        self.allowlist = {s.lower() for s in o.get("venue_allowlist") or []}
        self.denylist = {s.lower() for s in o.get("venue_denylist") or []}
        self.exclude_genres = {s.lower() for s in o.get("exclude_genres") or []}
        self.exclude_cities = {s.lower() for s in o.get("exclude_cities") or []}
        self.geo_params: dict[str, str] = {}
        for key, param in (("dma_id", "dmaId"), ("city", "city"), ("state_code", "stateCode"),
                           ("geo_point", "geoPoint"), ("radius", "radius"), ("unit", "unit"),
                           ("include_tba", "includeTBA"), ("include_tbd", "includeTBD")):
            if o.get(key) is not None:
                self.geo_params[param] = str(o[key])
        if not any(k in self.geo_params for k in ("geoPoint", "dmaId", "city")):
            self.geo_params["geoPoint"] = AUSTIN_GEOHASH
            self.geo_params.setdefault("radius", str(DEFAULT_RADIUS_MILES))
            self.geo_params.setdefault("unit", "miles")

    def _url(self, segment: str, start: datetime, end: datetime, page: int) -> str:
        params = {
            "apikey": self.api_key,
            "segmentName": segment,
            "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size": str(PAGE_SIZE),
            "page": str(page),
            "sort": "date,asc",
            **self.geo_params,
        }
        return f"{BASE_URL}?{urlencode(params)}"

    def _windows(self, now: datetime) -> list[tuple[datetime, datetime]]:
        out, start = [], now
        horizon = now + timedelta(days=self.horizon_days)
        while start < horizon:
            end = min(start + timedelta(days=self.window_days), horizon)
            out.append((start, end))
            start = end
        return out

    async def _get_json(self, ctx: FetchContext, url: str) -> dict:
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                resp = await ctx.get(url)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and attempt < RATE_LIMIT_RETRIES:
                    metrics.TM_API_CALLS.labels(status="rate_limited").inc()
                    log.warning("ticketmaster: rate limited, backing off")
                    await asyncio.sleep(RATE_LIMIT_BACKOFF * (attempt + 1))
                    continue
                metrics.TM_API_CALLS.labels(status="error").inc()
                raise RuntimeError(
                    f"ticketmaster api {exc.response.status_code}: {_redact(str(exc))}"
                ) from None
            metrics.TM_API_CALLS.labels(status="ok").inc()
            return resp.json()
        raise RuntimeError("ticketmaster api: rate limited after retries")

    def _keep(self, event: RawEvent) -> bool:
        slug = event.venue_slug.lower()
        name = (event.venue_name or "").lower()
        if self.allowlist and slug not in self.allowlist and name not in self.allowlist:
            return False
        if slug in self.denylist or name in self.denylist:
            return False
        if str(event.attrs.get("city", "")).lower() in self.exclude_cities:
            return False
        genres = {str(event.attrs.get("genre", "")).lower(),
                  str(event.attrs.get("subgenre", "")).lower()}
        return not (self.exclude_genres & genres)

    async def fetch(self, ctx: FetchContext) -> list[RawEvent]:
        if not self.api_key:
            raise RuntimeError("ticketmaster: no API key configured")
        now = utcnow()
        # TM lists the same event under several queries when a window boundary
        # or a multi-segment classification overlaps; last write wins by id
        collected: dict[str, RawEvent] = {}
        received = 0
        for segment in self.segments:
            for start, end in self._windows(now):
                received += await self._fetch_window(ctx, segment, start, end, now, collected)
        # drift is the API returning nothing, not the configured filters keeping
        # nothing — otherwise a strict allowlist would fake an alarm every poll
        if not received:
            raise ParseDriftError(
                "ticketmaster returned 200 but zero events across every segment and window")
        if not collected:
            log.warning("ticketmaster: %d events returned, all excluded by the configured "
                        "venue/genre/city filters", received)
        return list(collected.values())

    async def _fetch_window(self, ctx: FetchContext, segment: str, start: datetime,
                            end: datetime, now: datetime,
                            collected: dict[str, RawEvent]) -> int:
        """Page through one segment/window, adding kept events. Returns the
        number of events the API returned, before filtering."""
        received = 0
        for page in range(self.max_pages):
            if page * PAGE_SIZE >= DEEP_PAGING_CAP:
                break
            data = await self._get_json(ctx, self._url(segment, start, end, page))
            items = (data.get("_embedded") or {}).get("events") or []
            received += len(items)
            for item in items:
                event = _map_event(item, now)
                if event is not None and self._keep(event):
                    collected[event.source_key] = event
            page_info = data.get("page") or {}
            total = int(page_info.get("totalElements") or 0)
            if page == 0 and total > DEEP_PAGING_CAP:
                metrics.TM_WINDOW_TRUNCATED.labels(segment=segment).inc()
                log.warning("ticketmaster: %s %s..%s has %d events, past the %d paging cap "
                            "— lower window_days", segment, start.date(), end.date(),
                            total, DEEP_PAGING_CAP)
            if not items or page + 1 >= int(page_info.get("totalPages") or 1):
                break
        return received
