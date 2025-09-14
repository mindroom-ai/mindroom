"""Comprehensive HTTP API tests for usage endpoints."""

from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient


class TestUsageEndpoints:
    """Test usage endpoints via HTTP API."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    @pytest.fixture
    def mock_supabase(self):
        """Mock Supabase client."""
        with patch("backend.routes.usage.ensure_supabase") as mock:
            sb = MagicMock()
            mock.return_value = sb
            yield sb

    @pytest.fixture
    def mock_verify_user(self):
        """Mock user verification."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_user

        def override_verify_user():
            return {"account_id": "acc_test_123", "email": "test@example.com"}

        app.dependency_overrides[verify_user] = override_verify_user
        yield
        app.dependency_overrides.clear()

    def test_get_usage_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test getting usage metrics successfully."""
        # Mock subscriptions table query
        sub_mock = MagicMock()
        sub_mock.select.return_value = sub_mock
        sub_mock.eq.return_value = sub_mock
        sub_mock.execute.return_value = Mock(data=[{"id": "sub_123"}])

        # Mock usage_metrics table query
        usage_mock = MagicMock()
        usage_mock.select.return_value = usage_mock
        usage_mock.eq.return_value = usage_mock
        usage_mock.gte.return_value = usage_mock
        usage_mock.order.return_value = usage_mock
        usage_mock.execute.return_value = Mock(
            data=[
                {
                    "subscription_id": "sub_123",
                    "metric_date": "2024-01-01",
                    "messages_sent": 100,
                    "agents_used": 5,
                    "storage_used_gb": 2.5,
                },
                {
                    "subscription_id": "sub_123",
                    "metric_date": "2024-01-02",
                    "messages_sent": 150,
                    "agents_used": 6,
                    "storage_used_gb": 2.7,
                },
            ]
        )

        # Configure table method
        def table_side_effect(name):
            if name == "subscriptions":
                return sub_mock
            else:
                return usage_mock

        mock_supabase.table = Mock(side_effect=table_side_effect)

        # Make request
        response = client.get("/my/usage")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert "usage" in data
        assert "aggregated" in data
        assert len(data["usage"]) == 2
        assert data["aggregated"]["totalMessages"] == 250  # 100 + 150
        assert data["aggregated"]["totalAgents"] == 6  # max
        assert data["aggregated"]["totalStorage"] == 2.7  # max

    def test_get_usage_with_days_parameter(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test getting usage metrics with custom days parameter."""
        # Mock subscriptions table query
        sub_mock = MagicMock()
        sub_mock.select.return_value = sub_mock
        sub_mock.eq.return_value = sub_mock
        sub_mock.execute.return_value = Mock(data=[{"id": "sub_123"}])

        # Mock usage_metrics table query
        usage_mock = MagicMock()
        usage_mock.select.return_value = usage_mock
        usage_mock.eq.return_value = usage_mock
        usage_mock.gte.return_value = usage_mock
        usage_mock.order.return_value = usage_mock
        usage_mock.execute.return_value = Mock(
            data=[
                {
                    "subscription_id": "sub_123",
                    "metric_date": "2024-01-01",
                    "messages_sent": 50,
                    "agents_used": 3,
                    "storage_used_gb": 1.0,
                }
            ]
        )

        # Configure table method
        def table_side_effect(name):
            if name == "subscriptions":
                return sub_mock
            else:
                return usage_mock

        mock_supabase.table = Mock(side_effect=table_side_effect)

        # Make request with 7 days
        response = client.get("/my/usage?days=7")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert len(data["usage"]) == 1

    def test_get_usage_no_subscription(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test getting usage when user has no subscription."""
        # Setup - no subscription found
        sub_mock = MagicMock()
        sub_mock.select.return_value = sub_mock
        sub_mock.eq.return_value = sub_mock
        sub_mock.execute.return_value = Mock(data=[])

        mock_supabase.table.return_value = sub_mock

        # Make request
        response = client.get("/my/usage")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["usage"] == []
        assert data["aggregated"]["totalMessages"] == 0
        assert data["aggregated"]["totalAgents"] == 0
        assert data["aggregated"]["totalStorage"] == 0

    def test_get_usage_no_metrics(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test getting usage when no metrics exist."""
        # Mock subscriptions table query
        sub_mock = MagicMock()
        sub_mock.select.return_value = sub_mock
        sub_mock.eq.return_value = sub_mock
        sub_mock.execute.return_value = Mock(data=[{"id": "sub_123"}])

        # Mock usage_metrics table query with no data
        usage_mock = MagicMock()
        usage_mock.select.return_value = usage_mock
        usage_mock.eq.return_value = usage_mock
        usage_mock.gte.return_value = usage_mock
        usage_mock.order.return_value = usage_mock
        usage_mock.execute.return_value = Mock(data=[])

        # Configure table method
        def table_side_effect(name):
            if name == "subscriptions":
                return sub_mock
            else:
                return usage_mock

        mock_supabase.table = Mock(side_effect=table_side_effect)

        # Make request
        response = client.get("/my/usage")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["usage"] == []
        assert data["aggregated"]["totalMessages"] == 0
        assert data["aggregated"]["totalAgents"] == 0
        assert data["aggregated"]["totalStorage"] == 0.0

    def test_get_usage_unauthorized(self, client: TestClient):
        """Test getting usage without authentication."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_user
        from fastapi import HTTPException

        def override_verify_user():
            raise HTTPException(status_code=401, detail="Unauthorized")

        app.dependency_overrides[verify_user] = override_verify_user
        try:
            response = client.get("/my/usage")
            assert response.status_code == 401
        finally:
            app.dependency_overrides.clear()

    def test_get_usage_with_null_values(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test getting usage with null values in metrics."""
        # Mock subscriptions table query
        sub_mock = MagicMock()
        sub_mock.select.return_value = sub_mock
        sub_mock.eq.return_value = sub_mock
        sub_mock.execute.return_value = Mock(data=[{"id": "sub_123"}])

        # Mock usage_metrics table query with null values
        usage_mock = MagicMock()
        usage_mock.select.return_value = usage_mock
        usage_mock.eq.return_value = usage_mock
        usage_mock.gte.return_value = usage_mock
        usage_mock.order.return_value = usage_mock
        usage_mock.execute.return_value = Mock(
            data=[
                {
                    "subscription_id": "sub_123",
                    "metric_date": "2024-01-01",
                    "messages_sent": 100,
                    "agents_used": None,  # null value
                    "storage_used_gb": None,  # null value
                },
                {
                    "subscription_id": "sub_123",
                    "metric_date": "2024-01-02",
                    "messages_sent": 50,
                    "agents_used": 3,
                    "storage_used_gb": 1.5,
                },
            ]
        )

        # Configure table method
        def table_side_effect(name):
            if name == "subscriptions":
                return sub_mock
            else:
                return usage_mock

        mock_supabase.table = Mock(side_effect=table_side_effect)

        # Make request
        response = client.get("/my/usage")

        # Verify
        assert response.status_code == 200
        data = response.json()
        # Should handle null values gracefully
        assert data["aggregated"]["totalMessages"] == 150
        assert data["aggregated"]["totalAgents"] == 3
        assert data["aggregated"]["totalStorage"] == 1.5

    def test_get_usage_large_dataset(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test getting usage with large dataset."""
        # Setup - create 100 days of metrics
        metrics = []
        for i in range(100):
            metrics.append(
                {
                    "subscription_id": "sub_123",
                    "metric_date": f"2024-01-{i + 1:02d}",
                    "messages_sent": 10 * (i + 1),
                    "agents_used": min(i + 1, 10),  # cap at 10
                    "storage_used_gb": 0.1 * (i + 1),
                }
            )

        # Mock subscriptions table query
        sub_mock = MagicMock()
        sub_mock.select.return_value = sub_mock
        sub_mock.eq.return_value = sub_mock
        sub_mock.execute.return_value = Mock(data=[{"id": "sub_123"}])

        # Mock usage_metrics table query
        usage_mock = MagicMock()
        usage_mock.select.return_value = usage_mock
        usage_mock.eq.return_value = usage_mock
        usage_mock.gte.return_value = usage_mock
        usage_mock.order.return_value = usage_mock
        usage_mock.execute.return_value = Mock(data=metrics)

        # Configure table method
        def table_side_effect(name):
            if name == "subscriptions":
                return sub_mock
            else:
                return usage_mock

        mock_supabase.table = Mock(side_effect=table_side_effect)

        # Make request
        response = client.get("/my/usage")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert len(data["usage"]) == 100
        # Sum of 10 + 20 + ... + 1000 = 10 * (1 + 2 + ... + 100) = 10 * 5050 = 50500
        assert data["aggregated"]["totalMessages"] == 50500
        assert data["aggregated"]["totalAgents"] == 10  # max capped at 10
        assert data["aggregated"]["totalStorage"] == 10.0  # 0.1 * 100
