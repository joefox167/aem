import json
import re
from datetime import datetime, timedelta

import pytest
import respx
from httpx import Response

from aem.collectors.acl_live import RSS_URL, AclLiveCollector
from aem.collectors.base import FetchContext, ParseDriftError
from aem.collectors.bullock_imax import FILMS_URL, STORE_URL, BullockImaxCollector
from aem.collectors.paramount import BASE_URL as PARAMOUNT_URL
from aem.collectors.paramount import ParamountCollector, _map_event
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


PARAMOUNT_NOW = datetime(2026, 9, 4, 12, 0)  # fixture has events either side of this


@pytest.fixture
def paramount_now(monkeypatch):
    """Freeze the collector's clock so past-event filtering is deterministic."""
    monkeypatch.setattr("aem.collectors.paramount.utcnow", lambda: PARAMOUNT_NOW)
    return PARAMOUNT_NOW


def _paramount_route(total_pages="1"):
    return respx.get(url__startswith=PARAMOUNT_URL).mock(
        return_value=Response(200, text=fixture_text("paramount_events.json"),
                              headers={"x-wp-totalpages": total_pages,
                                       "content-type": "application/json"}))


@respx.mock
async def test_paramount_maps_wordpress_events(ctx, paramount_now):
    route = _paramount_route()

    events = await ParamountCollector().fetch(ctx)

    assert route.call_count == 1
    assert route.calls[0].request.url.params["per_page"] == "100"
    by_key = {e.source_key: e for e in events}

    film = by_key["21612"]
    assert film.title == "Project Hail Mary"
    assert film.kind == EventKind.movie
    assert film.venue_slug == "paramount-theatre"
    # 2026-10-12 19:00 America/Chicago (CDT, UTC-5) -> 2026-10-13 00:00 UTC
    assert film.starts_at == datetime(2026, 10, 13, 0, 0)
    assert film.ends_at is None
    assert film.ticket_status == TicketStatus.on_sale
    assert film.ticket_url == "https://tickets.austintheatre.org/14166"
    # the WordPress event page is dead site-wide; the ticketing page is the event page
    assert film.event_url == "https://tickets.austintheatre.org/14166" == film.ticket_url

    # a two-night run spans first performance to last
    run = by_key["21474"]
    assert run.title == "Steel Magnolias"
    assert run.starts_at == datetime(2026, 9, 10, 0, 30)
    assert run.ends_at == datetime(2026, 9, 11, 0, 30)
    assert len(run.attrs["performances"]) == 2

    assert by_key["21338"].kind == EventKind.comedy
    assert by_key["21586"].kind == EventKind.live_performance
    # titles arrive HTML-escaped; dedupe against other sources depends on unescaping
    assert by_key["20521"].title.startswith("Tommy Castro & The Painkillers")


@respx.mock
async def test_paramount_drops_past_events(ctx, paramount_now):
    _paramount_route()

    events = await ParamountCollector().fetch(ctx)
    keys = {e.source_key for e in events}

    # the fixture holds six events whose last performance is already over
    assert "21193" not in keys   # Pee Wee's Big Adventure, 2026-08-12
    assert "20135" not in keys   # Jaws, 2026-06-16
    assert "21209" not in keys   # Speed Racer, 2026-08-31
    assert all(e.starts_at >= PARAMOUNT_NOW - timedelta(hours=30) for e in events)


@respx.mock
async def test_paramount_reads_status_format_and_venue_from_tags(ctx, paramount_now):
    _paramount_route()
    events = {e.source_key: e for e in await ParamountCollector().fetch(ctx)}

    # an off-site presentation is mapped onto the venue slug AEM already uses
    scream = events["21153"]
    assert scream.venue_slug == "bass-concert-hall"
    assert scream.venue_name == "Bass Concert Hall"

    # unrecognized tags are kept rather than dropped
    assert "Second Show Added" in events["21338"].attrs["notes"]


def test_paramount_tag_vocabulary_is_split_by_meaning():
    """Sale state, film format and marketing copy share one flat tag list."""
    fixture = json.loads(fixture_text("paramount_events.json"))
    by_id = {str(i["id"]): i for i in fixture}

    sold_out = _map_event(by_id["20135"], datetime(2026, 6, 1))   # Jaws, before it ran
    assert sold_out.ticket_status == TicketStatus.sold_out

    imax = _map_event(by_id["21209"], datetime(2026, 8, 1))       # Speed Racer
    assert imax.ticket_status == TicketStatus.on_sale
    assert imax.attrs["format"] == "IMAX"

    film_35mm = _map_event(by_id["19845"], datetime(2026, 6, 1))  # Creed, tagged 35mm only
    assert film_35mm.attrs["format"] == "35mm"
    assert film_35mm.ticket_status == TicketStatus.unknown

    untagged = _map_event(by_id["21193"], datetime(2026, 8, 1))   # Pee Wee, no tags
    assert untagged.ticket_status == TicketStatus.unknown


@respx.mock
async def test_paramount_follows_paging_headers(ctx, paramount_now):
    route = _paramount_route(total_pages="3")

    await ParamountCollector().fetch(ctx)

    assert route.call_count == 3
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2", "3"]


@respx.mock
async def test_paramount_caps_runaway_paging(ctx, paramount_now):
    route = _paramount_route(total_pages="500")

    await ParamountCollector({"max_pages": 2}).fetch(ctx)

    # a source-side change cannot turn one poll into hundreds of requests
    assert route.call_count == 2


