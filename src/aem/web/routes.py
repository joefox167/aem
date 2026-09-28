from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from .. import changes
from ..fmt import local_stamp, local_today
from ..models import ChangeLog, ChangeType, Event, TicketStatus, Venue, utcnow
from . import listing

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# moved to aem.fmt so the digest email renders timestamps identically
_fmt_local = local_stamp

UPCOMING_DAYS = 14
NEW_SHOWN = 10
# rows per day/section on the dashboard before "See all ->": keeps it to a few
# phone screens instead of ~27
DASHBOARD_PER_SECTION = 5
CHANGES_DAYS = 7


def _ctx(request: Request):
    return {
        "request": request,
        "tz": request.app.state.cfg.timezone,
        "fmt": lambda dt, t=True: _fmt_local(dt, request.app.state.cfg.timezone, t),
    }


def _rows(session, events, tz: str, now) -> list[listing.Row]:
    return [listing.build_row(e, session.get(Venue, e.venue_id), tz, now) for e in events]


def _count(sections) -> int:
    return sum(len(rows) for _, rows in sections)


def _current_rows(session, tz: str, now, today) -> list[listing.Row]:
    events = session.scalars(select(Event).where(Event.status == "active")).all()
    return [r for r in _rows(session, events, tz, now) if listing.is_current(r, today, now)]


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    tz = request.app.state.cfg.timezone
    session = request.app.state.session_factory()
    try:
        now = utcnow()
        today = local_today(tz)
        rows = _current_rows(session, tz, now, today)

        weekend_start, weekend_end = listing.weekend_range(today)
        weekend, weekend_runs = listing.weekend_sections(rows, today)
        # the weekend has its own section and runs get a one-line mention, so the
        # 14-day list covers only the other dated events
        horizon = today + timedelta(days=UPCOMING_DAYS - 1)
        upcoming = [r for r in rows if r.day is not None and today <= r.day <= horizon
                    and not weekend_start <= r.day <= weekend_end]

        on_sale_soon = sorted(
            (r for r in rows if r.sale_at is not None or r.event.ticket_status in
             (TicketStatus.presale.value, TicketStatus.coming_soon.value)),
            key=lambda r: (r.sale_at is None, str(r.sale_at or ""), listing.sort_key(r)),
        )[:20]

        # one row per show: a touring musical's 16 new performances are one announcement
        new_shows = listing.group_shows([r for r in rows if listing.is_new(r)])

        upcoming_sections = listing.sections(upcoming, today)
        return templates.TemplateResponse(request, "index.html", _ctx(request) | {
            "new_shows": new_shows[:NEW_SHOWN], "new_count": len(new_shows),
            "on_sale_soon": on_sale_soon,
            "upcoming": upcoming_sections, "upcoming_count": _count(upcoming_sections),
            "upcoming_days": UPCOMING_DAYS, "per_section": DASHBOARD_PER_SECTION,
            "weekend": weekend, "weekend_start": weekend_start, "weekend_end": weekend_end,
            "weekend_runs": weekend_runs, "weekend_title": listing.weekend_title(today),
        })
    finally:
        session.close()


