"""Tests for scheduled data-retention cleanup tasks."""

from unittest.mock import MagicMock, patch

from backend.tasks.cleanup import cleanup_old_usage_metrics


def test_cleanup_old_usage_metrics_filters_by_metric_date() -> None:
    """Usage retention must query the real schema column."""
    supabase = MagicMock()
    query = supabase.table.return_value.delete.return_value
    query.lt.return_value.execute.return_value.data = []

    with patch("backend.tasks.cleanup.ensure_supabase", return_value=supabase):
        cleanup_old_usage_metrics()

    query.lt.assert_called_once()
    assert query.lt.call_args.args[0] == "metric_date"
