#!/usr/bin/env python
"""Live smoke poll of the Ticketmaster collector — read-only, throwaway DB.

Shows what the Austin DMA actually returns before any of it reaches the
production database, so segments / window_days / venue_denylist can be tuned
from real data.

    AEM_TICKETMASTER_API_KEY=... .venv/bin/python scripts/tm_smoke.py
    .venv/bin/python scripts/tm_smoke.py --key-file ~/.config/aem/ticketmaster.key
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aem.collectors.base import FetchContext  # noqa: E402
from aem.collectors.ticketmaster import TicketmasterCollector  # noqa: E402
from aem.db import init_db, make_engine, make_session_factory  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-file", type=Path)
    ap.add_argument("--horizon-days", type=int, default=90)
    ap.add_argument("--window-days", type=int, default=60)
    ap.add_argument("--segments", default="Music,Arts & Theatre,Film")
    ap.add_argument("--top", type=int, default=40, help="venues to list")
    args = ap.parse_args()

    key = os.environ.get("AEM_TICKETMASTER_API_KEY", "")
    if args.key_file:
        key = args.key_file.expanduser().read_text().strip()
    if not key:
        print("no API key: set AEM_TICKETMASTER_API_KEY or pass --key-file", file=sys.stderr)
        return 2

    collector = TicketmasterCollector({
        "api_key": key,
        "segments": [s.strip() for s in args.segments.split(",") if s.strip()],
        "horizon_days": args.horizon_days,
        "window_days": args.window_days,
    })

    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(str(Path(tmp) / "smoke.db"))
        init_db(engine)
        with make_session_factory(engine)() as session:
            ctx = FetchContext(session)
            try:
                events = await collector.fetch(ctx)
            finally:
                await ctx.close()

    print(f"\n{len(events)} events over {args.horizon_days} days\n")

    for label, counter in (
        ("kind", collections.Counter(e.kind.value for e in events)),
        ("ticket_status", collections.Counter(
            e.ticket_status.value if e.ticket_status else "preserved" for e in events)),
        ("genre", collections.Counter(str(e.attrs.get("genre")) for e in events)),
    ):
        print(f"by {label}:")
        for name, count in counter.most_common(15):
            print(f"  {count:5d}  {name}")
        print()

    venues = collections.Counter((e.venue_slug, e.venue_name) for e in events)
    print(f"venues ({len(venues)} total, top {args.top}):")
    for (slug, name), count in venues.most_common(args.top):
        print(f"  {count:5d}  {slug:<40} {name}")

    print("\nsample:")
    for event in sorted(events, key=lambda e: (e.starts_at is None, e.starts_at))[:10]:
        status = event.ticket_status.value if event.ticket_status else "-"
        print(f"  {str(event.starts_at):<20} {status:<12} {event.venue_slug:<28} {event.title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
