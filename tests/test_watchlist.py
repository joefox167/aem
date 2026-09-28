from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from aem import watch
from aem.config import AppConfig
from aem.models import ChangeLog, Event, Venue, Watch, utcnow
from aem.notify import digest
from aem.web.routes import router as web_router


@pytest.fixture
def seeded(session_factory):
    now = utcnow()
    with session_factory() as s:
        moody = Venue(source="t", name="Moody Center ATX", slug="moody")
        emos = Venue(source="t", name="Emo's Austin", slug="emos")
        s.add_all([moody, emos])
        s.flush()
        lizzy = Event(source="t", source_key="l", venue_id=moody.id, kind="concert",
                      title="Lizzy McAlpine: The Over Country Tour", title_norm="lizzy",
                      content_hash="l", starts_at=now + timedelta(days=30),
                      first_seen=now - timedelta(days=9))
        other = Event(source="t", source_key="o", venue_id=emos.id, kind="concert",
                      title="Some Band", title_norm="some band", content_hash="o",
                      starts_at=now + timedelta(days=31), first_seen=now - timedelta(days=9),
                      attrs={"openers": ["Tiny Opener"]})
        s.add_all([lizzy, other])
        s.flush()
        s.add_all([ChangeLog(event_id=lizzy.id, change_type="added", field_changes={}),
                   ChangeLog(event_id=other.id, change_type="added", field_changes={})])
        s.commit()
        return {"moody": moody.id, "emos": emos.id, "lizzy": lizzy.id, "other": other.id}


@pytest.fixture
def client(session_factory, cfg):
    app = FastAPI()
    app.include_router(web_router)
    app.state.cfg = cfg
    app.state.session_factory = session_factory
    return TestClient(app)


def test_keyword_matches_title_and_lineup_case_insensitively(session_factory, seeded):
    with session_factory() as s:
        watch.add_keyword(s, "  mcALPINE ")
        watch.add_keyword(s, "tiny opener")
        assert watch.add_keyword(s, "x") is None  # too short to be useful
        wl = watch.load(s)
        assert wl.matches(s.get(Event, seeded["lizzy"]))
        assert wl.matches(s.get(Event, seeded["other"]))  # via the openers
        assert [w.label for w in s.scalars(select(Watch).order_by(Watch.id))] == \
            ["mcALPINE", "tiny opener"]


def test_venue_watch_and_duplicates(session_factory, seeded):
    with session_factory() as s:
        a = watch.add_venue(s, seeded["emos"])
        b = watch.add_venue(s, seeded["emos"])
        assert a.id == b.id and a.label == "Emo's Austin"
        assert watch.add_venue(s, 9999) is None
        wl = watch.load(s)
        assert wl.matches(s.get(Event, seeded["other"]))
        assert not wl.matches(s.get(Event, seeded["lizzy"]))


def test_add_and_remove_through_the_pages(client, session_factory, seeded):
    resp = client.post("/watchlist/add", content="keyword=McAlpine&next=/event/1",
                       headers={"content-type": "application/x-www-form-urlencoded"},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/event/1"
    # an off-site next is refused
    resp = client.post("/watchlist/add", content="keyword=Band&next=https://evil.example/",
                       headers={"content-type": "application/x-www-form-urlencoded"},
                       follow_redirects=False)
    assert resp.headers["location"] == "/watchlist"

    html = client.get("/").text
    assert "Your watchlist" in html and "★" in html
    assert "Lizzy McAlpine" in client.get("/events?watched=1").text
    assert "Some Band" in client.get("/events?watched=1").text  # "Band" keyword
    page = client.get("/watchlist").text
    assert "McAlpine" in page and "1 upcoming show" in page

    with session_factory() as s:
        wid = s.scalar(select(Watch).where(Watch.value == "mcalpine")).id
    client.post("/watchlist/remove", content=f"id={wid}",
                headers={"content-type": "application/x-www-form-urlencoded"})
    with session_factory() as s:
        assert s.scalar(select(Watch).where(Watch.value == "mcalpine")) is None
    assert "<strong>McAlpine</strong>" not in client.get("/watchlist").text


def test_event_page_offers_watch_buttons(client, seeded):
    html = client.get(f"/event/{seeded['lizzy']}").text
    assert "☆ Watch Lizzy McAlpine: The Over Country Tour" in html
    assert "☆ Watch Moody Center ATX" in html


def test_empty_watchlist_shows_a_hint_not_a_section(client, seeded):
    html = client.get("/").text
    assert "Start a watchlist" in html and "Your watchlist" not in html


def test_digest_puts_watched_items_first(session_factory, seeded):
    with session_factory() as s:
        watch.add_keyword(s, "mcalpine")
        data = digest.build_digest(s, AppConfig())
    assert [i["event"].title for i in data["watchlist"]] == ["Lizzy McAlpine: The Over Country Tour"]
    assert "Moody Center ATX" not in data["concerts_added"]  # moved, not repeated
    assert data["listed"] == 2
    html = digest.render_digest(data, __import__("aem.config").config.Settings(), "Testday")
    assert html.index("Watchlist") < html.index("Concerts")
