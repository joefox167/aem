from unittest.mock import Mock, patch

from sqlalchemy import select

from aem.config import AppConfig, Settings
from aem.models import ChangeLog, Event, Venue
from aem.notify import digest
from aem.notify import email as email_sender


def _seed(session_factory):
    with session_factory() as s:
        theater = Venue(source="bullock_imax", name="Bullock Museum IMAX", slug="bullock-imax")
        hall = Venue(source="acl_live", name="ACL Live at The Moody Theater", slug="moody-theater")
        s.add_all([theater, hall])
        s.flush()
        movie = Event(source="bullock_imax", source_key="cat:1329", venue_id=theater.id,
                      kind="movie", title="The Odyssey", title_norm="odyssey",
                      content_hash="a", ticket_status="on_sale", attrs={"format": "IMAX"},
                      event_url="https://www.thestoryoftexas.com/imax/the-odyssey")
        show = Event(source="acl_live", source_key="g1", venue_id=hall.id,
                     kind="comedy", title="John Mulaney", title_norm="john mulaney",
                     content_hash="b", ticket_status="coming_soon",
                     attrs={"genre": "Comedy"},
                     event_url="https://acl-live.com/events/john-mulaney",
                     ticket_url="https://www.ticketmaster.com/event/john-mulaney")
        s.add_all([movie, show])
        s.flush()
        s.add_all([
            ChangeLog(event_id=movie.id, change_type="added", field_changes={}),
            ChangeLog(event_id=show.id, change_type="added", field_changes={}),
            ChangeLog(event_id=show.id, change_type="ticket_status",
                      field_changes={"ticket_status": ["coming_soon", "on_sale"]}),
            ChangeLog(event_id=movie.id, change_type="baseline", field_changes={}),
        ])
        s.commit()


def test_build_and_render_digest(session_factory):
    _seed(session_factory)
    cfg = AppConfig()
    settings = Settings(base_url="https://aem.home.arpa")
    with session_factory() as s:
        data = digest.build_digest(s, cfg)
    assert data["total"] == 3  # baseline excluded
    assert "Bullock Museum IMAX" in data["movies_added"]
    assert "ACL Live at The Moody Theater" in data["concerts_added"]
    assert len(data["ticket_changes"]) == 1

    html = digest.render_digest(data, settings, "Testday")
    assert "The Odyssey" in html
    assert "(IMAX)" in html
    assert "John Mulaney" in html
    assert "On Sale" in html or "on sale" in html.lower()
    # titles link to the public listing, never to the LAN-only dashboard host
    assert "home.arpa" not in html
    assert "https://www.thestoryoftexas.com/imax/the-odyssey" in html
    assert "https://acl-live.com/events/john-mulaney" in html
    # a distinct ticket_url still earns its own secondary link
    assert "https://www.ticketmaster.com/event/john-mulaney" in html
    # category badges: source genre wins, event kind is the fallback
    assert ">Comedy<" in html
    assert ">Movie<" in html


def test_event_without_public_url_renders_unlinked(session_factory):
    with session_factory() as s:
        venue = Venue(source="paramount", name="Paramount Theatre", slug="paramount")
        s.add(venue)
        s.flush()
        s.add(Event(source="paramount", source_key="p1", venue_id=venue.id,
                    kind="live_performance", title="Mystery Show", title_norm="mystery show",
                    content_hash="c", attrs={}))
        s.flush()
        event_id = s.scalars(select(Event).where(Event.title == "Mystery Show")).one().id
        s.add(ChangeLog(event_id=event_id, change_type="added", field_changes={}))
        s.commit()

    cfg = AppConfig()
    with session_factory() as s:
        data = digest.build_digest(s, cfg)
    html = digest.render_digest(data, Settings(), "Testday")
    assert "Mystery Show" in html
    assert "<a href" not in html  # no listing URL, so no broken link
    assert ">Live<" in html


