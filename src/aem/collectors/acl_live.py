"""ACL Live (Moody Theater / 3TEN / PBS tapings) collector.

Index: RSS feed at https://www.acllive.com/events/rss (title, guid, link,
ev:location, ev:startdate). Detail pages are fetched for new events and for
near-term events (to catch On Sale / Sold Out / presale transitions) and add
tour name, openers ("with X & Y" tagline) and the AXS ticket URL.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from selectolax.parser import HTMLParser

from ..models import EventKind, TicketStatus, utcnow
from .base import Collector, FetchContext, NotModified, ParseDriftError, RawEvent, VenueInfo

log = logging.getLogger(__name__)

RSS_URL = "https://www.acllive.com/events/rss"
DETAIL_HORIZON_DAYS = 21
DETAIL_FETCH_CAP = 20

NS = {"ev": "http://purl.org/rss/1.0/modules/event/"}


@dataclass
class RssItem:
    guid: str
    title: str
    link: str
    location: str
    starts_at: datetime | None


def _parse_rss(text: str) -> list[RssItem]:
    root = ET.fromstring(text)
    items = []
    for item in root.iter("item"):
        guid = (item.findtext("guid") or item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        if not guid or not title:
            continue
        start_raw = item.findtext("ev:startdate", namespaces=NS) or ""
        starts_at = None
        if start_raw:
            try:
                dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                starts_at = dt.astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                pass
        items.append(
            RssItem(
                guid=guid,
                title=title,
                link=(item.findtext("link") or guid).strip(),
                location=(item.findtext("ev:location", namespaces=NS) or "").strip(),
                starts_at=starts_at,
            )
        )
    return items


def _venue_slug(location: str) -> str:
    loc = location.lower()
    if "3ten" in loc:
        return "acl-3ten"
    if "taping" in loc or "pbs" in loc:
        return "acl-pbs-taping"
    return "moody-theater"


def _parse_detail(html: str) -> dict:
    """Extract tour, openers, ticket URL and sale status from a detail page."""
    tree = HTMLParser(html)
    out: dict = {}

    tour = tree.css_first(".event_heading .tour")
    if tour and tour.text(strip=True):
        out["tour"] = tour.text(strip=True)

    tagline = tree.css_first(".event_heading h2.tagline")
    if tagline:
        text = tagline.text(strip=True)
        m = re.match(r"^with\s+(.*)$", text, re.I)
        if m:
            out["openers"] = [p.strip() for p in re.split(r"\s*[&,]\s*|\s+and\s+", m.group(1)) if p.strip()]

    status = None
    ticket_url = None
    for a in tree.css("a.tickets"):
        href = a.attributes.get("href") or ""
        classes = (a.attributes.get("class") or "").lower()
        text = a.text(strip=True).lower()
        if "axs.com" in href or "ticket" in href:
            ticket_url = ticket_url or href
        if "soldout" in classes.replace("-", "") or "sold out" in text:
            status = TicketStatus.sold_out
        elif "presale" in classes or "presale" in text:
            status = status or TicketStatus.presale
        elif "onsalenow" in classes or "on sale now" in text or "get tickets" in text:
            status = status or TicketStatus.on_sale
        elif "coming soon" in text:
            status = status or TicketStatus.coming_soon
    if ticket_url:
        out["ticket_url"] = ticket_url
    if status:
        out["ticket_status"] = status
    return out


class AclLiveCollector(Collector):
    id = "acl_live"
    venues = [
        VenueInfo("moody-theater", "ACL Live at The Moody Theater"),
        VenueInfo("acl-3ten", "3TEN ACL Live"),
        VenueInfo("acl-pbs-taping", "Austin City Limits PBS Taping"),
    ]

    async def fetch(self, ctx: FetchContext) -> list[RawEvent]:
        resp = await ctx.get(RSS_URL, conditional=True)
        if resp is None:
            raise NotModified
        items = _parse_rss(resp.text)
        if not items:
            raise ParseDriftError(f"{RSS_URL} returned 200 but parsed to zero items")

        horizon = utcnow() + timedelta(days=DETAIL_HORIZON_DAYS)
        budget = DETAIL_FETCH_CAP
        events: list[RawEvent] = []
        for it in items:
            ev = RawEvent(
                source_key=it.guid,
                kind=EventKind.concert,
                title=it.title,
                venue_slug=_venue_slug(it.location),
                starts_at=it.starts_at,
                event_url=it.link,
            )
            is_new = it.guid not in ctx.known_keys
            near_term = it.starts_at is not None and it.starts_at <= horizon
            wants_refresh = it.guid in ctx.refresh_keys
            if budget > 0 and (is_new or near_term or wants_refresh):
                budget -= 1
                try:
                    detail_resp = await ctx.get(it.link)
                    detail = _parse_detail(detail_resp.text)
                except Exception as exc:  # detail failures must not lose the event
                    log.warning("acl_live: detail fetch failed for %s: %s", it.link, exc)
                    detail = {}
                if "tour" in detail:
                    ev.attrs["tour"] = detail["tour"]
                if "openers" in detail:
                    ev.attrs["openers"] = detail["openers"]
                ev.ticket_url = detail.get("ticket_url")
                ev.ticket_status = detail.get("ticket_status")
            events.append(ev)
        return events