@router.get("/events", response_class=HTMLResponse)
def events_page(request: Request, kind: str = "", genre: str = "", venue: str = "",
                when: str = "", new: str = "", day: str = "", page: int = 1):
    tz = request.app.state.cfg.timezone
    session = request.app.state.session_factory()
    try:
        now = utcnow()
        today = local_today(tz)
        rows = _current_rows(session, tz, now, today)
        venue_id = int(venue) if venue.isdigit() else None
        kind = kind if kind in listing.KIND_FILTERS else ""
        when = when if when in listing.WHEN_FILTERS else ""
        try:
            on_day = date.fromisoformat(day) if day else None
        except ValueError:
            on_day = None

        # dropdown options follow the chosen kind, so "Movies" doesn't offer "Metal"
        only_new = new == "1"
        genres, venues = listing.facet_counts(
            listing.apply_filters(rows, today, kind=kind, new=only_new))
        # a genre/venue picked under another kind would silently match nothing
        if genre not in {g for g, _ in genres}:
            genre = ""
        if venue_id not in {v for v, _, _ in venues}:
            venue_id = None
        shown = listing.apply_filters(rows, today, kind=kind, genre=genre,
                                      venue=venue_id, when=when, new=only_new, day=on_day)
        all_sections = listing.sections(shown, today)
        page = max(page, 1)
        page_sections, continued, has_more = listing.paginate(all_sections, page)
        params = {k: v for k, v in {"kind": kind, "genre": genre, "venue": venue_id or "",
                                    "when": when, "new": "1" if only_new else "",
                                    "day": on_day.isoformat() if on_day else ""}.items() if v}
        ctx = _ctx(request) | {
            "sections": page_sections, "count": _count(all_sections),
            "continued": continued, "next_page": page + 1 if has_more else None,
            "next_url": "/events?" + urlencode(params | {"page": page + 1}),
            "day": on_day,
            "kind": kind, "genre": genre, "venue": venue_id, "when": when, "new": only_new,
            "kinds": listing.KIND_FILTERS, "whens": listing.WHEN_FILTERS,
            "genres": genres, "venues": venues,
        }
        if request.headers.get("HX-Request"):
            # "Show more" appends a page; any filter change swaps the whole block
            template = "_event_page.html" if page > 1 else "_event_list.html"
        else:
            template = "events.html"
        return templates.TemplateResponse(request, template, ctx)
    finally:
        session.close()


@router.get("/changes", response_class=HTMLResponse)
def changes_page(request: Request):
    tz = request.app.state.cfg.timezone
    session = request.app.state.session_factory()
    try:
        recent = session.scalars(
            select(ChangeLog)
            .where(ChangeLog.change_type != ChangeType.baseline.value,
                   ChangeLog.detected_at >= utcnow() - timedelta(days=CHANGES_DAYS))
            .order_by(ChangeLog.id.desc())
        ).all()
        return templates.TemplateResponse(request, "changes.html", _ctx(request) | {
            "days": changes.feed(session, recent, tz, local_today(tz)),
            "changes_days": CHANGES_DAYS,
        })
    finally:
        session.close()


@router.get("/concerts")
def concerts():
    return RedirectResponse("/events?kind=concert", status_code=301)


def _grouped_by_canonical(session, kind_filter, tz: str):
    now = utcnow()
    today = local_today(tz)
    events = session.scalars(select(Event).where(Event.status == "active", kind_filter)).all()
    rows = listing.merge_performances(
        [r for r in _rows(session, events, tz, now) if listing.is_current(r, today, now)])
    groups: dict = {}
    order = []
    for row in rows:
        key = row.event.canonical_id or f"e{row.event.id}"
        if key not in groups:
            groups[key] = {"title": row.event.title, "rows": []}
            order.append(key)
        groups[key]["rows"].append(row)
    return [groups[k] for k in order]


@router.get("/movies", response_class=HTMLResponse)
def movies(request: Request):
    session = request.app.state.session_factory()
    try:
        groups = _grouped_by_canonical(session, Event.kind == "movie",
                                       request.app.state.cfg.timezone)
        return templates.TemplateResponse(request, "movies.html", _ctx(request) | {"groups": groups})
    finally:
        session.close()


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = ""):
    from ..api.routes import search as api_search

    results = api_search(request, q=q, limit=50) if q else []
    template = "_search_results.html" if request.headers.get("HX-Request") else "search.html"
    return templates.TemplateResponse(request, template, _ctx(request) | {"q": q, "results": results})


@router.get("/event/{event_id}", response_class=HTMLResponse)
def event_page(event_id: int, request: Request):
    tz = request.app.state.cfg.timezone
    session = request.app.state.session_factory()
    try:
        event = session.get(Event, event_id)
        if event is None:
            raise HTTPException(404, "event not found")
        venue = session.get(Venue, event.venue_id)
        now = utcnow()
        siblings = []
        if event.canonical_id:
            siblings = sorted(_rows(session, session.scalars(
                select(Event).where(Event.canonical_id == event.canonical_id,
                                    Event.id != event.id)), tz, now), key=listing.sort_key)
        return templates.TemplateResponse(request, "event.html", _ctx(request) | {
            "event": event, "venue": venue, "row": listing.build_row(event, venue, tz, now),
            "siblings": siblings,
            "history": [(c, *changes.describe_change(c, tz)) for c in reversed(event.changes)
                        if c.change_type != ChangeType.baseline.value and changes.is_news(c)],
        })
    finally:
        session.close()
