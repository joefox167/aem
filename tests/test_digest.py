from unittest.mock import patch

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
                      content_hash="a", ticket_status="on_sale", attrs={"format": "IMAX"})
        show = Event(source="acl_live", source_key="g1", venue_id=hall.id,
                     kind="concert", title="John Mulaney", title_norm="john mulaney",
                     content_hash="b", ticket_status="coming_soon", attrs={})
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
