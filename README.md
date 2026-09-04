# AEM — Austin Entertainment Monitor

Self-hosted monitor for Austin movie theaters and concert venues. Polls its sources
hourly, detects added / updated / removed events and ticket-status transitions, emails a
daily digest, and sends immediate [ntfy](https://ntfy.sh) alerts for the changes worth
interrupting you for.

## Sources

| Collector | Covers | Method |
|---|---|---|
| `ticketmaster` | Every Ticketmaster / Live Nation / Front Gate room within 40 miles of downtown Austin — Bass Concert Hall, Moody Center, Moody Amphitheater, Stubb's, Emo's, ZACH, Antone's, Scoot Inn, Germania, Cap City… | Discovery API v2 |
| `acl_live` | Moody Theater, 3TEN, ACL PBS tapings | RSS feed + event detail pages |
| `bullock_imax` | Bullock Museum IMAX + Texas Spirit Theater | Film listing page + ticket-store category list |

ACL Live and the Bullock keep dedicated collectors, and their rooms are denylisted from
the Ticketmaster collector, because their own pages carry tour names, openers and sale
state that Discovery does not expose.

## How it works

Collectors return everything their source currently lists; ingest diffs that against the
database and writes classified change rows; the rules engine decides what is worth an
immediate alert and what waits for the daily digest. A collector's first poll is a
`baseline` — its whole catalogue is recorded as history and none of it is notified.

See [docs/architecture.md](docs/architecture.md) for the pipeline, the data model and the
change-detection rules that keep it quiet.

## Documentation

| | |
|---|---|
| [architecture.md](docs/architecture.md) | Pipeline, data model, change classification, dedupe, scheduling |
| [collectors.md](docs/collectors.md) | Writing a new collector: the contract, conventions, testing |
| [notifications.md](docs/notifications.md) | Rules, quiet hours, delivery guarantees, the digest |
| [operations.md](docs/operations.md) | Deploying, secrets, backup/restore, monitoring, troubleshooting |

## Run locally

```bash
pip install -e '.[dev]'
pytest
AEM_DB_PATH=./data/aem.db AEM_CONFIG_FILE=./config/config.example.yaml \
  uvicorn aem.main:app --reload
```

Or `docker compose up --build`.

## Before pushing

`scripts/check.sh` runs exactly what CI runs — `ruff check src tests` and `pytest -q` —
against the tool versions pinned in `pyproject.toml`. It refuses to run if the venv has
drifted from the pinned ruff, because a local check on a different linter version is not
a check at all.

Install it as a pre-push hook, once per clone:

```bash
cp scripts/hooks/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push
```

A push CI would reject is then blocked locally; `git push --no-verify` bypasses it.

Ruff is pinned exactly. A ruff minor release can promote new rules to its default set
and redden CI with no code change — the pin means that happens when you bump it, not
when upstream ships it.

## Key endpoints

- `/` — dashboard (New Today, Recent Changes, Upcoming)
- `/movies`, `/concerts`, `/search`, `/event/{id}`
- `GET /api/events`, `/api/events/{id}`, `/api/changes`, `/api/search`, `/api/venues`
- `GET /api/digest/preview` — what the next digest would contain
- `POST /api/admin/poll` — poll now (optional `?collector=acl_live`)
- `POST /api/admin/send-digest` — send the digest now (`?force=true` to override the daily dedupe)
- `GET /healthz`, `GET /metrics`

## Configuration

Environment (secrets / deployment):

| Var | Purpose |
|---|---|
| `AEM_DB_PATH` | SQLite path (default `/data/aem.db`) |
| `AEM_CONFIG_FILE` | YAML config path (default `/config/config.yaml`) |
| `AEM_NTFY_URL` | Full ntfy topic URL for alerts |
| `AEM_NTFY_OPS_URL` | ntfy topic for operational warnings (collector failures) |
| `AEM_GMAIL_USER` / `AEM_GMAIL_APP_PASSWORD` | Gmail SMTP credentials for the digest |
| `AEM_DIGEST_TO` | Digest recipient address or comma-separated list |
| `AEM_TICKETMASTER_API_KEY` | [Discovery API](https://developer.ticketmaster.com) key; the `ticketmaster` collector skips itself without one |
| `AEM_BASE_URL` | Public base URL used in notification links |
| `AEM_SCHEDULER_ENABLED` | Set `false` to disable in-process scheduling |

Behavior — collectors, notification rules, favorite venues, quiet hours — lives in YAML:
see [`config/config.example.yaml`](config/config.example.yaml).

## Deployment

Single-replica Deployment on the homelab Kubernetes cluster with SQLite on a PVC. CI
builds `ghcr.io/joefox167/aem:sha-<short>` on every push to `main`; the chart lives in
the [k8_cluster](https://github.com/joefox167/k8_cluster) repo at `fleet/web-aem/chart`.

**Deploys are manual `helm upgrade`, not GitOps** — despite the `fleet/` path, Fleet is
not installed on the cluster, so pushing the k8s repo deploys nothing. See
[docs/operations.md](docs/operations.md).
