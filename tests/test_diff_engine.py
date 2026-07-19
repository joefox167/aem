"""Diff-engine behavior through real ingest polls with a fake collector."""

from datetime import datetime, timedelta

from sqlalchemy import select

from aem.collectors.base import Collector, RawEvent, VenueInfo
from aem.core.ingest import poll_collectors
from aem.models import ChangeLog, Event, TicketStatus, EventKind, utcnow


class FakeCollector(Collector):
    id = "fake"
    venues = [VenueInfo("fake-venue", "Fake Venue")]

    def __init__(self):
        self.batch: list[RawEvent] = []
        self.error: Exception | None = None

    async def fetch(self, ctx):
        if self.error:
            raise self.error
        return list(self.batch)


FIXED_START = datetime(2027, 1, 15, 20, 0)


def make_event(key="k1", title="The Thing", status=TicketStatus.coming_soon, **kw):
    defaults = dict(
        source_key=key, kind=EventKind.concert, title=title, venue_slug="fake-venue",
        starts_at=FIXED_START, ticket_status=status,
        ticket_url="https://tix.example/1",
    )
    defaults.update(kw)
    return RawEvent(**defaults)


def changes_of(session_factory, ctype=None):
    with session_factory() as s:
        stmt = select(ChangeLog)
        if ctype:
            stmt = stmt.where(ChangeLog.change_type == ctype)
        return s.scalars(stmt).all()


async def test_first_run_is_baseline_then_added(session_factory, cfg):
    col = FakeCollector()
    col.batch = [make_event("k1"), make_event("k2", title="Other Show")]
    await poll_collectors(session_factory, [col], cfg)

    assert len(changes_of(session_factory, "baseline")) == 2
    assert changes_of(session_factory, "added") == []

    col.batch.append(make_event("k3", title="Brand New"))
    await poll_collectors(session_factory, [col], cfg)
    added = changes_of(session_factory, "added")
    assert len(added) == 1


async def test_cosmetic_change_ignored_and_status_transition_detected(session_factory, cfg):
    col = FakeCollector()
    col.batch = [make_event("k1")]
    await poll_collectors(session_factory, [col], cfg)

    # cosmetic: non-meaningful attr changes only
    col.batch = [make_event("k1", attrs={"image": "new-poster.jpg"})]
    await poll_collectors(session_factory, [col], cfg)
    assert changes_of(session_factory, "updated") == []
    assert changes_of(session_factory, "ticket_status") == []

    # meaningful: coming_soon -> on_sale
    col.batch = [make_event("k1", status=TicketStatus.on_sale)]
    await poll_collectors(session_factory, [col], cfg)
    status_changes = changes_of(session_factory, "ticket_status")
    assert len(status_changes) == 1
    assert status_changes[0].field_changes["ticket_status"] == ["coming_soon", "on_sale"]


async def test_unknown_to_known_status_is_update_not_transition(session_factory, cfg):
    col = FakeCollector()
    col.batch = [make_event("k1", status=TicketStatus.unknown)]
    await poll_collectors(session_factory, [col], cfg)

    # backfilling a status we simply hadn't learned yet must not fire
    # a "tickets on sale" transition alert
    col.batch = [make_event("k1", status=TicketStatus.on_sale)]
    await poll_collectors(session_factory, [col], cfg)
    assert changes_of(session_factory, "ticket_status") == []
    updated = changes_of(session_factory, "updated")
    assert len(updated) == 1
    assert updated[0].field_changes["ticket_status"] == ["unknown", "on_sale"]


async def test_none_preserves_previous_ticket_state(session_factory, cfg):
    col = FakeCollector()
    col.batch = [make_event("k1", status=TicketStatus.on_sale)]
    await poll_collectors(session_factory, [col], cfg)

    # next poll skips the detail fetch: status/url are None -> preserved
    col.batch = [make_event("k1", status=None, ticket_url=None)]
    await poll_collectors(session_factory, [col], cfg)
    assert changes_of(session_factory, "ticket_status") == []
    assert changes_of(session_factory, "updated") == []
    with session_factory() as s:
        ev = s.scalar(select(Event))
        assert ev.ticket_status == "on_sale"
        assert ev.ticket_url == "https://tix.example/1"


async def test_removal_after_threshold_and_not_before(session_factory, cfg):
    col = FakeCollector()
    col.batch = [make_event("k1"), make_event("k2", title="Stays")]
    await poll_collectors(session_factory, [col], cfg)

    col.batch = [make_event("k2", title="Stays")]
    await poll_collectors(session_factory, [col], cfg)  # missing 1
    await poll_collectors(session_factory, [col], cfg)  # missing 2
    assert changes_of(session_factory, "removed") == []

    await poll_collectors(session_factory, [col], cfg)  # missing 3 -> removed
    removed = changes_of(session_factory, "removed")
    assert len(removed) == 1
    with session_factory() as s:
        gone = s.scalar(select(Event).where(Event.source_key == "k1"))
        assert gone.status == "removed"


async def test_failed_poll_never_increments_missing(session_factory, cfg):
    col = FakeCollector()
    col.batch = [make_event("k1")]
    await poll_collectors(session_factory, [col], cfg)

    col.error = RuntimeError("site down")
    for _ in range(5):
        await poll_collectors(session_factory, [col], cfg)
    assert changes_of(session_factory, "removed") == []
    with session_factory() as s:
        ev = s.scalar(select(Event))
        assert ev.missing_polls == 0
        assert ev.status == "active"


async def test_past_events_never_marked_removed(session_factory, cfg):
    col = FakeCollector()
    col.batch = [make_event("k1", starts_at=utcnow() - timedelta(days=2))]
    await poll_collectors(session_factory, [col], cfg)

    col.batch = []
    # empty batch raises nothing here (fake returns []) but ingest treats zero
    # events from a real 200 as drift; simulate with another event present
    col.batch = [make_event("k2", title="Other")]
    for _ in range(4):
        await poll_collectors(session_factory, [col], cfg)
    assert changes_of(session_factory, "removed") == []


async def test_error_isolation_between_collectors(session_factory, cfg):
    good, bad = FakeCollector(), FakeCollector()
    bad.__class__ = type("Bad", (FakeCollector,), {"id": "bad"})
    bad.error = RuntimeError("boom")
    good.batch = [make_event("k1")]

    results = await poll_collectors(session_factory, [bad, good], cfg)
    assert "error" in results["bad"]
    assert "error" not in results["fake"]
    with session_factory() as s:
        assert s.scalar(select(Event)) is not None
