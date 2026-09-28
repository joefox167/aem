from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

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


def test_sections_are_chronological_with_runs_then_tba_last():
    rows = [
        _row(title="tba", eid=1),
        _row(title="later", starts_at=datetime(2026, 11, 5, 2), eid=2),
        _row(title="today", starts_at=datetime(2026, 10, 2, 1), eid=3),
        _row(title="run", starts_at=datetime(2026, 9, 20), ends_at=datetime(2026, 10, 20),
             kind="movie", eid=4),
    ]
    got = [(label, [r.event.title for r in rs]) for label, rs in listing.sections(rows, TODAY)]
    # long runs go after the dated events, so they don't head every list
    assert got == [("Today", ["today"]), ("November", ["later"]),
                   ("Now showing", ["run"]), ("Date TBA", ["tba"])]


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
    assert titles(genre="Rock & Metal") == ["rock"]
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
        old = now - timedelta(days=30)  # not "new", unless a test says so
        s.add_all([
            Event(source="t", source_key="a", venue_id=venue.id, kind="concert",
                  title="Soon Band", title_norm="soon band", content_hash="a",
                  starts_at=now + timedelta(days=2), attrs={"genre": "Rock"},
                  ticket_status="sold_out", first_seen=old),
            Event(source="t", source_key="b", venue_id=venue.id, kind="movie",
                  title="Old Film", title_norm="old film", content_hash="b",
                  starts_at=now - timedelta(days=30), first_seen=old),
            Event(source="t", source_key="c", venue_id=venue.id, kind="comedy",
                  title="Presale Comic", title_norm="presale comic", content_hash="c",
                  starts_at=now + timedelta(days=40), ticket_status="presale",
                  first_seen=old),
        ])
        s.commit()
    app = FastAPI()
    app.include_router(web_router)
    app.state.cfg = cfg
    app.state.session_factory = session_factory
    return TestClient(app)


def test_dashboard_weekend_rows_are_not_repeated(client, session_factory):
    html = client.get("/").text
    assert "This weekend" in html or "Next weekend" in html
    # "Soon Band" is on the dashboard once, whether it lands on the weekend or not
    assert html.count('class="title" href="/event/1"') == 1


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


# --- sale timing ----------------------------------------------------------

def test_next_sale_picks_earliest_real_sale():
    from aem.fmt import next_sale
    attrs = {"presale_start": "2026-10-02T15:00:00Z", "public_sale_start": "2026-10-03T15:00:00Z"}
    assert next_sale(attrs, NOW) == (datetime(2026, 10, 2, 15, 0), "Presale")
    assert next_sale({"public_sale_start": "9999-12-31T06:00:00Z"}, NOW) is None


def test_upcoming_presale_badge():
    row = _row(ticket_status="coming_soon",
               attrs={"presale_start": "2026-10-02T15:00:00Z",
                      "public_sale_start": "2026-10-03T15:00:00Z"})
    assert row.badges == [("soon", "Presale Fri Oct 2, 10:00 AM")]


def test_presale_on_now_also_shows_public_date():
    row = _row(ticket_status="presale", attrs={"public_sale_start": "2026-10-03T15:00:00Z"})
    assert row.badges == [("presale", "Presale on now"), ("soon", "On sale Sat Oct 3, 10:00 AM")]


# --- weekend --------------------------------------------------------------

def test_weekend_sections_by_day_with_runs_counted_not_listed():
    rows = [
        _row(title="sat", starts_at=datetime(2026, 10, 4, 1), eid=1),   # Sat 8 PM local
        _row(title="sun", starts_at=datetime(2026, 10, 4, 20), eid=2),  # Sun 3 PM local
        _row(title="run", kind="movie", starts_at=datetime(2026, 9, 1),
             ends_at=datetime(2026, 12, 1), eid=3),
        _row(title="mon", starts_at=datetime(2026, 10, 6, 1), eid=4),
    ]
    secs, runs = listing.weekend_sections(rows, TODAY)
    assert [(label, [r.event.title for r in rs]) for label, rs in secs] == \
        [("Saturday", ["sat"]), ("Sunday", ["sun"])]
    assert runs == 1


def test_on_sunday_the_weekend_is_next_weekend():
    sunday = date(2026, 10, 4)
    assert listing.weekend_range(sunday) == (date(2026, 10, 9), date(2026, 10, 11))
    assert listing.weekend_title(sunday) == "Next weekend"
    assert listing.weekend_title(date(2026, 10, 3)) == "This weekend"
    rows = [_row(title="tonight", starts_at=datetime(2026, 10, 5, 1), eid=1),  # Sun 8 PM
            _row(title="next fri", starts_at=datetime(2026, 10, 10, 1), eid=2)]
    secs, _ = listing.weekend_sections(rows, sunday)
    assert [(label, [r.event.title for r in rs]) for label, rs in secs] == \
        [("Friday", ["next fri"])]


