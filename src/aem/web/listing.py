"""View-model for event lists: one display row per event, date sections, filters.

Kept free of FastAPI and templates so the grouping and filter rules are plain
functions the tests can drive with a fixed `today`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from ..fmt import category, local_day, local_time, next_sale, price_label, upcoming_sale
from ..models import Event, TicketStatus, Venue

NEW_FOR = timedelta(hours=48)
# how long after its start time a show still counts as on: long enough for an
# evening concert, short enough that a 1 PM matinee is gone by the evening
RUNS_FOR = timedelta(hours=3)

# chip key -> (label, event kinds it covers); "" is the unfiltered view
KIND_FILTERS: dict[str, tuple[str, frozenset[str] | None]] = {
    "": ("All", None),
    "concert": ("Concerts", frozenset({"concert"})),
    "comedy": ("Comedy", frozenset({"comedy"})),
    "live": ("Theater & Live", frozenset({"live_performance", "special_event"})),
    "movie": ("Movies", frozenset({"movie"})),
}

WHEN_FILTERS = {
    "": "Any time",
    "weekend": "This weekend",
    "7d": "Next 7 days",
    "30d": "Next 30 days",
}


@dataclass
class Row:
    event: Event
    venue: Venue | None
    day: date | None
    end_day: date | None
    time: str | None
    category: str
    price: str | None
    sale_at: datetime | None
    badges: list[tuple[str, str]] = field(default_factory=list)
    extra_times: list[str] = field(default_factory=list)

    @property
    def openers(self) -> list[str]:
        return list((self.event.attrs or {}).get("openers") or [])

    @property
    def times(self) -> str | None:
        """"7:30 PM", or "1:00 PM & 6:30 PM" when performances were merged."""
        times = [t for t in [self.time, *self.extra_times] if t]
        if len(times) <= 1:
            return times[0] if times else None
        return ", ".join(times[:-1]) + " & " + times[-1]


def _sale_label(prefix: str, when: datetime, tz: str) -> str:
    day = local_day(when, tz)
    time = local_time(when, tz)
    return f"{prefix} {day:%a %b} {day.day}" + (f", {time}" if time else "")


def build_row(event: Event, venue: Venue | None, tz: str, now: datetime) -> Row:
    day = local_day(event.starts_at, tz)
    end_day = local_day(event.ends_at, tz)
    sale = next_sale(event.attrs, now)
    sale_at = sale[0] if sale else None

    # only the states worth a glance: "on sale" is the norm and "unknown" says nothing
    badges: list[tuple[str, str]] = []
    if event.status == "removed":
        badges.append(("removed", "Removed"))
    if event.ticket_status == TicketStatus.sold_out.value:
        badges.append(("sold-out", "Sold out"))
    elif event.ticket_status == TicketStatus.presale.value:
        badges.append(("presale", "Presale on now"))
        public = upcoming_sale(event.attrs, now)
        if public is not None:
            badges.append(("soon", _sale_label("On sale", public, tz)))
    elif sale is not None:
        badges.append(("soon", _sale_label(sale[1], sale[0], tz)))
    elif event.ticket_status == TicketStatus.coming_soon.value:
        badges.append(("soon", "Coming soon"))
    if event.first_seen and now - event.first_seen < NEW_FOR:
        badges.append(("new", "New"))

    return Row(
        event=event, venue=venue, day=day,
        end_day=end_day if end_day and day and end_day > day else None,
        time=local_time(event.starts_at, tz), category=category(event),
        price=price_label(event.attrs), sale_at=sale_at, badges=badges,
    )


def is_current(row: Row, today: date, now: datetime | None = None) -> bool:
    """Not over yet: a dated event from today on, a run still playing, or no date.
    With `now`, a timed show is over RUNS_FOR after it starts; a date-only one
    lasts the whole day, since there's no time to judge by."""
    if row.day is None:
        return True
    if row.day >= today:
        return now is None or not row.time or row.event.starts_at + RUNS_FOR >= now
    return row.end_day is not None and row.end_day >= today


def _week_bounds(today: date) -> tuple[date, date]:
    """(Friday, Sunday) of the weekend at or after `today`."""
    sunday = today + timedelta(days=6 - today.weekday())
    return sunday - timedelta(days=2), sunday


def section_label(day: date | None, today: date) -> str:
    if day is None:
        return "Date TBA"
    if day < today:
        return "Now showing"
    if day == today:
        return "Today"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    friday, sunday = _week_bounds(today)
    if friday <= day <= sunday:
        return "This weekend"
    if day <= sunday:
        return "This week"
    if day <= sunday + timedelta(days=7):
        return "Next week"
    return f"{day:%B}" if day.year == today.year else f"{day:%B %Y}"


