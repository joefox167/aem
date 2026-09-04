# Architecture

AEM is a single FastAPI process with an in-process scheduler and a SQLite database.
There is no queue, no worker pool and no external state — a poll is a function call,
and everything it learns lands in one file on a PVC.

```
                  hourly (:07 + jitter)
                          |
   collectors  ──►  ingest.poll_collectors  ──►  events + change_log
   (per source)          |                              |
        |                | per collector, isolated       ├──► dispatch  → ntfy (immediate)
        |                |                               └──► digest    → email (daily 08:00)
        └── FetchContext (UA, throttle, conditional GET, retries)
```

## The pipeline

1. **Collect.** Each collector returns a flat list of `RawEvent` describing *everything
   the source currently lists*. Collectors never diff, never write, and never decide
   what is newsworthy — they translate a source into the common shape.
2. **Ingest.** `core.ingest` upserts the batch, compares it to what is stored, and
   writes `change_log` rows classifying what moved.
3. **Notify.** `notify.dispatch` sends immediate alerts for changes the rules mark
   `immediate`; `notify.digest` sweeps up everything else once a day.

Each collector runs inside its own try/except and its own `PollRun` row. A source that
breaks, hangs or changes its markup fails alone — the others still poll, and a failed
poll never advances the removal counter, so an outage cannot fake mass removals.

## Data model

`src/aem/models.py`. All datetimes are **naive UTC** — SQLite has no timezone type, so
the convention is enforced by `utcnow()` and by every collector converting on the way in.
Rendering converts to `cfg.timezone` at the edge.

| Table | Holds |
|---|---|
| `venues` | One row per room, keyed by a stable `slug` |
| `events` | One row per *(source, source_key)*: the same show from two sources is two rows |
| `canonical_events` | Groups those duplicate rows into one logical show |
| `change_log` | Every detected change, with `field_changes` and notification stamps |
| `showtimes` | Per-showtime data, for sources that expose it (not yet populated) |
| `notifications_sent` | Dedupe keys guaranteeing at-most-once delivery |
| `poll_runs` | Per-collector poll history: status, event count, error text |
| `http_cache` | ETag / Last-Modified per URL, for conditional GETs |

`source_key` is whatever the source uses as a stable identifier (a TM event id, an RSS
guid, a store category id). It must survive a title edit or a date change — that is what
makes "updated" distinguishable from "removed, then added".

## Change classification

`core.diff` decides what counts as a change, and this is where most of the product
judgment lives.

**Only meaningful fields are hashed.** `content_hash` covers the title, start/end,
ticket status, ticket URL and the `MEANINGFUL_ATTRS` allowlist. A source rewriting a
blurb, swapping a hero image or reordering markup cannot produce a change row, because
those fields never enter the hash.

**`None` means "unknown this poll", not "empty".** A collector that skips a detail page
(budget exhausted, fetch failed) sets `ticket_url`/`ticket_status` to `None`, and
`diff.resolve` keeps the stored value. Without this, every skipped detail fetch would
look like tickets being withdrawn.

**Change types:**

| Type | Meaning |
|---|---|
| `baseline` | Written on a collector's first successful poll. Stored for history, never notified — enabling a new source does not page you about its entire back catalogue. |
| `added` | A new `source_key`, or one reappearing after removal |
| `updated` | A meaningful field moved |
| `ticket_status` | A real sale-state transition |
| `removed` | Absent from `removal_threshold` consecutive polls |

**`unknown → X` is not a transition.** Backfilling a status you never knew is classified
`updated`, not `ticket_status`, so filling in blanks cannot fire "tickets on sale!".

**Removal needs repetition.** An event absent from one poll only increments
`missing_polls`; it is marked removed at `removal_threshold` (default 3). Events whose
date has already passed are skipped entirely — falling off a calendar after the show is
not a removal.

## Deduplication

`core.dedup` links events to a `canonical_event` by normalized title: strip known
prefixes (`IMAX |`, `TEXAS SPIRIT THEATER |`), punctuation and leading articles, then
exact match, then fuzzy match (`token_set_ratio >= 92`) guarded by a ±14-day date
window. The date guard stops an annual series from collapsing into one row.

This is what lets the same film at two theaters, or a show listed by both a venue and a
ticketing platform, render as one card with venue chips.

## Scheduling

`build_scheduler` (APScheduler, timezone from config):

| Job | When | Notes |
|---|---|---|
| `poll` | hourly at `poll_minute` | ±120s jitter, `max_instances=1`, 600s misfire grace |
| `digest` | daily at `digest_hour:00` | 3600s misfire grace |
| `flush` | every 10 minutes | Sends alerts held back by quiet hours |
| `backup` | daily 03:00 | `VACUUM INTO`, keeps 14 |

A `poll_lock` serializes polls, so a manual `/api/admin/poll` during a scheduled run
waits rather than racing it.

## HTTP behavior

`FetchContext` gives every collector the same manners: a descriptive User-Agent, a 2s
per-host throttle, two transport retries, a 20s timeout, and optional conditional GET
against the `http_cache` table. A source answering `304` raises `NotModified`, which
refreshes `last_seen` on every active event and resets `missing_polls` to zero —
"nothing changed" is positive evidence that everything it listed is still there.

A `200` that parses to zero events raises `ParseDriftError` — that is site drift, not an
empty calendar, and it increments `aem_parse_drift_total` instead of silently removing
every event the source has.
