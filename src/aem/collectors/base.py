from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session

from ..models import EventKind, HttpCache, TicketStatus, utcnow

USER_AGENT = "AEM/1.0 (personal homelab event monitor; +mailto:nightfox167@gmail.com)"
THROTTLE_SECONDS = 2.0


class NotModified(Exception):
    """Index resource returned 304; nothing changed this poll."""


class ParseDriftError(Exception):
    """A 200 response parsed to zero events — likely site markup drift."""


@dataclass
class VenueInfo:
    slug: str
    name: str


@dataclass
class RawEvent:
    source_key: str
    kind: EventKind
    title: str
    venue_slug: str
    # display name for venues discovered at poll time (platform collectors that
    # cannot declare their rooms up front); ignored for pre-declared venues
    venue_name: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    event_url: str | None = None
    # None means "unknown this poll — preserve the previously stored value"
    ticket_url: str | None = None
    ticket_status: TicketStatus | None = None
    attrs: dict = field(default_factory=dict)


class FetchContext:
    """Shared HTTP plumbing: UA, per-host throttle, retries, conditional GET."""

    def __init__(self, session: Session, known_keys: set[str] | None = None,
                 refresh_keys: set[str] | None = None,
                 throttle_seconds: float = THROTTLE_SECONDS):
        self.session = session
        self.known_keys = known_keys or set()
        # keys the ingest layer wants re-detailed (e.g. ticket status still unknown)
        self.refresh_keys = refresh_keys or set()
        self.throttle_seconds = throttle_seconds
        self._last_request: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=20.0,
            follow_redirects=True,
            transport=httpx.AsyncHTTPTransport(retries=2),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._last_request.get(host, 0.0) + self.throttle_seconds - now)
            self._last_request[host] = now + wait
        if wait > 0:
            await asyncio.sleep(wait)

    async def get(self, url: str, conditional: bool = False) -> httpx.Response | None:
        """GET with throttle. With conditional=True, returns None on 304."""
        await self._throttle(url)
        headers = {}
        cache = self.session.get(HttpCache, url) if conditional else None
        if cache is not None:
            if cache.etag:
                headers["If-None-Match"] = cache.etag
            if cache.last_modified:
                headers["If-Modified-Since"] = cache.last_modified
        resp = await self.client.get(url, headers=headers)
        if resp.status_code == 304:
            return None
        resp.raise_for_status()
        if conditional and (resp.headers.get("etag") or resp.headers.get("last-modified")):
            if cache is None:
                cache = HttpCache(url=url)
                self.session.add(cache)
            cache.etag = resp.headers.get("etag")
            cache.last_modified = resp.headers.get("last-modified")
            cache.fetched_at = utcnow()
        return resp


class Collector(ABC):
    id: str = ""
    venues: ClassVar[list[VenueInfo]] = []

    def __init__(self, options: dict | None = None):
        self.options = options or {}

    @abstractmethod
    async def fetch(self, ctx: FetchContext) -> list[RawEvent]:
        """Return all currently listed events. Raise NotModified if the source
        signalled no change since the last poll."""