def sort_key(row: Row):
    # within a day, shows with a time first: a date-only listing is usually an
    # evening show whose time the source didn't publish, not something at midnight.
    # naive-UTC starts_at all share one ISO shape, so the string orders like the value
    return (row.day is None, row.day or date.max, row.time is None,
            str(row.event.starts_at or ""), row.event.title.lower())


def merge_performances(rows: list[Row]) -> list[Row]:
    """One row per show per day: a matinee and an evening performance of the same
    title at the same venue become one row listing both times. Keeps the
    earliest performance's link; "Sold out" only if every performance is."""
    out: list[Row] = []
    by_key: dict[tuple, Row] = {}
    for row in sorted(rows, key=sort_key):
        if row.day is None:
            out.append(row)
            continue
        key = (row.event.title.strip().lower(), row.event.venue_id, row.day)
        first = by_key.get(key)
        if first is None:
            # a copy: the same Row objects feed several dashboard sections
            first = replace(row, badges=list(row.badges), extra_times=list(row.extra_times))
            by_key[key] = first
            out.append(first)
            continue
        # the same show listed twice (a duplicate ticketing entry) adds nothing
        if row.time and row.time != first.time and row.time not in first.extra_times:
            first.extra_times.append(row.time)
        if ("sold-out", "Sold out") not in row.badges:
            first.badges = [b for b in first.badges if b[0] != "sold-out"]
    return out


def sections(rows: list[Row], today: date) -> list[tuple[str, list[Row]]]:
    """Consecutive date sections in chronological order; runs already playing
    first, undated events last. Labels are monotonic in date, so grouping the
    sorted rows by label never splits a section."""
    out: list[tuple[str, list[Row]]] = []
    for row in merge_performances(rows):
        label = section_label(row.day, today)
        if out and out[-1][0] == label:
            out[-1][1].append(row)
        else:
            out.append((label, [row]))
    return out


def weekend_range(today: date) -> tuple[date, date]:
    """The part of the Fri-Sun weekend still worth planning: on a Saturday that's
    Saturday and Sunday; Monday to Thursday it's the coming weekend. On Sunday
    it's already next weekend -- what's left of today is in "Today"."""
    if today.weekday() == 6:
        return today + timedelta(days=5), today + timedelta(days=7)
    friday, sunday = _week_bounds(today)
    return max(friday, today), sunday


def weekend_title(today: date) -> str:
    return "Next weekend" if today.weekday() == 6 else "This weekend"


def weekend_sections(rows: list[Row], today: date) -> tuple[list[tuple[str, list[Row]]], int]:
    """The weekend's events grouped by day, plus how many runs (films playing
    for weeks or months) span it. Runs are counted, not listed: a museum's
    catalogue of IMAX films would otherwise top every weekend."""
    start, _ = weekend_range(today)
    in_window = apply_filters(rows, today, when="weekend")
    runs = sum(1 for r in in_window if r.day < start)
    out: list[tuple[str, list[Row]]] = []
    for row in merge_performances([r for r in in_window if r.day >= start]):
        label = f"{row.day:%A}" + (" (today)" if row.day == today else "")
        if out and out[-1][0] == label:
            out[-1][1].append(row)
        else:
            out.append((label, [row]))
    return out, runs


def _in_window(row: Row, when: str, today: date) -> bool:
    if not when:
        return True
    if row.day is None:
        return False
    if when == "weekend":
        start, end = weekend_range(today)
    else:
        start, end = today, today + timedelta(days=6 if when == "7d" else 29)
    # a run overlaps the window if it starts before the end and ends after the start
    return row.day <= end and (row.end_day or row.day) >= start


def apply_filters(rows: list[Row], today: date, *, kind: str = "", genre: str = "",
                  venue: int | None = None, when: str = "") -> list[Row]:
    kinds = KIND_FILTERS.get(kind, KIND_FILTERS[""])[1]
    return [
        r for r in rows
        if (kinds is None or r.event.kind in kinds)
        and (not genre or r.category == genre)
        and (venue is None or r.event.venue_id == venue)
        and _in_window(r, when, today)
    ]


def facet_counts(rows: list[Row]) -> tuple[list[tuple[str, int]], list[tuple[int, str, int]]]:
    """Genre and venue options for the filter dropdowns, most common first."""
    genres: dict[str, int] = {}
    venues: dict[int, tuple[str, int]] = {}
    for r in rows:
        genres[r.category] = genres.get(r.category, 0) + 1
        name = r.venue.name if r.venue else "?"
        count = venues.get(r.event.venue_id, (name, 0))[1]
        venues[r.event.venue_id] = (name, count + 1)
    genre_opts = sorted(genres.items(), key=lambda kv: (-kv[1], kv[0]))
    venue_opts = sorted(((vid, n, c) for vid, (n, c) in venues.items()),
                        key=lambda v: (-v[2], v[1]))
    return genre_opts, venue_opts
