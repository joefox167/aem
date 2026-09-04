from unittest.mock import patch

from sqlalchemy import select

from aem.config import AppConfig, Settings
from aem.models import ChangeLog, Event, NotificationSent, Venue, utcnow
from aem.notify import dispatch
from aem.notify.rules import evaluate


def _seed(session_factory, change_type="added", field_changes=None, kind="concert"):
    with session_factory() as s:
        venue = Venue(source="fake", name="Fake Venue", slug="fake-venue")
        s.add(venue)
        s.flush()
        event = Event(
            source="fake", source_key="k1", venue_id=venue.id, kind=kind,
            title="Test Show", title_norm="test show", content_hash="x",
            ticket_status="on_sale", attrs={"format": "IMAX"},
        )
        s.add(event)
        s.flush()
        change = ChangeLog(event_id=event.id, change_type=change_type,
                           field_changes=field_changes or {}, detected_at=utcnow())
        s.add(change)
        s.commit()
        return event.id, change.id


def test_rules_defaults(session_factory):
    cfg = AppConfig()
    event_id, _ = _seed(session_factory)
    with session_factory() as s:
        event = s.get(Event, event_id)
        change = s.scalar(select(ChangeLog))
        action, always = evaluate(change, event, "fake-venue", cfg)
        assert action == "immediate" and not always


def test_onsale_rule_bypasses_quiet_hours(session_factory):
    cfg = AppConfig()
    event_id, _ = _seed(session_factory, change_type="ticket_status",
                        field_changes={"ticket_status": ["coming_soon", "on_sale"]})
    with session_factory() as s:
        event = s.get(Event, event_id)
        change = s.scalar(select(ChangeLog))
        action, always = evaluate(change, event, "fake-venue", cfg)
        assert action == "immediate" and always


def test_favorite_venue_promotes_to_immediate(session_factory):
    cfg = AppConfig(favorite_venues=["fake-venue"],
                    rules=[{"match": {"change_type": "updated"}, "action": "digest"}])
    event_id, _ = _seed(session_factory, change_type="updated",
                        field_changes={"starts_at": ["a", "b"]})
    with session_factory() as s:
        event = s.get(Event, event_id)
        change = s.scalar(select(ChangeLog))
        action, _ = evaluate(change, event, "fake-venue", cfg)
        assert action == "immediate"


def test_dispatch_sends_once_and_is_idempotent(session_factory):
    cfg = AppConfig()
    cfg.quiet_hours.start = "00:00"
    cfg.quiet_hours.end = "00:00"  # never quiet
    settings = Settings(ntfy_url="https://ntfy.example/topic")
    _seed(session_factory)

    with patch("aem.notify.ntfy.send", return_value=True) as mock_send:
        with session_factory() as s:
            sent1 = dispatch.process_pending(s, settings, cfg)
        with session_factory() as s:
            sent2 = dispatch.process_pending(s, settings, cfg)
    assert sent1 == 1
    assert sent2 == 0
    assert mock_send.call_count == 1
    with session_factory() as s:
        assert len(s.scalars(select(NotificationSent)).all()) == 1


def test_baseline_changes_never_notify(session_factory):
    cfg = AppConfig()
    settings = Settings(ntfy_url="https://ntfy.example/topic")
    _seed(session_factory, change_type="baseline")
    with patch("aem.notify.ntfy.send", return_value=True) as mock_send, session_factory() as s:
        sent = dispatch.process_pending(s, settings, cfg)
    assert sent == 0
    assert mock_send.call_count == 0


def test_quiet_hours_hold_normal_but_not_onsale(session_factory):
    cfg = AppConfig()
    cfg.quiet_hours.start = "00:00"
    cfg.quiet_hours.end = "23:59"  # always quiet
    settings = Settings(ntfy_url="https://ntfy.example/topic")
    _seed(session_factory)  # 'added' -> immediate but not 'always'
    _seed_onsale(session_factory)

    with patch("aem.notify.ntfy.send", return_value=True) as mock_send, session_factory() as s:
        sent = dispatch.process_pending(s, settings, cfg)
    assert sent == 1  # only the on_sale change went out
    assert mock_send.call_count == 1


def _seed_onsale(session_factory):
    with session_factory() as s:
        venue = s.scalar(select(Venue)) or Venue(source="fake", name="V", slug="v")
        if venue.id is None:
            s.add(venue)
            s.flush()
        event = Event(
            source="fake", source_key="k-onsale", venue_id=venue.id, kind="concert",
            title="Hot Show", title_norm="hot show", content_hash="y",
            ticket_status="on_sale", attrs={},
        )
        s.add(event)
        s.flush()
        s.add(ChangeLog(event_id=event.id, change_type="ticket_status",
                        field_changes={"ticket_status": ["presale", "on_sale"]},
                        detected_at=utcnow()))
        s.commit()


def test_platform_source_adds_are_digested_not_alerted(session_factory):
    """A DMA-wide source announces far too much to page on every add."""
    cfg = AppConfig()
    event_id, _ = _seed(session_factory)
    with session_factory() as s:
        event = s.get(Event, event_id)
        event.source = "ticketmaster"
        change = s.scalar(select(ChangeLog))
        assert evaluate(change, event, "moody-amphitheater", cfg) == ("digest", False)
        # ...unless it happens at a venue the user cares about
        cfg.favorite_venues = ["moody-amphitheater"]
        assert evaluate(change, event, "moody-amphitheater", cfg)[0] == "immediate"
