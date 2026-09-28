from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..fmt import local_stamp, local_today
from ..models import ChangeLog, ChangeType, Event, TicketStatus, Venue, utcnow
from . import listing

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# moved to aem.fmt so the digest email renders timestamps identically
_fmt_local = local_stamp

UPCOMING_DAYS = 14


def _ctx(request: Request):
    return {
        "request": request,
        "tz": request.app.state.cfg.timezone,
        "fmt": lambda dt, t=True: _fmt_local(dt, request.app.state.cfg.timezone, t),
    }


def _hydrate_changes(session, changes):
    out = []
    for change in changes:
        event = session.get(Event, change.event_id)
        if event is None:
            continue
        venue = session.get(Venue, event.venue_id)
        out.append({"change": change, "event": event, "venue": venue})
    return out


def _rows(session, events, tz: str, now) -> list[listing.Row]:
    return [listing.build_row(e, session.get(Venue, e.venue_id), tz, now) for e in events]


def _current_rows(session, tz: str, now, today) -> list[listing.Row]:
    events = session.scalars(select(Event).where(Event.status == "active")).all()
    return [r for r in _rows(session, events, tz, now) if listing.is_current(r, today)]


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    tz = request.app.state.cfg.timezone
    session = request.app.state.session_factory()
    try:
        now = utcnow()
        today = local_today(tz)
        rows = _current_rows(session, tz, now, today)

        horizon = today + timedelta(days=UPCOMING_DAYS - 1)
        upcoming = [r for r in rows if r.day is not None and r.day <= horizon]

        on_sale_soon = sorted(
            (r for r in rows if r.sale_at is not None or r.event.ticket_status in
             (TicketStatus.presale.value, TicketStatus.coming_soon.value)),
            key=lambda r: (r.sale_at is None, r.sale_at or now, listing.sort_key(r)),
        )[:20]

        new_changes = session.scalars(
            select(ChangeLog)
            .where(ChangeLog.change_type == ChangeType.added.value,
                   ChangeLog.detected_at >= now - timedelta(hours=24))
            .order_by(ChangeLog.detected_at.desc()).limit(50)
        ).all()
        by_id = {r.event.id: r for r in rows}
        seen: set[int] = set()
        new_today = []
        for change in new_changes:
            row = by_id.get(change.event_id)
            if row is not None and row.event.id not in seen:
                seen.add(row.event.id)
                new_today.append(row)

        recent = _hydrate_changes(session, session.scalars(
            select(ChangeLog)
            .where(ChangeLog.change_type != ChangeType.baseline.value,
                   ChangeLog.detected_at >= now - timedelta(days=7))
            .order_by(ChangeLog.detected_at.desc()).limit(100)
        ))
        return templates.TemplateResponse(request, "index.html", _ctx(request) | {
            "new_today": new_today, "on_sale_soon": on_sale_soon,
            "upcoming": listing.sections(upcoming, today), "upcoming_count": len(upcoming),
            "upcoming_days": UPCOMING_DAYS, "recent": recent,
        })
    finally:
        session.close()


@router.get("/events", response_class=HTMLResponse)
def events_page(request: Request, kind: str = "", genre: str = "", venue: str = "",
                when: str = ""):
    tz = request.app.state.cfg.timezone
    session = request.app.state.session_factory()
    try:
        now = utcnow()
        today = local_today(tz)
        rows = _current_rows(session, tz, now, today)
        venue_id = int(venue) if venue.isdigit() else None
        kind = kind if kind in listing.KIND_FILTERS else ""
        when = when if when in listing.WHEN_FILTERS else ""

        # dropdown options follow the chosen kind, so "Movies" doesn't offer "Metal"
        genres, venues = listing.facet_counts(listing.apply_filters(rows, today, kind=kind))
        # a genre/venue picked under another kind would silently match nothing
        if genre not in {g for g, _ in genres}:
            genre = ""
        if venue_id not in {v for v, _, _ in venues}:
            venue_id = None
        shown = listing.apply_filters(rows, today, kind=kind, genre=genre,
                                      venue=venue_id, when=when)
        ctx = _ctx(request) | {
            "sections": listing.sections(shown, today), "count": len(shown),
            "kind": kind, "genre": genre, "venue": venue_id, "when": when,
            "kinds": listing.KIND_FILTERS, "whens": listing.WHEN_FILTERS,
            "genres": genres, "venues": venues,
        }
        template = "_event_list.html" if request.headers.get("HX-Request") else "events.html"
        return templates.TemplateResponse(request, template, ctx)
    finally:
        session.close()


@router.get("/concerts")
def concerts():
    return RedirectResponse("/events?kind=concert", status_code=301)


def _grouped_by_canonical(session, kind_filter, tz: str):
    now = utcnow()
    today = local_today(tz)
    events = session.scalars(select(Event).where(Event.status == "active", kind_filter)).all()
    rows = sorted((r for r in _rows(session, events, tz, now) if listing.is_current(r, today)),
                  key=listing.sort_key)
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
            "siblings": siblings, "changes": list(reversed(event.changes)),
        })
    finally:
        session.close()
