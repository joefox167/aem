"""Local-time formatting shared by the dashboard and the digest email.

Lives here rather than in `web` because `notify` needs it too, and
`notify -> web -> api -> notify` would be circular.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def local_stamp(dt: datetime | None, tz_name: str, with_time: bool = True) -> str:
    """Format a naive-UTC datetime in local time.

    Sources that publish a date with no showtime -- an ACL RSS `startdate`, a
    Bullock film run, a Paramount performance date -- store UTC midnight. Such a
    value is a calendar date, not an instant: converting it would move it to the
    previous evening and invent a 7 PM start, so it is rendered as the date it is.
    """
    if dt is None:
        return "TBA"
    if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
        return dt.strftime("%a %b %d, %Y")
    local = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz_name))
    return local.strftime("%a %b %d, %Y" + (" %I:%M %p" if with_time else ""))


def parse_utc(value: object) -> datetime | None:
    """Parse the timestamps we store as strings -- naive ISO in
    ChangeLog.field_changes, `...Z`-suffixed ISO in Event.attrs (fromisoformat
    handles the Z on 3.11+) -- to naive UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return parsed
