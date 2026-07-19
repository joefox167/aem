from unittest.mock import Mock, patch

from aem.db import init_db, make_engine
from aem.scheduler import backup_db


def test_backup_db_writes_backup_and_updates_metrics(tmp_path):
    db_path = tmp_path / "aem.db"
    engine = make_engine(str(db_path))
    init_db(engine)
    engine.dispose()

    with patch("aem.scheduler.metrics.DB_BACKUP_RUNS") as mock_runs:
        with patch("aem.scheduler.metrics.DB_BACKUP_LAST_SUCCESS") as mock_last_success:
            success_metric = Mock()
            mock_runs.labels.return_value = success_metric
            backup_db(str(db_path))

    backups = list((tmp_path / "backups").glob("aem-*.db"))
    assert len(backups) == 1
    mock_runs.labels.assert_called_once_with(status="success")
    success_metric.inc.assert_called_once()
    mock_last_success.set_to_current_time.assert_called_once()
