# Operations

Runbook for the deployed instance: one replica in namespace `web` on the homelab
cluster, SQLite on a PVC, reachable at `https://aem.home.arpa`.

| | |
|---|---|
| Helm release | `web-aem` (ns `web`) |
| Workload names | Deployment / Service are `aem`, not `web-aem` |
| Chart | `fleet/web-aem/chart` in the [k8_cluster](https://github.com/joefox167/k8_cluster) repo |
| Images | `ghcr.io/joefox167/aem:sha-<short>`, built by CI on push to `main` |

## Deploying

**Deploys are GitOps.** This cluster runs a Fleet agent registered to the upstream
Rancher at `rancher.home.arpa`; the `web-aem` bundle is defined by
`fleet/web-aem/fleet.yaml` in the k8s repo. Pushing to `main` is the deploy.

```bash
# 1. push app changes; CI publishes ghcr.io/joefox167/aem:sha-<short>
# 2. pin the tag in the k8s repo and push -- that is the whole deploy
$EDITOR fleet/web-aem/chart/values.yaml     # image.tag: sha-<short>
git commit -am 'chore(web-aem): promote sha-<short>' && git push
```

Fleet picks up the commit and upgrades the Helm release itself.

> **Do not run `helm upgrade` by hand against a bundle Fleet owns.** Fleet reconciles
> continuously, so a manual upgrade races its own operation — expect
> `another operation (install/upgrade/rollback) is in progress` and
> `secrets "sh.helm.release.v1.web-aem.vN" not found` — and any drift you introduce is
> reverted at the next reconcile. Change git, not the cluster.

`paused: true` in `fleet.yaml` stops Fleet reconciling the bundle; that is the escape
hatch if you ever do need to drive Helm manually.

### Watching a deploy

The `GitRepo` and `Bundle` resources live on the **upstream Rancher**, not on this
cluster, so `kubectl get gitrepo` here legitimately returns "no such resource" — that is
not evidence Fleet is missing. Watch the agent instead:

```bash
kubectl -n cattle-fleet-system logs deploy/fleet-agent -f | grep web-aem
kubectl -n web get deploy aem -o wide          # ground truth for what is running
helm history web-aem -n web                    # Fleet's own upgrades appear here
```

## Secrets

Encrypted with SOPS (age) in `fleet/web-aem/chart/files/aem-secrets.yaml`. The creation
rules only match when sops runs **from the k8s repo root**, not from `~`.

```bash
cd ~/k8s
sops fleet/web-aem/chart/files/aem-secrets.yaml
sops -d fleet/web-aem/chart/files/aem-secrets.yaml | kubectl apply --server-side -f -
kubectl -n web rollout restart deploy/aem     # env is read at pod start
```

Keys: `AEM_NTFY_URL`, `AEM_NTFY_OPS_URL`, `AEM_GMAIL_USER`, `AEM_GMAIL_APP_PASSWORD`,
`AEM_DIGEST_TO`, `AEM_TICKETMASTER_API_KEY`.

## Backups and restore

A `VACUUM INTO` snapshot runs daily at 03:00 into `/data/backups/aem-YYYYMMDD.db`,
keeping 14. `VACUUM INTO` is consistent against a live database — no downtime needed.

```bash
kubectl -n web exec deploy/aem -- ls -la /data/backups     # list
```

To restore, stop writers first — SQLite in WAL mode will not thank you for swapping the
file underneath a running process:

```bash
kubectl -n web scale deploy/aem --replicas=0

# throwaway pod holding the same PVC (aem-data)
kubectl -n web run aem-restore --image=busybox --restart=Never -it --rm \
  --overrides='{"spec":{"containers":[{"name":"aem-restore","image":"busybox",
    "stdin":true,"tty":true,"command":["sh"],
    "volumeMounts":[{"name":"data","mountPath":"/data"}]}],
    "volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"aem-data"}}]}}'

#   ls /data/backups
#   cp /data/backups/aem-YYYYMMDD.db /data/aem.db
#   rm -f /data/aem.db-wal /data/aem.db-shm     # stale WAL would resurrect old writes
#   exit

kubectl -n web scale deploy/aem --replicas=1
```

Verify a copy before trusting it: `sqlite3 aem-YYYYMMDD.db 'PRAGMA integrity_check;'`.

## Monitoring

`/metrics` is scraped by kube-prometheus-stack.

| Metric | Watch for |
|---|---|
| `aem_collector_last_success_timestamp` | No success in > 6h = a stuck source |
| `aem_collector_errors_total` | Sustained increase |
| `aem_parse_drift_total` | A `200` parsing to zero events: site markup changed |
| `aem_events_active{kind,venue}` | A cliff means a source stopped listing |
| `aem_changes_total{type}` | A `removed` spike is usually a source problem, not reality |
| `aem_digest_last_sent_timestamp` | Older than ~26h = digest not going out |
| `aem_db_backup_last_success_timestamp` | Older than ~26h = backups stopped |
| `aem_tm_api_calls_total{status}` | `rate_limited` climbing = poll too aggressive |
| `aem_tm_window_truncated_total` | A Discovery query hit the 1000-result cap; lower `window_days` |

`/healthz` returns `{"ok": true, "scheduler_running": true}` and is wired to both
probes. Note it reports the scheduler *exists and is running*, not that polls succeed —
use the collector metrics for that.

## Manual operations

```bash
P="kubectl -n web exec deploy/aem -- python -c"

# poll now (all collectors, or one)
$P "import urllib.request as u; print(u.urlopen(u.Request('http://localhost:8000/api/admin/poll?collector=ticketmaster', method='POST'), timeout=900).read())"

# force today's digest
$P "import urllib.request as u; print(u.urlopen(u.Request('http://localhost:8000/api/admin/send-digest?force=true', method='POST'), timeout=180).read())"
```

A manual poll waits on the same lock as the scheduled one, so it cannot race it.

## Troubleshooting

**A collector reports `error` in `/api/changes` or logs.** Read the stored traceback:
`poll_runs.error` keeps the last failure per run. One collector failing never affects
the others, and a failed poll does not advance `missing_polls`, so no false removals
accumulate while you fix it.

**Mass "removed" changes.** Almost always source drift, not reality. Check
`aem_parse_drift_total` and the collector's fixture against a fresh response. The
removal threshold (3 polls) buys roughly three hours to notice.

**Ticketmaster returning nothing.** A `401` means the key; the collector redacts the key
from error text, so look for `apikey=***` in the message. Check the key is present:
`kubectl -n web get secret aem-secrets -o jsonpath='{.data.AEM_TICKETMASTER_API_KEY}' | wc -c`.

**Ticketmaster missing venues.** `aem_tm_window_truncated_total` means a segment/window
exceeded Discovery's 1000-result paging cap and got cut off — lower `window_days`.

**Digest not arriving.** `/api/digest/preview` shows what it would contain; an empty
digest is skipped unless forced. `sent: true` means SMTP accepted the message, which is
not the same as inbox delivery.

**`UPGRADE FAILED: secrets "sh.helm.release.v1.web-aem.vN" not found`.** Almost always
a manual `helm upgrade` racing Fleet's reconcile of the same release. Stop driving Helm
by hand and let Fleet finish; `helm history web-aem -n web` and
`kubectl -n web get deploy aem -o wide` show where it actually landed.

## Local development

```bash
pip install -e '.[dev]'
scripts/check.sh                    # exactly what CI runs
AEM_DB_PATH=./data/aem.db AEM_CONFIG_FILE=./config/config.example.yaml \
  uvicorn aem.main:app --reload
```

Install the pre-push hook once per clone so CI cannot fail on something reproducible
locally:

```bash
cp scripts/hooks/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push
```

Ruff is pinned exactly in `pyproject.toml`, and `check.sh` refuses to run when the venv
has drifted from the pin — a lint check on a different linter version proves nothing
about CI. Bump the pin deliberately, then re-run `scripts/check.sh`.

`scripts/tm_smoke.py` probes live Ticketmaster coverage against a throwaway database,
for tuning segments, windows and denylists without touching production:

```bash
python scripts/tm_smoke.py --key-file ~/.config/aem/ticketmaster.key --horizon-days 90
```