def test_send_digest_stamps_and_dedupes(session_factory):
    _seed(session_factory)
    cfg = AppConfig()
    settings = Settings(gmail_user="u@example.com", gmail_app_password="pw",
                        digest_to="me@example.com")
    with patch("aem.notify.email.send_html", return_value=True) as mock_send:
        with session_factory() as s:
            first = digest.send_digest(s, settings, cfg)
        with session_factory() as s:
            second = digest.send_digest(s, settings, cfg)
    assert first == {"sent": True, "changes": 3}
    assert second["sent"] is False
    assert mock_send.call_count == 1
    with session_factory() as s:
        undigested = s.scalars(
            select(ChangeLog).where(ChangeLog.digested_at.is_(None),
                                    ChangeLog.change_type != "baseline")
        ).all()
        assert undigested == []


def test_email_parses_multiple_recipients():
    assert email_sender._parse_recipients("a@example.com, b@example.com") == [
        "a@example.com", "b@example.com"
    ]
    assert email_sender._parse_recipients("Alice <a@example.com>; Bob <b@example.com>") == [
        "a@example.com", "b@example.com"
    ]


def test_send_digest_records_sent_metrics(session_factory):
    _seed(session_factory)
    cfg = AppConfig()
    settings = Settings(gmail_user="u@example.com", gmail_app_password="pw",
                        digest_to="me@example.com")
    with (
        patch("aem.notify.email.send_html", return_value=True),
        patch("aem.notify.digest.metrics.DIGEST_RUNS") as mock_runs,
        patch("aem.notify.digest.metrics.DIGEST_LAST_SENT") as mock_last_sent,
    ):
        sent_metric = Mock()
        mock_runs.labels.return_value = sent_metric
        with session_factory() as s:
            result = digest.send_digest(s, settings, cfg, force=True)
    assert result == {"sent": True, "changes": 3}
    mock_runs.labels.assert_called_once_with(status="sent")
    sent_metric.inc.assert_called_once()
    mock_last_sent.set_to_current_time.assert_called_once()


def test_send_digest_records_failure_metrics(session_factory):
    _seed(session_factory)
    cfg = AppConfig()
    settings = Settings(gmail_user="u@example.com", gmail_app_password="pw",
                        digest_to="me@example.com")
    with (
        patch("aem.notify.email.send_html", return_value=False),
        patch("aem.notify.digest.metrics.DIGEST_RUNS") as mock_runs,
    ):
        failure_metric = Mock()
        mock_runs.labels.return_value = failure_metric
        with session_factory() as s:
            result = digest.send_digest(s, settings, cfg, force=True)
    assert result == {"sent": False, "reason": "smtp send failed"}
    mock_runs.labels.assert_called_once_with(status="failure")
    failure_metric.inc.assert_called_once()


def _seed_updates(session_factory):
    with session_factory() as s:
        venue = Venue(source="acl_live", name="ACL Live", slug="acl-live")
        s.add(venue)
        s.flush()
        moved = Event(source="acl_live", source_key="m1", venue_id=venue.id, kind="concert",
                      title="Khruangbin", title_norm="khruangbin", content_hash="x",
                      event_url="https://acl-live.com/khruangbin", attrs={})
        churn = Event(source="ticketmaster", source_key="t1", venue_id=venue.id, kind="concert",
                      title="Link Churn Only", title_norm="link churn only", content_hash="y",
                      event_url="https://tm.example/churn", attrs={})
        s.add_all([moved, churn])
        s.flush()
        s.add_all([
            ChangeLog(event_id=moved.id, change_type="updated", field_changes={
                "starts_at": ["2026-10-10T01:00:00", "2026-10-18T02:00:00"],
                "ticket_status": ["unknown", "on_sale"],
                "ticket_url": ["https://a", "https://b"],
            }),
            ChangeLog(event_id=churn.id, change_type="updated", field_changes={
                "ticket_url": ["https://a", "https://b"],
                "ends_at": ["2026-10-10T03:00:00", "2026-10-10T04:00:00"],
            }),
        ])
        s.commit()


