from typing import ClassVar

from aem.core.dedup import normalize_title


def test_normalize_strips_prefixes_and_articles():
    assert normalize_title("IMAX | The Odyssey") == "odyssey"
    assert normalize_title("IMAX DOC | T.REX 3D") == "t rex 3d"
    assert normalize_title("The Odyssey") == "odyssey"
    assert normalize_title("TEXAS SPIRIT THEATER | Shipwrecked") == "shipwrecked"
    assert normalize_title("Member IMAX Screening: Horse Power") == "horse power"


async def test_same_movie_across_sources_links_once(session_factory, cfg):
    from datetime import timedelta

    from sqlalchemy import select

    from aem.collectors.base import Collector, RawEvent, VenueInfo
    from aem.core.ingest import poll_collectors
    from aem.models import CanonicalEvent, Event, EventKind, utcnow

    class SourceA(Collector):
        id = "source_a"
        venues: ClassVar[list[VenueInfo]] = [VenueInfo("venue-a", "Venue A")]

        async def fetch(self, ctx):
            return [RawEvent(source_key="a1", kind=EventKind.movie,
                             title="IMAX | The Odyssey", venue_slug="venue-a",
                             starts_at=utcnow() + timedelta(days=10))]

    class SourceB(Collector):
        id = "source_b"
        venues: ClassVar[list[VenueInfo]] = [VenueInfo("venue-b", "Venue B")]

        async def fetch(self, ctx):
            return [RawEvent(source_key="b1", kind=EventKind.movie,
                             title="The Odyssey", venue_slug="venue-b",
                             starts_at=utcnow() + timedelta(days=12))]

    await poll_collectors(session_factory, [SourceA(), SourceB()], cfg)
    with session_factory() as s:
        events = s.scalars(select(Event)).all()
        canonicals = s.scalars(select(CanonicalEvent)).all()
        assert len(events) == 2
        assert len(canonicals) == 1
        assert events[0].canonical_id == events[1].canonical_id
