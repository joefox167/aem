# AEM — Austin Entertainment Monitor

Self-hosted monitor for Austin movie theaters and concert venues. Polls sources hourly,
detects added/updated/removed events and ticket-status transitions, sends immediate
[ntfy](https://ntfy.sh) push alerts for high-priority changes, and emails a daily 8 AM digest.

## Sources (v1)

| Collector | Venue(s) | Method |
|---|---|---|
| `acl_live` | Moody Theater, 3TEN, ACL PBS tapings | RSS feed + event detail pages |
| `bullock_imax` | Bullock Museum IMAX + Texas Spirit Theater | Film listing page + ticket-store category list |

## Run locally

```bash
pip install -e .[dev]
pytest
AEM_DB_PATH=./data/aem.db AEM_CONFIG_FILE=./config/config.example.yaml \
  uvicorn aem.main:app --reload
```

Or `docker compose up --build`.

## Key endpoints

- `/` — dashboard (New Today, Recent Changes, Upcoming)
- `/movies`, `/concerts`, `/search`, `/event/{id}`
- `GET /api/events`, `/api/changes`, `/api/search`, `/api/venues`
- `GET /api/digest/preview` — what the next digest would contain
- `POST /api/admin/poll` — trigger a poll now (optional `?collector=acl_live`)
- `POST /api/admin/send-digest` — send the digest now
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
| `AEM_DIGEST_TO` | Digest recipient address |
| `AEM_BASE_URL` | Public base URL used in notification links |
| `AEM_SCHEDULER_ENABLED` | Set `false` to disable in-process scheduling |

App config (rules, favorites, quiet hours, collector toggles): see
[`config/config.example.yaml`](config/config.example.yaml).

## Deployment

Runs as a single-replica Deployment on Kubernetes (Fleet bundle `web-aem` in the
[k8_cluster](https://github.com/joefox167/k8_cluster) repo) with SQLite on a PVC.
Images are built by CI and pushed to `ghcr.io/joefox167/aem`.
