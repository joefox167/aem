import re

import pytest
import respx
from httpx import Response

from aem.collectors.acl_live import RSS_URL, AclLiveCollector
from aem.collectors.bullock_imax import FILMS_URL, STORE_URL, BullockImaxCollector
from aem.collectors.base import FetchContext
from aem.models import EventKind, TicketStatus

from .conftest import fixture_text


@pytest.fixture
def ctx(session_factory):
    with session_factory() as session:
        yield FetchContext(session, throttle_seconds=0)


@respx.mock
async def test_acl_live_parses_rss_and_details(ctx):
    respx.get(RSS_URL).mock(return_value=Response(200, text=fixture_text("acl_rss.xml")))
    respx.get(url__regex=r"https://www\.acllive\.com/events/detail/.*").mock(
        return_value=Response(200, text=fixture_text("acl_event.html")))

    events = await AclLiveCollector().fetch(ctx)

    assert len(events) >= 50
    assert all(e.kind == EventKind.concert for e in events)
    assert all(e.source_key.startswith("https://www.acllive.com/") for e in events)

    pretty = next(e for e in events if "Pretty Reckless" in e.title)
    # detail page fixture is served for every detail URL; this one is 2nd in feed
    assert pretty.attrs["tour"] == "Dear God Tour"
    assert pretty.attrs["openers"] == ["Paris Jackson", "doug."]
    assert pretty.ticket_status == TicketStatus.on_sale
    assert "axs.com" in pretty.ticket_url
    assert pretty.starts_at is not None

    scott = next(e for e in events if e.title == "Elijah Scott")
    assert scott.venue_slug == "acl-3ten"


@respx.mock
async def test_bullock_parses_store_and_listing(ctx):
    respx.get(url__regex=re.escape(STORE_URL) + ".*").mock(
        return_value=Response(200, text=fixture_text("bullock_store.html")))
    respx.get(FILMS_URL).mock(
        return_value=Response(200, text=fixture_text("bullock_films.html")))

    events = await BullockImaxCollector().fetch(ctx)

    assert len(events) >= 10
    assert all(e.kind == EventKind.movie for e in events)
    by_title = {e.title: e for e in events}

    odyssey = by_title["The Odyssey"]
    assert odyssey.source_key == "cat:1329"
    assert odyssey.attrs["format"] == "IMAX"
    assert odyssey.venue_slug == "bullock-imax"
    assert "categoryId=1329" in odyssey.ticket_url
    assert odyssey.ticket_status == TicketStatus.on_sale

    trex = by_title["T.REX 3D"]
    assert trex.attrs["format"] == "3D"

    shipwrecked = by_title["Shipwrecked"]
    assert shipwrecked.venue_slug == "bullock-tst"
    # listing page enriches with film page URL and run dates
    assert "thestoryoftexas.com/films/shipwrecked" in shipwrecked.event_url
    assert shipwrecked.starts_at is not None
    assert shipwrecked.ends_at is not None

    member = next(e for e in events if "Horse Power" in e.title
                  and e.attrs.get("special_presentation"))
    assert member.attrs["special_presentation"] == "Member Screening"

    # non-film categories are filtered out
    assert not any("Parking" in e.title or "Membership" in e.title or "Donation" in e.title
                   for e in events)
