from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from .. import changes, watch
from ..fmt import local_stamp, local_today
from ..models import ChangeLog, ChangeType, Event, TicketStatus, Venue, Watch, utcnow
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
    wl = watch.load(session)
    rows = [listing.build_row(e, session.get(Venue, e.venue_id), tz, now) for e in events]
    for row in rows:
        row.watched = wl.matches(row.event)
    return rows


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

        watched_shows = listing.group_shows([r for r in rows if r.watched and r.day])
        # one row per show: a touring musical's 16 new performances are one announcement
        new_shows = listing.group_shows([r for r in rows if listing.is_new(r)])

        upcoming_sections = listing.sections(upcoming, today)
        return templates.TemplateResponse(request, "index.html", _ctx(request) | {
            "new_shows": new_shows[:NEW_SHOWN], "new_count": len(new_shows),
            "on_sale_soon": on_sale_soon,
            "watched_shows": watched_shows[:DASHBOARD_PER_SECTION],
            "watched_count": len(watched_shows), "has_watchlist": bool(watch.load(session)),
            "upcoming": upcoming_sections, "upcoming_count": _count(upcoming_sections),
            "upcoming_days": UPCOMING_DAYS, "per_section": DASHBOARD_PER_SECTION,
            "weekend": weekend, "weekend_start": weekend_start, "weekend_end": weekend_end,
            "weekend_runs": weekend_runs, "weekend_title": listing.weekend_title(today),
        })
    finally:
        session.close()


@router.get("/events", response_class=HTMLResponse)
def events_page(request: Request, kind: str = "", genre: str = "", venue: str = "",
                when: str = "", new: str = "", day: str = "", page: int = 1,
                watched: str = ""):
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
        only_watched = watched == "1"
        genres, venues = listing.facet_counts(
            listing.apply_filters(rows, today, kind=kind, new=only_new, watched=only_watched))
        # a genre/venue picked under another kind would silently match nothing
        if genre not in {g for g, _ in genres}:
            genre = ""
        if venue_id not in {v for v, _, _ in venues}:
            venue_id = None
        shown = listing.apply_filters(rows, today, kind=kind, genre=genre,
                                      venue=venue_id, when=when, new=only_new, day=on_day,
                                      watched=only_watched)
        all_sections = listing.sections(shown, today)
        page = max(page, 1)
        page_sections, continued, has_more = listing.paginate(all_sections, page)
        params = {k: v for k, v in {"kind": kind, "genre": genre, "venue": venue_id or "",
                                    "when": when, "new": "1" if only_new else "",
                                    "day": on_day.isoformat() if on_day else "",
                                    "watched": "1" if only_watched else ""}.items() if v}
        ctx = _ctx(request) | {
            "sections": page_sections, "count": _count(all_sections),
            "continued": continued, "next_page": page + 1 if has_more else None,
            "next_url": "/events?" + urlencode(params | {"page": page + 1}),
            "day": on_day, "watched": only_watched,
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


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request):
    tz = request.app.state.cfg.timezone
    session = request.app.state.session_factory()
    try:
        now = utcnow()
        today = local_today(tz)
        rows = _current_rows(session, tz, now, today)
        entries = []
        for w in session.scalars(select(Watch).order_by(Watch.kind, Watch.label)):
            one = watch.Watchlist(keywords=(w.value,)) if w.kind == "keyword" else \
                watch.Watchlist(venue_ids=frozenset({int(w.value)}))
            shows = listing.group_shows([r for r in rows if r.day and one.matches(r.event)])
            entries.append({"watch": w, "shows": len(shows)})
        venues = session.scalars(select(Venue).order_by(Venue.name)).all()
        return templates.TemplateResponse(request, "watchlist.html", _ctx(request) | {
            "entries": entries, "venues": venues,
        })
    finally:
        session.close()


async def _form(request: Request) -> dict[str, str]:
    """application/x-www-form-urlencoded fields, without pulling in
    python-multipart for two tiny forms."""
    body = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[0] for k, v in parse_qs(body).items()}


def _back(target: str) -> RedirectResponse:
    # only ever redirect within AEM
    if not target.startswith("/") or target.startswith("//"):
        target = "/watchlist"
    return RedirectResponse(target, status_code=303)


@router.post("/watchlist/add")
async def watchlist_add(request: Request):
    form = await _form(request)
    session = request.app.state.session_factory()
    try:
        if form.get("venue", "").isdigit():
            watch.add_venue(session, int(form["venue"]))
        elif form.get("keyword"):
            watch.add_keyword(session, form["keyword"])
    finally:
        session.close()
    return _back(form.get("next", "/watchlist"))


@router.post("/watchlist/remove")
async def watchlist_remove(request: Request):
    form = await _form(request)
    session = request.app.state.session_factory()
    try:
        if form.get("id", "").isdigit():
            watch.remove(session, int(form["id"]))
    finally:
        session.close()
    return _back(form.get("next", "/watchlist"))


@router.get("/concerts")
def concerts():
    return RedirectResponse("/events?kind=concert", status_code=301)


LONG_RUN_DAYS = 14


@router.get("/movies", response_class=HTMLResponse)
def movies(request: Request):
    """Dated screenings first, date-grouped like Events; films on long runs
    (a museum's year-round IMAX catalogue) in one compact list after them."""
    tz = request.app.state.cfg.timezone
    session = request.app.state.session_factory()
    try:
        now = utcnow()
        today = local_today(tz)
        events = session.scalars(select(Event).where(Event.status == "active",
                                                     Event.kind == "movie")).all()
        rows = [r for r in _rows(session, events, tz, now) if listing.is_current(r, today, now)]
        long_runs = [r for r in rows if r.end_day and r.day
                     and (r.end_day - r.day).days > LONG_RUN_DAYS]
        screenings = [r for r in rows if r not in long_runs]

        films: dict = {}
        for r in sorted(long_runs, key=lambda r: r.event.title.lower()):
            film = films.setdefault(r.event.canonical_id or r.event.title.strip().lower(),
                                    {"row": r, "venues": [], "until": r.end_day})
            film["venues"].append(r.venue.name if r.venue else "?")
            film["until"] = max(film["until"], r.end_day)
        return templates.TemplateResponse(request, "movies.html", _ctx(request) | {
            "sections": listing.sections(screenings, today), "films": list(films.values()),
        })
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
        row = listing.build_row(event, venue, tz, now)
        wl = watch.load(session)
        row.watched = wl.matches(event)
        existing = {(w.kind, w.value): w for w in session.scalars(select(Watch))}
        names = list((event.attrs or {}).get("performers") or []) or [event.title]
        watch_options = [("keyword", n, existing.get(("keyword", n.lower()))) for n in names[:4]]
        if venue is not None:
            watch_options.append(("venue", venue.name, existing.get(("venue", str(venue.id)))))
        return templates.TemplateResponse(request, "event.html", _ctx(request) | {
            "event": event, "venue": venue, "row": row, "watch_options": watch_options,
            "siblings": siblings,
            "history": [(c, *changes.describe_change(c, tz)) for c in reversed(event.changes)
                        if c.change_type != ChangeType.baseline.value and changes.is_news(c)],
        })
    finally:
        session.close()