def test_timed_show_is_over_three_hours_after_it_starts():
    matinee = _row(starts_at=datetime(2026, 10, 1, 18))   # 1 PM local, today
    evening = _row(starts_at=datetime(2026, 10, 2, 0, 30))  # 7:30 PM local, today
    date_only = _row(starts_at=datetime(2026, 10, 1))
    at_8pm = datetime(2026, 10, 2, 1, 0)
    assert not listing.is_current(matinee, TODAY, at_8pm)
    assert listing.is_current(evening, TODAY, at_8pm)
    assert listing.is_current(date_only, TODAY, at_8pm)


def test_matinee_and_evening_merge_into_one_row():
    rows = [
        _row(title="Mrs. Doubtfire", starts_at=datetime(2026, 10, 3, 18), eid=1,
             ticket_status="sold_out"),
        _row(title="Mrs. Doubtfire", starts_at=datetime(2026, 10, 3, 23, 30), eid=2),
        _row(title="Mrs. Doubtfire", starts_at=datetime(2026, 10, 4, 18), eid=3),
    ]
    merged = listing.merge_performances(rows)
    assert [(r.event.id, r.times) for r in merged] == \
        [(1, "1:00 PM & 6:30 PM"), (3, "1:00 PM")]
    # the evening show isn't sold out, so the merged row isn't either
    assert ("sold-out", "Sold out") not in merged[0].badges
    # merging works on copies: the inputs are untouched and a second pass is identical
    assert rows[0].extra_times == [] and rows[0].badges == [("sold-out", "Sold out")]
    assert [r.times for r in listing.merge_performances(rows)] == ["1:00 PM & 6:30 PM", "1:00 PM"]


def test_duplicate_listing_does_not_repeat_the_time():
    rows = [_row(title="Sara Bareilles", starts_at=datetime(2026, 10, 9, 1), eid=1),
            _row(title="Sara Bareilles", starts_at=datetime(2026, 10, 9, 1), eid=2)]
    assert [r.times for r in listing.merge_performances(rows)] == ["8:00 PM"]


def test_timed_shows_sort_before_date_only_on_the_same_day():
    rows = [_row(title="date only", starts_at=datetime(2026, 10, 3), eid=1),
            _row(title="noon", starts_at=datetime(2026, 10, 3, 17), eid=2)]
    assert [r.event.title for r in sorted(rows, key=listing.sort_key)] == ["noon", "date only"]


def test_group_shows_collapses_a_run_of_performances():
    rows = [_row(title="Harry Potter", starts_at=datetime(2027, 4, d, 1), eid=d)
            for d in (6, 7, 8)] + [_row(title="Solo", starts_at=datetime(2027, 4, 7, 1), eid=9)]
    grouped = listing.group_shows(rows)
    assert [(r.event.title, r.performances, r.day, r.end_day, r.times) for r in grouped] == [
        ("Harry Potter", 3, date(2027, 4, 5), date(2027, 4, 7), None),
        ("Solo", 1, date(2027, 4, 6), None, "8:00 PM"),
    ]
    assert rows[0].performances == 1  # inputs untouched


def test_new_filter_uses_the_new_badge():
    fresh = _row(title="fresh", first_seen=NOW - timedelta(hours=5), eid=1)
    stale = _row(title="stale", eid=2)
    assert listing.apply_filters([fresh, stale], TODAY, new=True) == [fresh]


def test_just_announced_groups_performances(client, session_factory):
    now = utcnow()
    with session_factory() as s:
        for d in range(3):
            s.add(Event(source="t", source_key=f"hp{d}", venue_id=1, kind="live_performance",
                        title="Harry Potter", title_norm="harry potter", content_hash="h",
                        starts_at=now + timedelta(days=60 + d)))
        s.commit()
    html = client.get("/").text
    assert "Just announced" in html and "3 performances" in html
    assert html.count(">Harry Potter</a>") == 1
    html = client.get("/events?new=1").text
    assert "Harry Potter" in html and "Presale Comic" not in html



# --- paging / day filter ----------------------------------------------------

def test_paginate_continues_a_section_without_repeating_it():
    rows = [_row(title=f"e{i}", starts_at=datetime(2026, 10, 20, 1) + timedelta(minutes=i),
                 eid=i) for i in range(5)]
    secs = listing.sections(rows, TODAY)
    page1, cont1, more1 = listing.paginate(secs, 1, size=3)
    page2, cont2, more2 = listing.paginate(secs, 2, size=3)
    assert [len(r) for _, r in page1] == [3] and not cont1 and more1
    assert [len(r) for _, r in page2] == [2] and cont2 and not more2