def test_updated_rows_say_what_changed(session_factory):
    _seed_updates(session_factory)
    with session_factory() as s:
        data = digest.build_digest(s, AppConfig())

    assert len(data["updated"]) == 1          # the churn-only row is held back
    assert data["minor_updates"] == 1
    details = "; ".join(data["updated"][0]["details"])
    # naive-UTC stored values render in local time, both sides of the move
    assert "moved Fri Oct 09, 2026 08:00 PM → Sat Oct 17, 2026 09:00 PM" in details
    assert "tickets unknown → on sale" in details
    assert "ticket_url" not in details        # noise filtered out of a kept row

    html = digest.render_digest(data, Settings(), "Testday")
    assert "Khruangbin" in html
    assert "Link Churn Only" not in html
    assert "1 minor update not shown" in html


def test_suppressed_rows_are_still_stamped_digested(session_factory):
    _seed_updates(session_factory)
    settings = Settings(gmail_user="u@example.com", gmail_app_password="pw",
                        digest_to="me@example.com")
    with patch("aem.notify.email.send_html", return_value=True), session_factory() as s:
        assert digest.send_digest(s, settings, AppConfig())["sent"] is True
    with session_factory() as s:
        # both changes were digested, including the one kept out of the body
        assert s.scalars(select(ChangeLog).where(ChangeLog.digested_at.is_(None))).all() == []


def test_on_sale_shown_only_while_upcoming(session_factory):
    from datetime import timedelta

    from aem.models import utcnow
    soon = (utcnow() + timedelta(days=9)).isoformat()
    past = (utcnow() - timedelta(days=9)).isoformat()
    with session_factory() as s:
        venue = Venue(source="ticketmaster", name="Moody Center", slug="moody-center")
        s.add(venue)
        s.flush()
        upcoming = Event(source="ticketmaster", source_key="u1", venue_id=venue.id,
                         kind="concert", title="Future Presale", title_norm="future presale",
                         content_hash="a", attrs={"public_sale_start": soon})
        opened = Event(source="ticketmaster", source_key="u2", venue_id=venue.id,
                       kind="concert", title="Already Open", title_norm="already open",
                       content_hash="b", attrs={"public_sale_start": past})
        s.add_all([upcoming, opened])
        s.flush()
        s.add_all([
            ChangeLog(event_id=upcoming.id, change_type="added", field_changes={}),
            ChangeLog(event_id=opened.id, change_type="added", field_changes={}),
        ])
        s.commit()

    with session_factory() as s:
        data = digest.build_digest(s, AppConfig())
    by_title = {i["event"].title: i for v in data["concerts_added"].values() for i in v}
    assert by_title["Future Presale"]["on_sale"] is not None
    assert by_title["Already Open"]["on_sale"] is None
    assert "on sale" in digest.render_digest(data, Settings(), "Testday")


def test_text_alternative_is_rendered_and_sent_first(session_factory):
    _seed(session_factory)
    with session_factory() as s:
        data = digest.build_digest(s, AppConfig())
    text = digest.render_digest_text(data, "Testday")
    assert "The Odyssey" in text
    assert "John Mulaney" in text
    assert "<" not in text          # genuinely plain, not HTML with tags stripped
    assert "home.arpa" not in text

    with patch("aem.notify.email.smtplib.SMTP") as smtp:
        ok = email_sender.send_html("u@example.com", "pw", "me@example.com",
                                    "subj", "<p>hi</p>", text="hi there")
    assert ok
    raw = smtp.return_value.__enter__.return_value.sendmail.call_args[0][2]
    assert "text/plain" in raw and "text/html" in raw
    assert raw.index("text/plain") < raw.index("text/html")


def test_date_only_events_keep_their_date():
    """UTC midnight means 'this calendar date' for the RSS/scraped sources.
    Converting it would shift the day and fabricate an evening showtime."""
    from datetime import datetime

    from aem.fmt import local_stamp
    date_only = datetime(2026, 9, 13, 0, 0, 0)
    assert local_stamp(date_only, "America/Chicago") == "Sun Sep 13, 2026"

    # a real showtime still converts and still shows the time
    real_show = datetime(2026, 10, 10, 1, 0, 0)  # 01:00 UTC
    assert local_stamp(real_show, "America/Chicago") == "Fri Oct 09, 2026 08:00 PM"
