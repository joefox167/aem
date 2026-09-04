import json
import re
from datetime import datetime

import pytest
import respx
from httpx import Response

from aem.collectors.acl_live import RSS_URL, AclLiveCollector
from aem.collectors.base import FetchContext, ParseDriftError
from aem.collectors.bullock_imax import FILMS_URL, STORE_URL, BullockImaxCollector
from aem.collectors.ticketmaster import AUSTIN_GEOHASH, TicketmasterCollector
from aem.collectors.ticketmaster import BASE_URL as TM_URL
from aem.config import Settings
from aem.main import build_collectors
from aem.metrics import TM_WINDOW_TRUNCATED
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


TM_OPTIONS = {"api_key": "test-key", "segments": ["Music"],
              "horizon_days": 30, "window_days": 30}


def _tm_payload(events=None, **page):
    payload = json.loads(fixture_text("ticketmaster_events.json"))
    if events is not None:
        payload["_embedded"]["events"] = events
    payload["page"].update(page)
    return payload


@respx.mock
async def test_ticketmaster_maps_discovery_events(ctx):
    route = respx.get(url__startswith=TM_URL).mock(
        return_value=Response(200, text=fixture_text("ticketmaster_events.json")))

    events = await TicketmasterCollector(TM_OPTIONS).fetch(ctx)

    assert route.call_count == 1
    params = route.calls[0].request.url.params
    # geo search, not dmaId: Discovery's Austin DMA also covers San Antonio
    assert params["geoPoint"] == AUSTIN_GEOHASH
    assert params["radius"] == "40" and params["unit"] == "miles"
    assert "dmaId" not in params
    assert params["segmentName"] == "Music"
    # the announcement with no venue is dropped, the other nine map through
    assert len(events) == 9
    by_key = {e.source_key: e for e in events}

    show = by_key["vv1"]
    assert show.title == "Khruangbin"
    assert show.kind == EventKind.concert
    assert show.venue_slug == "moody-amphitheater"
    assert show.venue_name == "Moody Amphitheater"
    assert show.starts_at == datetime(2026, 10, 16, 1, 0)
    assert show.ends_at == datetime(2026, 10, 16, 4, 0)
    assert show.ticket_status == TicketStatus.on_sale
    assert show.event_url == show.ticket_url == "https://www.ticketmaster.com/event/vv1"
    assert show.attrs["performers"] == ["Khruangbin", "Hermanos Gutierrez"]
    assert show.attrs["promoter"] == "C3 Presents"
    assert show.attrs["city"] == "Austin"
    assert show.attrs["price_min"] == 59.5

    assert by_key["vv2"].ticket_status == TicketStatus.sold_out
    assert by_key["vv3"].ticket_status == TicketStatus.presale
    assert by_key["vv4"].ticket_status == TicketStatus.coming_soon

    # a cancellation is schedule news, not sale news: status is left untouched
    # (None preserves the stored value) and the reason lands in attrs
    cancelled = by_key["vv5"]
    assert cancelled.ticket_status is None
    assert cancelled.attrs["status_note"] == "cancelled"

    assert by_key["vv6"].kind == EventKind.comedy
    assert by_key["vv7"].kind == EventKind.movie
    # rooms AEM already tracks keep the slug their own collector uses
    assert by_key["vv7"].venue_slug == "bullock-imax"
    assert by_key["vv10"].venue_slug == "moody-theater"

    nutcracker = by_key["vv8"]
    assert nutcracker.kind == EventKind.live_performance
    # local date/time without a UTC dateTime is converted through the venue tz
    assert nutcracker.starts_at == datetime(2026, 12, 13, 1, 30)


@respx.mock
async def test_ticketmaster_pages_and_flags_truncated_windows(ctx):
    all_events = json.loads(fixture_text("ticketmaster_events.json"))["_embedded"]["events"]

    def handler(request):
        page = int(request.url.params["page"])
        # more results than the deep-paging cap can ever return
        return Response(200, json=_tm_payload(
            all_events[page * 5:(page + 1) * 5],
            totalElements=1500, totalPages=8, number=page))

    route = respx.get(url__startswith=TM_URL).mock(side_effect=handler)
    before = TM_WINDOW_TRUNCATED.labels(segment="Music")._value.get()

    events = await TicketmasterCollector({**TM_OPTIONS, "max_pages": 3}).fetch(ctx)

    assert route.call_count == 3  # stops at max_pages, not at totalPages
    assert [c.request.url.params["page"] for c in route.calls] == ["0", "1", "2"]
    assert TM_WINDOW_TRUNCATED.labels(segment="Music")._value.get() == before + 1
    # pages 0-1 hold the ten fixture events (one venue-less), page 2 is empty
    assert len(events) == 9


@respx.mock
async def test_ticketmaster_applies_venue_and_genre_filters(ctx):
    respx.get(url__startswith=TM_URL).mock(
        return_value=Response(200, text=fixture_text("ticketmaster_events.json")))

    events = await TicketmasterCollector(
        {**TM_OPTIONS, "venue_denylist": ["moody-amphitheater"],
         "exclude_genres": ["comedy"]}).fetch(ctx)
    keys = {e.source_key for e in events}
    assert "vv1" not in keys and "vv6" not in keys and "vv2" in keys

    events = await TicketmasterCollector(
        {**TM_OPTIONS, "venue_allowlist": ["stubb-s-bar-b-q"]}).fetch(ctx)
    assert {e.source_key for e in events} == {"vv2"}

    # a city guard for anyone who swaps the geo search back to a DMA. Filtering
    # everything out is an empty batch, never a ParseDriftError — that alarm is
    # reserved for the API itself going quiet
    events = await TicketmasterCollector(
        {**TM_OPTIONS, "exclude_cities": ["austin"]}).fetch(ctx)
    assert events == []


@respx.mock
async def test_ticketmaster_empty_result_is_drift(ctx):
    respx.get(url__startswith=TM_URL).mock(
        return_value=Response(200, json=_tm_payload([], totalElements=0, totalPages=0)))

    with pytest.raises(ParseDriftError):
        await TicketmasterCollector(TM_OPTIONS).fetch(ctx)


@respx.mock
async def test_ticketmaster_error_never_leaks_the_api_key(ctx):
    respx.get(url__startswith=TM_URL).mock(return_value=Response(401, json={"fault": "denied"}))

    with pytest.raises(RuntimeError) as exc:
        await TicketmasterCollector(TM_OPTIONS).fetch(ctx)
    # the key rides in the query string, and poll errors are stored and shipped to ntfy
    assert "test-key" not in str(exc.value)
    assert "apikey=***" in str(exc.value)


def test_ticketmaster_is_skipped_without_an_api_key(cfg):
    without = build_collectors(cfg, Settings(ticketmaster_api_key=""))
    assert TicketmasterCollector.id not in {c.id for c in without}

    with_key = build_collectors(cfg, Settings(ticketmaster_api_key="k"))
    assert TicketmasterCollector.id in {c.id for c in with_key}
