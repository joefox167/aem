from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aem.fmt import local_day, local_time, price_label, upcoming_sale
from aem.models import Event, Venue, utcnow
from aem.web import listing
from aem.web.routes import router as web_router

TZ = "America/Chicago"
NOW = datetime(2026, 10, 1, 17, 0)  # Thu Oct 1, noon in Austin
TODAY = date(2026, 10, 1)


def _event(title="Show", kind="concert", starts_at=None, ends_at=None, attrs=None,
           ticket_status="on_sale", first_seen=datetime(2026, 1, 1), venue_id=1, eid=1):
    return Event(id=eid, source="t", source_key=title, venue_id=venue_id, kind=kind,
                 title=title, title_norm=title.lower(), starts_at=starts_at, ends_at=ends_at,
                 attrs=attrs or {}, ticket_status=ticket_status, status="active",
                 content_hash="x", first_seen=first_seen)


def _row(**kw):
    return listing.build_row(_event(**kw), None, TZ, NOW)


# --- fmt helpers ----------------------------------------------------------

def test_local_day_converts_instants_but_keeps_date_only_values():
    # 01:00 UTC Oct 2 is 8 PM Oct 1 in Austin
    assert local_day(datetime(2026, 10, 2, 1, 0), TZ) == date(2026, 10, 1)
    # UTC midnight means "a date, no showtime": must not slide to the previous day
    assert local_day(datetime(2026, 10, 2), TZ) == date(2026, 10, 2)
    assert local_time(datetime(2026, 10, 2), TZ) is None
    assert local_time(datetime(2026, 10, 2, 1, 0), TZ) == "8:00 PM"


def test_upcoming_sale_ignores_placeholder_and_past():
    assert upcoming_sale({"public_sale_start": "9999-12-31T06:00:00Z"}, NOW) is None
    assert upcoming_sale({"public_sale_start": "2026-09-01T15:00:00Z"}, NOW) is None
    assert upcoming_sale({"public_sale_start": "2026-10-03T15:00:00Z"}, NOW) == \
        datetime(2026, 10, 3, 15, 0)
    assert upcoming_sale({}, NOW) is None


@pytest.mark.parametrize("attrs,expected", [
    ({"price_min": 27.62, "price_max": 104.87}, "$28–$105"),
    ({"price_min": 28.61, "price_max": 28.61}, "$29"),
    ({"price_min": 0.0, "price_max": 0.0}, None),
    ({}, None),
])
def test_price_label(attrs, expected):
    assert price_label(attrs) == expected


# --- badges ---------------------------------------------------------------

def test_on_sale_is_not_badged_but_sold_out_and_new_are():
    assert _row().badges == []
    assert _row(ticket_status="unknown").badges == []
    assert ("sold-out", "Sold out") in _row(ticket_status="sold_out").badges
    assert ("new", "New") in _row(first_seen=NOW - timedelta(hours=3)).badges


def test_upcoming_sale_badge_uses_local_time():
    row = _row(ticket_status="coming_soon", attrs={"public_sale_start": "2026-10-03T15:00:00Z"})
    assert row.badges == [("soon", "On sale Sat Oct 3, 10:00 AM")]


def test_sold_out_placeholder_sale_date_shows_no_sale_badge():
    row = _row(ticket_status="sold_out", attrs={"public_sale_start": "9999-12-31T06:00:00Z"})
    assert row.badges == [("sold-out", "Sold out")]
    assert row.sale_at is None


# --- sections -------------------------------------------------------------

@pytest.mark.parametrize("day,label", [
    (date(2026, 9, 30), "Now showing"),
    (date(2026, 10, 1), "Today"),       # Thu
    (date(2026, 10, 2), "Tomorrow"),    # Fri: tomorrow wins over weekend
    (date(2026, 10, 3), "This weekend"),
    (date(2026, 10, 4), "This weekend"),
    (date(2026, 10, 5), "Next week"),
    (date(2026, 10, 11), "Next week"),
    (date(2026, 10, 12), "October"),
    (date(2027, 1, 5), "January 2027"),
    (None, "Date TBA"),
])
def test_section_label(day, label):
    assert listing.section_label(day, TODAY) == label


def test_section_label_this_week_before_weekend():
    monday = date(2026, 9, 28)
    assert listing.section_label(date(2026, 9, 30), monday) == "This week"
    assert listing.section_label(date(2026, 10, 2), monday) == "This weekend"


