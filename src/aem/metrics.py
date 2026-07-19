from prometheus_client import Counter, Gauge, Histogram

POLL_DURATION = Histogram(
    "aem_poll_duration_seconds", "Duration of a collector poll", ["collector"]
)
COLLECTOR_ERRORS = Counter(
    "aem_collector_errors_total", "Collector poll failures", ["collector"]
)
COLLECTOR_LAST_SUCCESS = Gauge(
    "aem_collector_last_success_timestamp", "Unix time of last successful poll", ["collector"]
)
EVENTS_ACTIVE = Gauge(
    "aem_events_active", "Active events currently tracked", ["kind", "venue"]
)
CHANGES = Counter(
    "aem_changes_total", "Detected changes", ["type"]
)
NOTIFICATIONS = Counter(
    "aem_notifications_sent_total", "Notifications sent", ["channel"]
)
PARSE_DRIFT = Counter(
    "aem_parse_drift_total", "Polls where a 200 response parsed to zero events", ["collector"]
)
DIGEST_RUNS = Counter(
    "aem_digest_runs_total", "Digest send attempts", ["status"]
)
DIGEST_LAST_SENT = Gauge(
    "aem_digest_last_sent_timestamp", "Unix time of last successfully sent digest email"
)
DB_BACKUP_RUNS = Counter(
    "aem_db_backup_runs_total", "Database backup runs", ["status"]
)
DB_BACKUP_LAST_SUCCESS = Gauge(
    "aem_db_backup_last_success_timestamp", "Unix time of last successful database backup"
)
