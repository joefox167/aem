# Writing a collector

A collector translates one source into `RawEvent`s. It does not diff, does not write to
the database, and does not decide what is worth notifying — ingest and the rules engine
own that. Getting a new source live is normally one file plus one fixture-backed test.

## The contract

```python
class MyVenueCollector(Collector):
    id = "my_venue"                                   # stable: it is stored on every row
    venues: ClassVar[list[VenueInfo]] = [             # ClassVar, not a bare list
        VenueInfo("my-venue", "My Venue"),
    ]

    async def fetch(self, ctx: FetchContext) -> list[RawEvent]:
        ...
```

`fetch` returns **everything the source currently lists**, every poll. Ingest infers
removals from absence, so returning a partial list marks the rest missing. If you cannot
enumerate the source in one poll, filter server-side (a date window) rather than
returning a slice — a stable window is not the same as an arbitrary truncation.

Raise `NotModified` when the source says nothing changed (a `304`), and
`ParseDriftError` when a `200` parses to zero events.

### `RawEvent` fields

| Field | Notes |
|---|---|
| `source_key` | Stable id within this source. Must survive title and date edits. |
| `kind` | `movie`, `concert`, `comedy`, `live_performance`, `special_event` |
| `title` | As published; normalization happens downstream |
| `venue_slug` | Must match a declared `VenueInfo`, or be paired with `venue_name` |
| `venue_name` | For platform sources that discover rooms at poll time |
| `starts_at` / `ends_at` | **Naive UTC.** Convert before returning. |
| `event_url` / `ticket_url` | `None` means "unknown this poll" — see below |
| `ticket_status` | Same `None` semantics |
| `attrs` | Anything else. Only `MEANINGFUL_ATTRS` keys affect change detection. |

### The `None` convention

`ticket_url` and `ticket_status` of `None` mean *"I did not learn this on this poll"*,
and the stored value is preserved. Use it whenever you skip a detail page — for a
budget cap, or because a fetch failed. Returning a default instead of `None` would look
like a real change and generate a false alert.

## Using `FetchContext`

`ctx.get(url, conditional=False)` handles the User-Agent, a 2s per-host throttle,
retries and timeouts. With `conditional=True` it stores ETag/Last-Modified per URL and
returns `None` on a `304`.

Never use `conditional=True` on a URL carrying a secret — the URL becomes a database
key. The Ticketmaster collector fetches unconditionally for exactly this reason.

`ctx.known_keys` holds every `source_key` already stored for your collector, and
`ctx.refresh_keys` those whose ticket status is still `unknown`. Together they let you
spend a limited detail-fetch budget on the events that actually need it:

```python
is_new = key not in ctx.known_keys
near_term = starts_at is not None and starts_at <= horizon
if budget > 0 and (is_new or near_term or key in ctx.refresh_keys):
    ...
```

## Registering and configuring

Add it to `ALL_COLLECTORS` in `src/aem/collectors/__init__.py`, then to
`config.example.yaml` and the deployed ConfigMap:

```yaml
collectors:
  my_venue:
    enabled: true
    options:
      horizon_days: 180
```

`options` reaches your `__init__` as `self.options`. If you override `__init__`, call
`super().__init__(options)`.

A collector needing a secret reads it from `Settings` in `main.build_collectors`, which
should skip the collector with a warning when the secret is absent rather than letting
it fail on every poll.

## Testing

Every collector ships with a fixture and a `respx`-mocked test — no network in the test
suite. Save a real response into `tests/fixtures/`, then assert on the mapping:

```python
@respx.mock
async def test_my_venue_parses(ctx):
    respx.get(INDEX_URL).mock(return_value=Response(200, text=fixture_text("my_venue.html")))
    events = await MyVenueCollector().fetch(ctx)
    assert events[0].starts_at == datetime(2026, 10, 16, 1, 0)
```

Cover the mapping, whatever status derivation you do, the filters, and the drift path.
Prefer asserting on a couple of specific events by key over asserting a total count,
which turns every fixture refresh into a test edit.

## Choosing an approach

The order of preference is a documented API, then a structured feed (RSS/JSON), then
HTML scraping. Prefer one collector per *platform* over one per venue: the Ticketmaster
Discovery collector covers a dozen rooms that would otherwise have been a dozen
scrapers, each with its own drift risk.

Before writing a venue-specific scraper, check whether the venue sells through a
platform already covered — and if it does, whether the dedicated scraper offers
something the platform does not. ACL Live keeps its own collector because the venue page
carries tour names, openers and AXS sale state that Discovery does not expose; those
rooms are denylisted from the Ticketmaster collector so they are not tracked twice.

## Checklist

- [ ] Stable `source_key` that survives edits
- [ ] Naive UTC datetimes
- [ ] `None` for anything not learned this poll
- [ ] `NotModified` / `ParseDriftError` raised where they apply
- [ ] Detail fetches bounded by a budget
- [ ] Secrets redacted from any error message that could reach a log or ntfy
- [ ] Fixture-backed tests, no network
- [ ] Registered in `ALL_COLLECTORS`, documented in `config.example.yaml`
- [ ] Added to the ConfigMap in the k8s repo
