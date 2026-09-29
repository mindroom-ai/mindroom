"""Test usage metrics collection tasks."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

import pytest


class TestUsageMetrics:
    """Test usage metrics collection."""

    @pytest.fixture
    def mock_supabase(self):
        """Mock Supabase client."""
        with patch("backend.tasks.usage_metrics.supabase") as mock:
            yield mock

    @pytest.fixture
    def mock_logger(self):
        """Mock logger."""
        with patch("backend.tasks.usage_metrics.logger") as mock:
            yield mock

    @pytest.mark.asyncio
    async def test_collect_daily_usage_metrics_no_accounts(self, mock_supabase: MagicMock, mock_logger: MagicMock):
        """Test metrics collection with no active accounts."""
        from backend.tasks.usage_metrics import collect_daily_usage_metrics

        # Setup - no accounts
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[])

        # Test
        await collect_daily_usage_metrics()

        # Verify
        mock_logger.info.assert_called_with("No active accounts to collect metrics for")
        # Should not try to insert any metrics
        mock_supabase.table().insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_collect_daily_usage_metrics_with_accounts(self, mock_supabase: MagicMock, mock_logger: MagicMock):
        """Test metrics collection with active accounts."""
        from backend.tasks.usage_metrics import collect_daily_usage_metrics

        # Setup - mock accounts
        mock_accounts = Mock(data=[{"id": "account_1"}, {"id": "account_2"}])

        # Mock audit logs for metrics collection
        mock_audit_logs = Mock(
            data=[
                {"action": "create", "created_at": "2024-01-01T12:00:00"},
                {"action": "send", "created_at": "2024-01-01T13:00:00"},
                {"action": "api_call", "created_at": "2024-01-01T14:00:00"},
            ]
        )

        # Mock instances
        mock_instances = Mock(data=[{"agent_count": 3, "status": "running"}])

        # Configure mock chain
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table

        # Different responses for different queries
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:  # First call - get accounts
                return mock_table
            elif call_count in [2, 5]:  # audit_logs queries
                return mock_table
            elif call_count in [3, 6]:  # instances queries
                return mock_table
            else:  # insert calls
                return mock_table

        mock_supabase.table.side_effect = side_effect

        # Configure the chain returns
        mock_table.select.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.gte.return_value = mock_table
        mock_table.lte.return_value = mock_table
        mock_table.insert.return_value = mock_table

        # Execute returns based on query type
        execute_count = 0

        def execute_side_effect():
            nonlocal execute_count
            execute_count += 1
            if execute_count == 1:  # accounts query
                return mock_accounts
            elif execute_count in [2, 4]:  # audit logs
                return mock_audit_logs
            elif execute_count in [3, 5]:  # instances
                return mock_instances
            else:
                return Mock(data=True)

        mock_table.execute.side_effect = execute_side_effect

        # Test
        await collect_daily_usage_metrics()

        # Verify
        assert mock_supabase.table.call_count >= 4  # accounts + 2x(audit + instances + insert)
        assert mock_logger.info.call_count >= 2  # One for each account

    @pytest.mark.asyncio
    async def test_collect_daily_usage_metrics_error_handling(self, mock_supabase: MagicMock, mock_logger: MagicMock):
        """Test error handling in metrics collection."""
        from backend.tasks.usage_metrics import collect_daily_usage_metrics

        # Setup - raise exception
        mock_supabase.table().select().eq().execute.side_effect = Exception("DB Error")

        # Test
        await collect_daily_usage_metrics()

        # Verify
        mock_logger.exception.assert_called_once()
        assert "Error collecting usage metrics" in str(mock_logger.exception.call_args)

    @pytest.mark.asyncio
    async def test_collect_account_metrics(self, mock_supabase: MagicMock):
        """Test collecting metrics for a specific account."""
        from backend.tasks.usage_metrics import _collect_account_metrics

        # Setup
        start_date = datetime.now(UTC) - timedelta(days=1)
        end_date = datetime.now(UTC)

        # Mock audit logs with various actions
        mock_audit_logs = Mock(
            data=[{"action": "create"}, {"action": "send_message"}, {"action": "api_call"}, {"action": "message_sent"}]
        )

        # Mock instances
        mock_instances = Mock(data=[{"agent_count": 2}, {"agent_count": 3}])

        # Configure mock
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.select.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.gte.return_value = mock_table
        mock_table.lte.return_value = mock_table

        # Different responses for audit logs vs instances
        execute_count = 0

        def execute_side_effect():
            nonlocal execute_count
            execute_count += 1
            if execute_count == 1:
                return mock_audit_logs
            else:
                return mock_instances

        mock_table.execute.side_effect = execute_side_effect

        # Test
        metrics = await _collect_account_metrics(mock_supabase, "account_1", start_date, end_date)

        # Verify
        assert metrics["api_calls"] == 4  # Total audit logs
        assert metrics["messages_sent"] == 3  # Logs with message-like actions
        assert metrics["agent_count"] == 5  # Sum of agent counts
        assert metrics["storage_mb"] == 200  # 2 instances * 100

    @pytest.mark.asyncio
    async def test_collect_account_metrics_no_data(self, mock_supabase: MagicMock):
        """Test collecting metrics when no data exists."""
        from backend.tasks.usage_metrics import _collect_account_metrics

        # Setup - no data
        mock_supabase.table().select().eq().gte().lte().execute.return_value = Mock(data=[])
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[])

        start_date = datetime.now(UTC) - timedelta(days=1)
        end_date = datetime.now(UTC)

        # Test
        metrics = await _collect_account_metrics(mock_supabase, "account_1", start_date, end_date)

        # Verify - should return zeros
        assert metrics["messages_sent"] == 0
        assert metrics["api_calls"] == 0
        assert metrics["agent_count"] == 0
        assert metrics["storage_mb"] == 0

    @pytest.mark.asyncio
    async def test_collect_account_metrics_error_handling(self, mock_supabase: MagicMock, mock_logger: MagicMock):
        """Test error handling in account metrics collection."""
        from backend.tasks.usage_metrics import _collect_account_metrics

        # Setup - raise exception
        mock_supabase.table.side_effect = Exception("Query failed")

        start_date = datetime.now(UTC) - timedelta(days=1)
        end_date = datetime.now(UTC)

        # Test
        metrics = await _collect_account_metrics(mock_supabase, "account_1", start_date, end_date)

        # Verify - should return default metrics on error
        assert metrics["messages_sent"] == 0
        assert metrics["api_calls"] == 0
        mock_logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_collect_account_metrics_with_agent_count(self, mock_supabase: MagicMock):
        """Test agent count calculation from instances."""
        from backend.tasks.usage_metrics import _collect_account_metrics

        # Setup
        start_date = datetime.now(UTC) - timedelta(days=1)
        end_date = datetime.now(UTC)

        # Mock instances with different agent counts
        mock_instances = Mock(
            data=[
                {"agent_count": 5},
                {},  # Missing agent_count - will use default 1
                {"agent_count": 3},
            ]
        )

        mock_audit = Mock(data=[])

        # Configure mock
        mock_table = MagicMock()
        mock_supabase.table.return_value = mock_table
        mock_table.select.return_value = mock_table
        mock_table.eq.return_value = mock_table
        mock_table.gte.return_value = mock_table
        mock_table.lte.return_value = mock_table

        execute_count = 0

        def execute_side_effect():
            nonlocal execute_count
            execute_count += 1
            if execute_count == 1:
                return mock_audit
            else:
                return mock_instances

        mock_table.execute.side_effect = execute_side_effect

        # Test
        metrics = await _collect_account_metrics(mock_supabase, "account_1", start_date, end_date)

        # Verify
        # The code uses: inst.get("agent_count", 1) which means missing key becomes 1
        assert metrics["agent_count"] == 9  # 5 + 1 (missing defaults to 1) + 3
        assert metrics["storage_mb"] == 300  # 3 instances * 100