@respx.mock
async def test_paramount_empty_api_is_drift(ctx, paramount_now):
    respx.get(url__startswith=PARAMOUNT_URL).mock(
        return_value=Response(200, json=[], headers={"x-wp-totalpages": "1"}))

    with pytest.raises(ParseDriftError):
        await ParamountCollector().fetch(ctx)



# --- Ticketmaster sale state ------------------------------------------------

def _tm_item(code="onsale", public_start=None, presales=None):
    sales = {"public": {"startDateTime": public_start}} if public_start else {}
    if presales:
        sales["presales"] = presales
    return {"dates": {"status": {"code": code}}, "sales": sales}


def test_tm_offsale_before_public_sale_is_coming_soon_not_sold_out():
    from aem.collectors.ticketmaster import _ticket_status
    from aem.models import TicketStatus
    now = datetime(2026, 9, 28, 12)
    item = _tm_item("offsale", "2026-10-02T15:00:00Z")
    assert _ticket_status(item, now) == (TicketStatus.coming_soon, None)
    # after the public sale opened, offsale does mean gone
    assert _ticket_status(_tm_item("offsale", "2026-09-01T15:00:00Z"), now) == \
        (TicketStatus.sold_out, None)
    # no real sale date: Ticketmaster isn't selling it, which isn't "sold out"
    assert _ticket_status(_tm_item("offsale", "9999-12-31T06:00:00Z"), now) == \
        (TicketStatus.unknown, None)
    assert _ticket_status(_tm_item("offsale"), now) == (TicketStatus.unknown, None)


def test_tm_presale_window_and_vip_packages():
    from aem.collectors.ticketmaster import _next_presale, _ticket_status
    from aem.models import TicketStatus
    now = datetime(2026, 9, 28, 12)
    vip = {"name": "VIP Packages Onsale", "startDateTime": "2010-01-01T15:00:00Z",
           "endDateTime": "2027-01-01T15:00:00Z"}
    wave1 = {"name": "Artist Presale Wave 1", "startDateTime": "2026-09-30T15:00:00Z",
             "endDateTime": "2026-10-01T04:00:00Z"}
    venue = {"name": "Venue Presale", "startDateTime": "2026-10-01T15:00:00Z",
             "endDateTime": "2026-10-02T04:00:00Z"}
    item = _tm_item("offsale", "2026-10-02T15:00:00Z", [vip, venue, wave1])
    # an open VIP window is not a ticket presale
    assert _ticket_status(item, now) == (TicketStatus.coming_soon, None)
    assert _next_presale(item["sales"], now) == "2026-09-30T15:00:00Z"
    during = datetime(2026, 9, 30, 16)
    assert _ticket_status(item, during) == (TicketStatus.presale, None)
    assert _next_presale(item["sales"], during) == "2026-10-01T15:00:00Z"


# --- pictures ---------------------------------------------------------------

def test_tm_picks_a_real_16_9_image_near_640_wide():
    from aem.collectors.ticketmaster import _pick_image
    item = {"images": [
        {"url": "stock.jpg", "ratio": "16_9", "width": 640, "fallback": True},
        {"url": "tiny.jpg", "ratio": "16_9", "width": 100},
        {"url": "good.jpg", "ratio": "16_9", "width": 640},
        {"url": "huge.jpg", "ratio": "16_9", "width": 2048},
        {"url": "square.jpg", "ratio": "4_3", "width": 640},
    ]}
    assert _pick_image(item) == "good.jpg"
    # only placeholders -> no picture, rather than a misleading stock photo
    assert _pick_image({"images": [{"url": "stock.jpg", "fallback": True}]}) is None
    assert _pick_image({}) is None


def test_bullock_poster_from_listing_card():
    from aem.collectors.bullock_imax import _parse_films_page, _poster
    films = _parse_films_page(fixture_text("bullock_films.html"))
    real = [f for f in films if "/films/" in (f["url"] or "")]  # skip the newsletter card
    assert real and all(f["image"] and f["image"].startswith("https://") for f in real)

    from selectolax.parser import HTMLParser
    img = HTMLParser('<img class="Listing-thumbnail-image" src="s.jpg" '
                     'srcset="a.jpg 300w, b.jpg 600w, c.jpg 1200w">').css_first("img")
    assert _poster(img) == "b.jpg"
    assert _poster(HTMLParser('<img src="only.jpg">').css_first("img")) == "only.jpg"
    assert _poster(None) is None



def test_acl_detail_page_picture_and_checked_marker():
    from aem.collectors.acl_live import _parse_detail
    detail = _parse_detail(fixture_text("acl_event.html"))
    assert detail["image_url"] == \
        "https://images.discovery-prod.axs.com/2026/03/uploadedimage_69b2e0878c553.jpg"
    # a page without og:image is recorded as checked ("") so it isn't refetched
    assert _parse_detail("<html><body></body></html>")["image_url"] == ""


def test_paramount_picture_from_the_feed():
    from aem.collectors.paramount import _map_event as map_paramount
    items = json.loads(fixture_text("paramount_events.json"))
    now = datetime(2020, 1, 1)
    it = next(i for i in items if map_paramount(i, now) is not None)
    it = {**it, "acf": {**(it.get("acf") or {}), "event_image": {"url": "https://s3.example/p.jpg"}}}
    assert map_paramount(it, now).attrs["image_url"] == "https://s3.example/p.jpg"
    it["acf"]["event_image"] = {"url": ""}
    it["acf"]["event_poster_image"] = {"url": "https://s3.example/poster.jpg"}
    assert map_paramount(it, now).attrs["image_url"] == "https://s3.example/poster.jpg"