def test_day_filter_matches_that_day_only():
    fri = _row(title="fri", starts_at=datetime(2026, 10, 3, 1), eid=1)
    run = _row(title="run", kind="movie", starts_at=datetime(2026, 9, 1),
               ends_at=datetime(2026, 12, 1), eid=2)
    assert listing.apply_filters([fri, run], TODAY, day=date(2026, 10, 2)) == [fri]


def test_events_show_more_returns_just_the_next_page(client, session_factory):
    now = utcnow()
    with session_factory() as s:
        for i in range(70):
            s.add(Event(source="t", source_key=f"p{i}", venue_id=1, kind="concert",
                        title=f"Band {i:02d}", title_norm=f"band {i}", content_hash="p",
                        starts_at=now + timedelta(days=20, minutes=i), first_seen=now - timedelta(days=9)))
        s.commit()
    html = client.get("/events").text
    assert "Show more" in html and "72 events" in html
    more = client.get("/events?page=2", headers={"HX-Request": "true"}).text
    assert "<html" not in more and 'id="browse"' not in more
    assert "Band 69" in more and "Show more" not in more


def test_dashboard_caps_sections_and_links_to_the_day(client, session_factory):
    from aem.fmt import local_today
    now = utcnow()
    with session_factory() as s:
        for i in range(8):
            s.add(Event(source="t", source_key=f"d{i}", venue_id=1, kind="concert",
                        title=f"Tomorrow Act {i}", title_norm=f"tomorrow act {i}", content_hash="d",
                        starts_at=now + timedelta(days=1, minutes=i), first_seen=now - timedelta(days=9)))
        s.commit()
    html = client.get("/").text
    shown = html.count(">Tomorrow Act ")
    assert shown <= 5
    tomorrow = local_today("America/Chicago") + timedelta(days=1)
    assert "See all" in html and ("/events?day=" + tomorrow.isoformat()) in html
    assert "Recent changes" not in html and 'href="/changes"' in html


@pytest.mark.parametrize("attrs,kind,label", [
    ({"genre": "Rock"}, "concert", "Rock & Metal"),
    ({"genre": "Metal"}, "concert", "Rock & Metal"),
    ({"genre": "Hip-Hop/Rap"}, "concert", "Hip-Hop & R&B"),
    ({"segment": "Arts & Theatre", "genre": "Performance Art"}, "live_performance", "Theater"),
    ({"genre": "Theatre"}, "live_performance", "Theater"),
    ({"genre": "Religious"}, "concert", "Music"),     # unmapped -> the kind
    ({}, "concert", "Music"),
    ({}, "live_performance", "Theater"),
    ({}, "movie", "Film"),
])
def test_categories_collapse_to_one_vocabulary(attrs, kind, label):
    from aem.fmt import category
    assert category(_event(kind=kind, attrs=attrs)) == label



def test_movies_page_splits_screenings_from_long_runs(client, session_factory):
    now = utcnow()
    with session_factory() as s:
        s.add_all([
            Event(source="t", source_key="m1", venue_id=1, kind="movie", title="Jaws",
                  title_norm="jaws", content_hash="m", starts_at=now + timedelta(days=3),
                  first_seen=now - timedelta(days=9)),
            Event(source="t", source_key="m2", venue_id=1, kind="movie", title="Horse Power",
                  title_norm="horse power", content_hash="m", starts_at=now - timedelta(days=100),
                  ends_at=now + timedelta(days=200), first_seen=now - timedelta(days=9)),
        ])
        s.commit()
    html = client.get("/movies").text
    assert html.index("Screenings") < html.index("Jaws") < html.index("Always showing") \
        < html.index("Horse Power")
    assert html.count(">Horse Power<") == 1 or html.count("Horse Power</strong>") == 1


def test_filters_fold_behind_a_toggle(client):
    html = client.get("/events").text
    assert '<details class="more-filters">' in html          # closed by default
    html = client.get("/events?when=7d").text
    assert '<details class="more-filters" open>' in html      # an active filter opens it


def test_pictures_only_in_highlights_and_on_event_pages(client, session_factory):
    from aem import watch
    with session_factory() as s:
        ev = s.scalar(select(Event).where(Event.title == "Soon Band"))
        ev.attrs = {**ev.attrs, "image_url": "https://img.example/soon.jpg"}
        s.commit()
        watch.add_keyword(s, "Soon Band")
        eid = ev.id
    html = client.get("/").text
    # the watchlist highlight shows it; the weekend/coming-up rows stay text-only
    assert html.count('src="https://img.example/soon.jpg"') == 1
    assert 'referrerpolicy="no-referrer"' in html and 'onerror="this.remove()"' in html
    assert 'src="https://img.example/soon.jpg"' not in client.get("/events").text
    assert 'class="hero" src="https://img.example/soon.jpg"' in client.get(f"/event/{eid}").text
