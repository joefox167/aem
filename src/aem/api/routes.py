from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from rapidfuzz import fuzz
from sqlalchemy import or_, select

from ..models import ChangeLog, ChangeType, Event, Venue, utcnow
from ..notify import digest as digest_mod

router = APIRouter()

DURATION_RE = re.compile(r"^(\d+)([hdw])$")
DURATION_UNITS = {"h": "hours", "d": "days", "w": "weeks"}


def _parse_window(value: str) -> timedelta:
    m = DURATION_RE.match(value.strip())
    if not m:
        raise HTTPException(400, f"bad duration '{value}', expected e.g. 24h, 7d, 2w")
    return timedelta(**{DURATION_UNITS[m.group(2)]: int(m.group(1))})


def _event_dict(event: Event, venue: Venue | None) -> dict:
    return {
        "id": event.id,
        "source": event.source,
        "kind": event.kind,
        "title": event.title,
        "venue": venue.name if venue else None,
        "venue_slug": venue.slug if venue else None,
        "starts_at": event.starts_at.isoformat() if event.starts_at else None,
        "ends_at": event.ends_at.isoformat() if event.ends_at else None,
        "event_url": event.event_url,
        "ticket_url": event.ticket_url,
        "ticket_status": event.ticket_status,
        "attrs": event.attrs,
        "status": event.status,
        "first_seen": event.first_seen.isoformat(),
        "last_seen": event.last_seen.isoformat(),
        "canonical_id": event.canonical_id,
    }


@router.get("/api/events")
def list_events(request: Request, kind: str | None = None, venue: str | None = None,
                status: str = "active", q: str | None = None,
                new_within: str | None = None,
                limit: int = Query(200, le=1000)):
    session = request.app.state.session_factory()
    try:
        stmt = select(Event).order_by(Event.starts_at.is_(None), Event.starts_at)
        if status != "all":
            stmt = stmt.where(Event.status == status)
        if kind:
            stmt = stmt.where(Event.kind == kind)
        if venue:
            venue_row = session.scalar(select(Venue).where(Venue.slug == venue))
            if venue_row is None:
                return []
            stmt = stmt.where(Event.venue_id == venue_row.id)
        if new_within:
            stmt = stmt.where(Event.first_seen >= utcnow() - _parse_window(new_within))
        if q:
            stmt = stmt.where(or_(Event.title.ilike(f"%{q}%"), Event.title_norm.ilike(f"%{q}%")))
        events = session.scalars(stmt.limit(limit)).all()
        return [_event_dict(e, session.get(Venue, e.venue_id)) for e in events]
    finally:
        session.close()


@router.get("/api/events/{event_id}")
def get_event(event_id: int, request: Request):
    session = request.app.state.session_factory()
    try:
        event = session.get(Event, event_id)
        if event is None:
            raise HTTPException(404, "event not found")
        data = _event_dict(event, session.get(Venue, event.venue_id))
        data["changes"] = [
            {
                "type": c.change_type,
                "fields": c.field_changes,
                "detected_at": c.detected_at.isoformat(),
            }
            for c in event.changes
        ]
        if event.canonical_id:
            siblings = session.scalars(
                select(Event).where(Event.canonical_id == event.canonical_id,
                                    Event.id != event.id)
            ).all()
            data["also_at"] = [_event_dict(s, session.get(Venue, s.venue_id)) for s in siblings]
        return data
    finally:
        session.close()


@router.get("/api/changes")
def list_changes(request: Request, since: str = "24h", type: str | None = None,
                 limit: int = Query(500, le=2000)):
    session = request.app.state.session_factory()
    try:
        stmt = (
            select(ChangeLog)
            .where(ChangeLog.detected_at >= utcnow() - _parse_window(since),
                   ChangeLog.change_type != ChangeType.baseline.value)
            .order_by(ChangeLog.detected_at.desc())
            .limit(limit)
        )
        if type:
            stmt = stmt.where(ChangeLog.change_type == type)
        out = []
        for change in session.scalars(stmt):
            event = session.get(Event, change.event_id)
            if event is None:
                continue
            venue = session.get(Venue, event.venue_id)
            out.append({
                "id": change.id,
                "type": change.change_type,
                "fields": change.field_changes,
                "detected_at": change.detected_at.isoformat(),
                "event": _event_dict(event, venue),
            })
        return out
    finally:
        session.close()


@router.get("/api/search")
def search(request: Request, q: str, limit: int = Query(50, le=200)):
    session = request.app.state.session_factory()
    try:
        ql = q.lower().strip()
        results = []
        for event in session.scalars(select(Event)):
            hay = " ".join(filter(None, [
                event.title.lower(),
                " ".join((event.attrs or {}).get("openers", [])).lower(),
                (event.attrs or {}).get("tour", "").lower(),
            ]))
            venue = session.get(Venue, event.venue_id)
            if venue:
                hay += " " + venue.name.lower()
            score = fuzz.partial_ratio(ql, hay) if len(ql) > 2 else (100 if ql in hay else 0)
            if ql in hay:
                score = 100
            if score >= 75:
                results.append((score, event, venue))
        results.sort(key=lambda r: (-r[0], r[1].starts_at or utcnow()))
        return [_event_dict(e, v) | {"score": s} for s, e, v in results[:limit]]
    finally:
        session.close()


@router.get("/api/venues")
def list_venues(request: Request):
    session = request.app.state.session_factory()
    try:
        return [
            {"slug": v.slug, "name": v.name, "source": v.source}
            for v in session.scalars(select(Venue).order_by(Venue.name))
        ]
    finally:
        session.close()


@router.get("/api/digest/preview")
def digest_preview(request: Request):
    session = request.app.state.session_factory()
    try:
        data = digest_mod.build_digest(session, request.app.state.cfg)
        html = digest_mod.render_digest(data, request.app.state.settings, "Preview")
        return Response(content=html, media_type="text/html")
    finally:
        session.close()


@router.post("/api/admin/poll")
async def admin_poll(request: Request, collector: str | None = None):
    from ..scheduler import run_poll

    state = request.app.state
    results = await run_poll(state.session_factory, state.collectors, state.settings,
                             state.cfg, only=collector)
    return results


@router.post("/api/admin/send-digest")
def admin_send_digest(request: Request, force: bool = False):
    session = request.app.state.session_factory()
    try:
        return digest_mod.send_digest(session, request.app.state.settings,
                                      request.app.state.cfg, force=force)
    finally:
        session.close()


@router.get("/healthz")
def healthz(request: Request):
    session = request.app.state.session_factory()
    try:
        session.execute(select(Venue.id).limit(1))
    finally:
        session.close()
    sched = getattr(request.app.state, "scheduler", None)
    return {
        "ok": True,
        "scheduler_running": bool(sched and sched.running),
    }


@router.get("/metrics")
def metrics_endpoint():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
