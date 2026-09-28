"""Local-time formatting shared by the dashboard and the digest email.

Lives here rather than in `web` because `notify` needs it too, and
`notify -> web -> api -> notify` would be circular.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


def local_stamp(dt: datetime | None, tz_name: str, with_time: bool = True,
                this_year: int | None = None) -> str:
    """Format a naive-UTC datetime in local time: "Fri Oct 2, 2026, 6:00 PM".

    With `this_year`, a date in that year drops the year ("Fri Oct 2, 6:00 PM"):
    in an email about this season, repeating 2026 on every line is noise.

    Sources that publish a date with no showtime -- an ACL RSS `startdate`, a
    Bullock film run, a Paramount performance date -- store UTC midnight. Such a
    value is a calendar date, not an instant: converting it would move it to the
    previous evening and invent a 7 PM start, so it is rendered as the date it is.
    """
    if dt is None:
        return "TBA"
    time = None
    if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
        day = dt.date()
    else:
        local = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz_name))
        day = local.date()
        if with_time:
            time = local.strftime("%I:%M %p").lstrip("0")
    out = f"{day:%a %b} {day.day}"
    if day.year != this_year:
        out += f", {day.year}"
    return out + (f", {time}" if time else "")


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


def _is_date_only(dt: datetime) -> bool:
    # see local_stamp: UTC midnight is how sources store "a date, no showtime"
    return (dt.hour, dt.minute, dt.second) == (0, 0, 0)


def local_day(dt: datetime | None, tz_name: str) -> date | None:
    """The calendar day an event falls on for someone in `tz_name`."""
    if dt is None:
        return None
    if _is_date_only(dt):
        return dt.date()
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz_name)).date()


def local_time(dt: datetime | None, tz_name: str) -> str | None:
    """"8:00 PM" in local time, or None when the source gave only a date."""
    if dt is None or _is_date_only(dt):
        return None
    local = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz_name))
    return local.strftime("%I:%M %p").lstrip("0")


KIND_LABELS = {
    "movie": "Film",
    "concert": "Music",
    "comedy": "Comedy",
    "live_performance": "Theater",
    "special_event": "Special",
}

# One vocabulary across sources. Ticketmaster alone produced "Theatre",
# "Arts & Theatre" and "Performance Art" for the same kind of show, and
# "Concert"/"Music" both meant "genre unknown".
GENRE_LABELS = {
    "rock": "Rock & Metal", "alternative": "Rock & Metal", "metal": "Rock & Metal",
    "punk": "Rock & Metal", "hard rock": "Rock & Metal",
    "pop": "Pop",
    "country": "Country & Folk", "folk": "Country & Folk", "bluegrass": "Country & Folk",
    "hip-hop/rap": "Hip-Hop & R&B", "r&b": "Hip-Hop & R&B", "soul": "Hip-Hop & R&B",
    "dance/electronic": "Electronic", "electronic": "Electronic",
    "jazz": "Jazz & Blues", "blues": "Jazz & Blues",
    "latin": "Latin & World", "world": "Latin & World", "reggae": "Latin & World",
    "classical": "Classical & Dance", "dance": "Classical & Dance", "ballet": "Classical & Dance",
    "opera": "Classical & Dance",
    "theatre": "Theater", "theater": "Theater", "arts & theatre": "Theater",
    "performance art": "Theater", "musical": "Theater", "broadway": "Theater",
    "comedy": "Comedy",
    "movie": "Film", "film": "Film", "animation": "Film",
    "family": "Family", "children's theatre": "Family",
}


def category(event) -> str:
    """At-a-glance label: the source's genre mapped onto GENRE_LABELS, else the
    event kind. Genres we have no mapping for ("Religious", "Music", ...) fall
    back to the kind rather than adding one-off labels to the filter."""
    attrs = event.attrs or {}
    for key in ("genre", "subgenre", "segment"):
        value = str(attrs.get(key) or "").strip().lower()
        if value in GENRE_LABELS:
            return GENRE_LABELS[value]
    return KIND_LABELS.get(event.kind, event.kind.replace("_", " ").title())


# Ticketmaster fills public_sale_start with 9999-12-31 when there is no sale to
# announce (seen on off-sale / sold-out events); it is not a date
_SALE_PLACEHOLDER_YEAR = 9000


def upcoming_sale(attrs: dict | None, now: datetime) -> datetime | None:
    """The public on-sale time if it is real and still ahead of `now` (naive UTC)."""
    when = parse_utc((attrs or {}).get("public_sale_start"))
    if when is None or when.year >= _SALE_PLACEHOLDER_YEAR or when <= now:
        return None
    return when


def price_label(attrs: dict | None) -> str | None:
    """"$28", "$28–$105", or None. A 0/0 range is a missing price, not a free show."""
    attrs = attrs or {}
    lo, hi = attrs.get("price_min"), attrs.get("price_max")
    if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)) or hi <= 0:
        return None
    lo_s, hi_s = f"${round(lo)}", f"${round(hi)}"
    return lo_s if lo_s == hi_s else f"{lo_s}–{hi_s}"


def local_today(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()


def next_sale(attrs: dict | None, now: datetime) -> tuple[datetime, str] | None:
    """The earliest real upcoming sale as (when, "Presale" | "On sale")."""
    options = []
    presale = parse_utc((attrs or {}).get("presale_start"))
    if presale is not None and presale > now and presale.year < _SALE_PLACEHOLDER_YEAR:
        options.append((presale, "Presale"))
    public = upcoming_sale(attrs, now)
    if public is not None:
        options.append((public, "On sale"))
    return min(options) if options else None
