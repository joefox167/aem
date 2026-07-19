from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..models import ChangeLog, ChangeType, Event, Venue, utcnow

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _fmt_local(dt, tz_name: str, with_time: bool = True):
    if dt is None:
        return "TBA"
    local = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz_name))
    fmt = "%a %b %d, %Y" + (" %I:%M %p" if with_time and (local.hour or local.minute) else "")
    return local.strftime(fmt)


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


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    session = request.app.state.session_factory()
    try:
        day_ago = utcnow() - timedelta(hours=24)
        week_ago = utcnow() - timedelta(days=7)
        new_today = _hydrate_changes(session, session.scalars(
            select(ChangeLog)
            .where(ChangeLog.change_type == ChangeType.added.value,
                   ChangeLog.detected_at >= day_ago)
            .order_by(ChangeLog.detected_at.desc()).limit(50)
        ))
        recent = _hydrate_changes(session, session.scalars(
            select(ChangeLog)
            .where(ChangeLog.change_type != ChangeType.baseline.value,
                   ChangeLog.detected_at >= week_ago)
            .order_by(ChangeLog.detected_at.desc()).limit(100)
        ))
        upcoming = session.scalars(
            select(Event)
            .where(Event.status == "active",
                   Event.starts_at.isnot(None),
                   Event.starts_at >= utcnow(),
                   Event.starts_at <= utcnow() + timedelta(days=14))
            .order_by(Event.starts_at).limit(100)
        ).all()
        upcoming = [{"event": e, "venue": session.get(Venue, e.venue_id)} for e in upcoming]
        return templates.TemplateResponse(request, "index.html", _ctx(request) | {
            "new_today": new_today, "recent": recent, "upcoming": upcoming,
        })
    finally:
        session.close()


def _grouped_by_canonical(session, kind_filter):
    events = session.scalars(
        select(Event).where(Event.status == "active", kind_filter)
        .order_by(Event.starts_at.is_(None), Event.starts_at)
    ).all()
    groups: dict = {}
    order = []
    for event in events:
        key = event.canonical_id or f"e{event.id}"
        if key not in groups:
            groups[key] = {"title": event.title, "entries": []}
            order.append(key)
        groups[key]["entries"].append({
            "event": event, "venue": session.get(Venue, event.venue_id),
        })
    return [groups[k] for k in order]


@router.get("/movies", response_class=HTMLResponse)
def movies(request: Request):
    session = request.app.state.session_factory()
    try:
        groups = _grouped_by_canonical(session, Event.kind == "movie")
        return templates.TemplateResponse(request, "movies.html", _ctx(request) | {"groups": groups})
    finally:
        session.close()


@router.get("/concerts", response_class=HTMLResponse)
def concerts(request: Request):
    session = request.app.state.session_factory()
    try:
        groups = _grouped_by_canonical(session, Event.kind != "movie")
        return templates.TemplateResponse(request, "concerts.html", _ctx(request) | {"groups": groups})
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
    session = request.app.state.session_factory()
    try:
        event = session.get(Event, event_id)
        if event is None:
            raise HTTPException(404, "event not found")
        venue = session.get(Venue, event.venue_id)
        siblings = []
        if event.canonical_id:
            siblings = [
                {"event": s, "venue": session.get(Venue, s.venue_id)}
                for s in session.scalars(
                    select(Event).where(Event.canonical_id == event.canonical_id,
                                        Event.id != event.id))
            ]
        return templates.TemplateResponse(request, "event.html", _ctx(request) | {
            "event": event, "venue": venue, "siblings": siblings,
            "changes": list(reversed(event.changes)),
        })
    finally:
        session.close()
