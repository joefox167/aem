"""Bullock Museum IMAX / Texas Spirit Theater collector.

Primary source: the Convergence ticket store category list at
https://tickets.thestoryoftexas.com/mainstore — every purchasable film appears
there with a stable numeric categoryId (e.g. "IMAX | The Odyssey" -> 1329).
The museum film-listing page enriches matching titles with theater label,
film-page URL and run date range (only Texas Spirit Theater films are
server-rendered there today; IMAX titles typically appear store-first).

Event granularity is a film engagement (title + venue + date range), not
individual showtimes (those are JS-rendered in the store; Phase 3+).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from selectolax.parser import HTMLParser

from ..core.dedup import normalize_title
from ..models import EventKind, TicketStatus
from .base import Collector, FetchContext, ParseDriftError, RawEvent, VenueInfo

log = logging.getLogger(__name__)

FILMS_URL = "https://www.thestoryoftexas.com/visit/imax-and-films/"
STORE_URL = "https://tickets.thestoryoftexas.com/mainstore"

FILM_CATEGORY_RE = re.compile(
    r"^(IMAX(?:\s+DOC)?|TEXAS SPIRIT THEATER)\s*\|\s*(.+)$", re.I
)
MEMBER_SCREENING_RE = re.compile(r"^Member\s+IMAX\s+Screening:\s*(.+)$", re.I)

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


def _parse_date_range(text: str) -> tuple[datetime | None, datetime | None]:
    """Parse strings like 'July 19–December 31, 2026' or
    'July 19, 2026–January 4, 2027'. Returns (start, end) as naive UTC dates."""
    text = text.replace("–", "-").replace("—", "-")
    part_re = re.compile(r"([A-Za-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?")
    parts = part_re.findall(text)
    years = re.findall(r"(\d{4})", text)
    if not parts or not years:
        return None, None

    def build(month_name: str, day: str, year: str | None, fallback_year: str) -> datetime | None:
        month = MONTHS.get(month_name.lower())
        if not month:
            return None
        try:
            return datetime(int(year or fallback_year), month, int(day))
        except ValueError:
            return None

    start = build(*parts[0], fallback_year=years[0])
    end = build(*parts[-1], fallback_year=years[-1]) if len(parts) > 1 else None
    if start and end and end < start:
        # 'July 19-January 4, 2027' style: start year is actually the prior year
        start = start.replace(year=end.year - 1)
    return start, end


def _parse_store(html: str) -> list[dict]:
    """Category links -> [{category_id, raw_name, theater, title, special}]."""
    tree = HTMLParser(html)
    seen: dict[str, dict] = {}
    for a in tree.css("a[href*='categoryId=']"):
        href = a.attributes.get("href") or ""
        m = re.search(r"categoryId=(\d+)", href)
        if not m:
            continue
        cat_id = m.group(1)
        name = re.sub(r"\s+", " ", a.text(separator=" ", strip=True)).strip()
        if not name or cat_id in seen and not FILM_CATEGORY_RE.match(name):
            continue
        fm = FILM_CATEGORY_RE.match(name)
        mm = MEMBER_SCREENING_RE.match(name)
        if fm:
            prefix = fm.group(1).upper()
            seen[cat_id] = {
                "category_id": cat_id,
                "raw_name": name,
                "theater": "Texas Spirit Theater" if "SPIRIT" in prefix else "IMAX",
                "title": fm.group(2).strip(),
                "special": None,
            }
        elif mm:
            seen[cat_id] = {
                "category_id": cat_id,
                "raw_name": name,
                "theater": "IMAX",
                "title": mm.group(1).strip(),
                "special": "Member Screening",
            }
    return list(seen.values())


def _parse_films_page(html: str) -> list[dict]:
    """Museum listing cards -> [{title, url, theater, date_text}]."""
    tree = HTMLParser(html)
    films = []
    for li in tree.css("li.Listing-item"):
        link = li.css_first("a.Listing-title-link")
        if link is None:
            continue
        title = re.sub(r"\s+", " ", link.text(separator=" ", strip=True)).strip()
        label = li.css_first(".Listing-labels-text")
        times = li.css_first(".Listing-times")
        films.append({
            "title": title,
            "url": link.attributes.get("href"),
            "theater": label.text(strip=True) if label else "",
            "date_text": re.sub(r"\s+", " ", times.text(separator=" ", strip=True)) if times else "",
        })
    return films


class BullockImaxCollector(Collector):
    id = "bullock_imax"
    venues = [
        VenueInfo("bullock-imax", "Bullock Museum IMAX"),
        VenueInfo("bullock-tst", "Bullock Museum Texas Spirit Theater"),
    ]

    async def fetch(self, ctx: FetchContext) -> list[RawEvent]:
        store_resp = await ctx.get(STORE_URL, conditional=True)
        films_resp = await ctx.get(FILMS_URL, conditional=True)
        # Both conditional 304 would mean nothing changed, but Convergence rarely
        # sends validators; treat missing responses as "fetch fresh copies".
        if store_resp is None:
            store_resp = await ctx.get(STORE_URL)
        if films_resp is None:
            films_resp = await ctx.get(FILMS_URL)

        categories = _parse_store(store_resp.text)
        if not categories:
            raise ParseDriftError(f"{STORE_URL} returned 200 but no film categories parsed")
        listing = _parse_films_page(films_resp.text)
        listing_by_norm = {normalize_title(f["title"]): f for f in listing}

        events: list[RawEvent] = []
        for cat in categories:
            norm = normalize_title(cat["title"])
            card = listing_by_norm.get(norm)
            fmt = "3D" if re.search(r"\b3D\b", cat["title"], re.I) else (
                "IMAX" if cat["theater"] == "IMAX" else "Standard")
            starts_at = ends_at = None
            date_text = card["date_text"] if card else ""
            if date_text:
                starts_at, ends_at = _parse_date_range(date_text)
                if "multisensory" in date_text.lower():
                    fmt = "Multisensory"
            attrs = {"format": fmt, "theater": cat["theater"]}
            if cat["special"]:
                attrs["special_presentation"] = cat["special"]
            events.append(RawEvent(
                source_key=f"cat:{cat['category_id']}",
                kind=EventKind.movie,
                title=cat["title"],
                venue_slug="bullock-tst" if cat["theater"] == "Texas Spirit Theater" else "bullock-imax",
                starts_at=starts_at,
                ends_at=ends_at,
                event_url=(card or {}).get("url") or FILMS_URL,
                ticket_url=f"{STORE_URL}?categoryId={cat['category_id']}",
                ticket_status=TicketStatus.on_sale,
                attrs=attrs,
            ))
        return events