def test_sections_are_chronological_with_tba_last():
    rows = [
        _row(title="tba", eid=1),
        _row(title="later", starts_at=datetime(2026, 11, 5, 2), eid=2),
        _row(title="today", starts_at=datetime(2026, 10, 2, 1), eid=3),
        _row(title="run", starts_at=datetime(2026, 9, 20), ends_at=datetime(2026, 10, 20),
             kind="movie", eid=4),
    ]
    got = [(label, [r.event.title for r in rs]) for label, rs in listing.sections(rows, TODAY)]
    assert got == [("Now showing", ["run"]), ("Today", ["today"]),
                   ("November", ["later"]), ("Date TBA", ["tba"])]


def test_is_current_hides_past_events_but_keeps_running_ones():
    assert not listing.is_current(_row(starts_at=datetime(2026, 9, 20, 2)), TODAY)
    assert listing.is_current(_row(starts_at=datetime(2026, 9, 20),
                                   ends_at=datetime(2026, 10, 5)), TODAY)
    assert listing.is_current(_row(), TODAY)


# --- filters --------------------------------------------------------------

def test_filters_by_kind_genre_venue_and_window():
    rows = [
        _row(title="rock", attrs={"genre": "Rock"}, starts_at=datetime(2026, 10, 3, 2), eid=1),
        _row(title="film", kind="movie", starts_at=datetime(2026, 10, 20), venue_id=2, eid=2),
        _row(title="play", kind="live_performance", starts_at=datetime(2026, 10, 6, 1), eid=3),
        _row(title="tba", eid=4),
    ]

    def titles(**kw):
        return [r.event.title for r in listing.apply_filters(rows, TODAY, **kw)]

    assert titles(kind="movie") == ["film"]
    assert titles(kind="live") == ["play"]
    assert titles(kind="bogus") == ["rock", "film", "play", "tba"]
    assert titles(genre="Rock") == ["rock"]
    assert titles(venue=2) == ["film"]
    assert titles(when="weekend") == ["rock"]
    assert titles(when="7d") == ["rock", "play"]
    assert titles(when="30d") == ["rock", "film", "play"]


def test_weekend_filter_includes_a_run_spanning_it():
    run = _row(kind="movie", starts_at=datetime(2026, 9, 1), ends_at=datetime(2026, 12, 1))
    assert listing.apply_filters([run], TODAY, when="weekend") == [run]


# --- pages ----------------------------------------------------------------

@pytest.fixture
def client(session_factory, cfg):
    now = utcnow()
    with session_factory() as s:
        venue = Venue(source="t", name="Emo's Austin", slug="emos")
        s.add(venue)
        s.flush()
        s.add_all([
            Event(source="t", source_key="a", venue_id=venue.id, kind="concert",
                  title="Soon Band", title_norm="soon band", content_hash="a",
                  starts_at=now + timedelta(days=2), attrs={"genre": "Rock"},
                  ticket_status="sold_out"),
            Event(source="t", source_key="b", venue_id=venue.id, kind="movie",
                  title="Old Film", title_norm="old film", content_hash="b",
                  starts_at=now - timedelta(days=30)),
            Event(source="t", source_key="c", venue_id=venue.id, kind="comedy",
                  title="Presale Comic", title_norm="presale comic", content_hash="c",
                  starts_at=now + timedelta(days=40), ticket_status="presale"),
        ])
        s.commit()
    app = FastAPI()
    app.include_router(web_router)
    app.state.cfg = cfg
    app.state.session_factory = session_factory
    return TestClient(app)


def test_dashboard_renders_sections(client):
    html = client.get("/").text
    assert "Going on sale soon" in html and "Presale Comic" in html
    assert "Soon Band" in html and "Sold out" in html
    assert "Old Film" not in html


def test_events_page_filters_and_hides_past(client):
    html = client.get("/events").text
    assert "Soon Band" in html and "Presale Comic" in html
    assert "Old Film" not in html
    assert "2 events" in html

    html = client.get("/events?kind=comedy").text
    assert "Presale Comic" in html and "Soon Band" not in html


def test_events_htmx_returns_partial(client):
    html = client.get("/events?kind=concert", headers={"HX-Request": "true"}).text
    assert html.lstrip().startswith("{#") or '<div id="browse">' in html
    assert "<html" not in html


def test_stale_genre_is_dropped_when_kind_changes(client):
    html = client.get("/events?kind=comedy&genre=Rock").text
    assert "Presale Comic" in html


def test_concerts_redirects_to_events(client):
    resp = client.get("/concerts", follow_redirects=False)
    assert resp.status_code == 301 and resp.headers["location"] == "/events?kind=concert"
