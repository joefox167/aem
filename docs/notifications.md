# Notifications

Two channels, deliberately different in character:

- **ntfy, immediately** — for things you would want to act on within the hour, chiefly
  tickets going on sale.
- **email, once a day at 08:00** — everything else, batched.

Email is the primary channel; ntfy is optional and stays quiet if `AEM_NTFY_URL` is unset.

## Rules

`config.yaml` holds an ordered list; **the first match wins**. Fields inside one `match`
are AND-ed, list values are OR-ed, and an omitted field matches anything.

```yaml
rules:
  - match: {change_type: ticket_status, ticket_status_to: [on_sale, presale]}
    action: immediate
    always: true                      # bypasses quiet hours
  - match: {change_type: added, source: ticketmaster}
    action: digest
  - match: {change_type: added}
    action: immediate
  - match: {change_type: [updated, removed, ticket_status]}
    action: digest
```

Matchable fields: `change_type`, `source`, `kind`, `format`, `ticket_status_to`, `venue`.
Actions: `immediate`, `digest`, `ignore`. Default when nothing matches: `digest`.

Two behaviors worth knowing:

- **`favorite_venues` overrides the matched rule.** Any non-ignored change at a favorite
  venue is promoted to `immediate`, whatever the rules said. It is the simplest dial:
  list the rooms you actually care about.
- **`source` exists for platform collectors.** A metro-wide source announces far more
  per hour than a single venue, so its adds are digested while single-venue adds stay
  immediate. Without that distinction, enabling Ticketmaster would have made every add
  a push notification.

## Quiet hours

Alerts arising inside `quiet_hours` are **held, not dropped** — the change row stays
pending and the flush job (every 10 minutes) sends it once the window ends. A rule with
`always: true` ignores quiet hours entirely; that is reserved for on-sale and presale
transitions, where being four hours late is the same as not being told.

## Delivery guarantees

- A change is pending while `notified_immediate_at` and `digested_at` are both NULL, so
  a restart mid-poll never loses an alert.
- `notifications_sent` holds a dedupe key per change per channel (`change:<id>:ntfy`),
  making delivery at-most-once even if a poll is retried.
- Only changes from the last 48 hours are eligible for immediate alerting; anything
  older is left to the digest rather than arriving as stale news after an outage.
- Collector failures produce **one ops warning per collector per day**, to
  `AEM_NTFY_OPS_URL` if set. A source broken for a week is not a week of pages.

## The digest

Sent daily at `digest_hour`, and idempotent per day: a second attempt returns
`already sent today` unless forced. It sweeps every change not yet digested — including
ones that were never eligible for an immediate alert — groups them into new movies, new
concerts, ticket changes, updates and removals, and stamps `digested_at` only after SMTP
accepts the message. A failed send leaves everything pending for the next attempt.

`baseline` changes are excluded everywhere. Enabling a new collector writes its whole
catalogue as baseline, so the first digest after adding a source stays about what
actually changed.

Preview without sending:

```bash
curl -s localhost:8000/api/digest/preview
```

Force a send (bypasses both the daily dedupe and the empty check):

```bash
curl -X POST 'localhost:8000/api/admin/send-digest?force=true'
```

## Gmail

`AEM_GMAIL_USER` plus an **app password** (not the account password), sent over
STARTTLS on port 587. Google displays app passwords as `xxxx xxxx xxxx xxxx`; the spaced
form works as-is. `AEM_DIGEST_TO` takes one address or a comma/semicolon-separated list.
