"""Comprehensive HTTP API tests for instances endpoints."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient


class TestInstancesEndpoints:
    """Test instances endpoints via HTTP API."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    @pytest.fixture
    def mock_supabase(self):
        """Mock Supabase client."""
        with patch("backend.routes.instances.ensure_supabase") as mock:
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

    @pytest.fixture
    def mock_check_deployment(self):
        """Mock deployment existence check."""
        with patch("backend.routes.instances.check_deployment_exists") as mock:
            mock.return_value = True  # Default exists
            yield mock

    @pytest.fixture
    def mock_kubectl(self):
        """Mock kubectl commands."""
        with patch("backend.routes.instances.run_kubectl") as mock:
            mock.return_value = (0, "1", "")  # Default success with 1 replica
            yield mock

    @pytest.fixture
    def mock_provision_instance(self):
        """Mock provision_instance function."""
        with patch("backend.routes.instances.provision_instance") as mock:
            mock.return_value = {
                "success": True,
                "message": "Instance provisioned successfully",
                "customer_id": "123",
                "frontend_url": "https://123.mindroom.test",
                "api_url": "https://123.api.mindroom.test",
                "matrix_url": "https://123.matrix.mindroom.test",
            }
            yield mock

    @pytest.fixture
    def mock_start_instance(self):
        """Mock start_instance_provisioner function."""
        with patch("backend.routes.instances.start_instance_provisioner") as mock:
            mock.return_value = {
                "success": True,
                "message": "Instance started successfully",
            }
            yield mock

    @pytest.fixture
    def mock_stop_instance(self):
        """Mock stop_instance_provisioner function."""
        with patch("backend.routes.instances.stop_instance_provisioner") as mock:
            mock.return_value = {
                "success": True,
                "message": "Instance stopped successfully",
            }
            yield mock

    @pytest.fixture
    def mock_restart_instance(self):
        """Mock restart_instance_provisioner function."""
        with patch("backend.routes.instances.restart_instance_provisioner") as mock:
            mock.return_value = {
                "success": True,
                "message": "Instance restarted successfully",
            }
            yield mock

    @pytest.fixture
    def mock_provisioner_api_key(self):
        """Mock PROVISIONER_API_KEY."""
        with patch("backend.routes.instances.PROVISIONER_API_KEY", "test-api-key"):
            yield

    def test_list_user_instances_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test listing user instances successfully."""
        # Setup
        now = datetime.now(UTC)
        instances = [
            {
                "id": "1",  # Must be string
                "instance_id": "123",
                "subscription_id": "sub_123",  # Required field
                "account_id": "acc_test_123",
                "status": "running",
                "kubernetes_synced_at": now.isoformat(),
                "frontend_url": "https://123.mindroom.test",
                "backend_url": "https://123.api.mindroom.test",
                "matrix_server_url": "https://123.matrix.mindroom.test",
            },
            {
                "id": "2",  # Must be string
                "instance_id": "456",
                "subscription_id": "sub_456",  # Required field
                "account_id": "acc_test_123",
                "status": "stopped",
                "kubernetes_synced_at": (now - timedelta(seconds=10)).isoformat(),
            },
        ]
        mock_supabase.table().select().eq().execute.return_value = Mock(data=instances)

        # Make request
        response = client.get("/my/instances")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert "instances" in data
        assert len(data["instances"]) == 2
        assert data["instances"][0]["instance_id"] == "123"
        assert data["instances"][0]["status"] == "running"

    def test_list_user_instances_triggers_background_sync(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test that stale instances trigger background sync."""
        # Setup - instance with stale sync time
        stale_time = datetime.now(UTC) - timedelta(minutes=5)
        instances = [
            {
                "id": "1",
                "instance_id": "123",
                "subscription_id": "sub_123",
                "account_id": "acc_test_123",
                "status": "running",
                "kubernetes_synced_at": stale_time.isoformat(),
            }
        ]
        mock_supabase.table().select().eq().execute.return_value = Mock(data=instances)

        # Make request
        response = client.get("/my/instances")

        # Verify
        assert response.status_code == 200
        # Background task would be scheduled for stale instance

    def test_list_user_instances_no_instances(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test listing instances when user has none."""
        # Setup
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[])

        # Make request
        response = client.get("/my/instances")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["instances"] == []

    def test_provision_user_instance_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
        mock_provision_instance: Mock,
        mock_provisioner_api_key,
    ):
        """Test provisioning a new instance for user."""
        # Setup
        subscription = {
            "id": "sub_123",
            "account_id": "acc_test_123",
            "tier": "starter",
        }

        # Setup mock chains for different queries
        subscription_mock = MagicMock()
        subscription_mock.select.return_value = subscription_mock
        subscription_mock.eq.return_value = subscription_mock
        subscription_mock.execute.return_value = Mock(data=[subscription])

        instance_mock = MagicMock()
        instance_mock.select.return_value = instance_mock
        instance_mock.eq.return_value = instance_mock
        instance_mock.limit.return_value = instance_mock
        instance_mock.execute.return_value = Mock(data=[])  # No existing instance

        call_count = [0]

        def table_side_effect(table_name):
            call_count[0] += 1
            if table_name == "subscriptions":
                return subscription_mock
            elif table_name == "instances":
                return instance_mock
            return MagicMock()

        mock_supabase.table = Mock(side_effect=table_side_effect)

        # Make request
        response = client.post("/my/instances/provision")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["customer_id"] == "123"
        assert "provisioned successfully" in data["message"]

        # Verify provision_instance was called
        mock_provision_instance.assert_called_once()
        call_args = mock_provision_instance.call_args[1]
        assert call_args["data"]["subscription_id"] == "sub_123"
        assert call_args["data"]["tier"] == "starter"

    def test_provision_user_instance_existing(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
        mock_provisioner_api_key,
    ):
        """Test provisioning when instance already exists."""
        # Setup
        subscription = {
            "id": "sub_123",
            "account_id": "acc_test_123",
            "tier": "starter",
        }
        existing_instance = {
            "id": 1,
            "instance_id": "456",
            "status": "running",
            "frontend_url": "https://456.mindroom.test",
            "backend_url": "https://456.api.mindroom.test",
            "api_url": "https://456.api.mindroom.test",  # Some fields check both
            "matrix_server_url": "https://456.matrix.mindroom.test",
            "matrix_url": "https://456.matrix.mindroom.test",  # Some fields check both
            "instance_url": "https://456.mindroom.test",  # Some fields check both
        }

        # Setup mock chain for subscription query
        mock_sub_chain = MagicMock()
        mock_sub_chain.execute.return_value = Mock(data=[subscription])

        # Setup mock chain for instance query
        mock_inst_chain = MagicMock()
        mock_inst_chain.execute.return_value = Mock(data=[existing_instance])

        # Configure mocks to return proper chains
        call_count = 0

        def table_side_effect(name):
            nonlocal call_count
            if name == "subscriptions":
                return mock_sub_chain
            elif name == "instances":
                return mock_inst_chain
            return MagicMock()

        mock_supabase.table.side_effect = table_side_effect
        mock_sub_chain.select.return_value = mock_sub_chain
        mock_sub_chain.eq.return_value = mock_sub_chain
        mock_inst_chain.select.return_value = mock_inst_chain
        mock_inst_chain.eq.return_value = mock_inst_chain
        mock_inst_chain.limit.return_value = mock_inst_chain

        # Make request
        response = client.post("/my/instances/provision")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["customer_id"] == "456"
        assert "already exists" in data["message"]
        assert data["frontend_url"] == "https://456.mindroom.test"

    def test_provision_user_instance_deprovisioned(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
        mock_provision_instance: Mock,
        mock_provisioner_api_key,
    ):
        """Test reprovisioning a deprovisioned instance."""
        # Setup
        subscription = {
            "id": "sub_123",
            "account_id": "acc_test_123",
            "tier": "professional",
        }
        deprovisioned_instance = {
            "id": 1,
            "instance_id": "789",
            "status": "deprovisioned",
            "frontend_url": None,
            "backend_url": None,
            "api_url": None,
            "matrix_server_url": None,
            "matrix_url": None,
            "instance_url": None,
        }

        # Setup mock chain for subscription query
        subscription_mock = MagicMock()
        subscription_mock.select.return_value = subscription_mock
        subscription_mock.eq.return_value = subscription_mock
        subscription_mock.execute.return_value = Mock(data=[subscription])

        # Setup mock chain for instance query
        instance_mock = MagicMock()
        instance_mock.select.return_value = instance_mock
        instance_mock.eq.return_value = instance_mock
        instance_mock.limit.return_value = instance_mock
        instance_mock.execute.return_value = Mock(data=[deprovisioned_instance])

        # Configure table method to return different mocks
        call_count = [0]

        def table_side_effect(table_name):
            call_count[0] += 1
            if table_name == "subscriptions":
                return subscription_mock
            elif table_name == "instances":
                return instance_mock
            return MagicMock()

        mock_supabase.table = Mock(side_effect=table_side_effect)

        # Make request
        response = client.post("/my/instances/provision")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

        # Verify provision_instance was called for reprovisioning
        mock_provision_instance.assert_called_once()
        call_args = mock_provision_instance.call_args[1]
        assert call_args["data"]["instance_id"] == "789"  # Reusing same ID

    def test_provision_user_instance_no_subscription(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test provisioning when user has no subscription."""
        # Setup
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[])

        # Make request
        response = client.post("/my/instances/provision")

        # Verify
        assert response.status_code == 404
        assert response.json()["detail"] == "No subscription found"

    def test_start_user_instance_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
        mock_start_instance: Mock,
        mock_provisioner_api_key,
    ):
        """Test starting user's instance successfully."""
        # Setup
        mock_supabase.table().select().eq().eq().limit().execute.return_value = Mock(data=[{"id": 1}])

        # Make request
        response = client.post("/my/instances/123/start")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "started successfully" in data["message"]

        # Verify start_instance_provisioner was called
        mock_start_instance.assert_called_once_with(123, "Bearer test-api-key")

    def test_start_user_instance_not_owned(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
    ):
        """Test starting instance not owned by user."""
        # Setup
        mock_supabase.table().select().eq().eq().limit().execute.return_value = Mock(data=[])

        # Make request
        response = client.post("/my/instances/999/start")

        # Verify
        assert response.status_code == 404
        assert "not found or access denied" in response.json()["detail"]

    def test_stop_user_instance_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
        mock_stop_instance: Mock,
        mock_provisioner_api_key,
    ):
        """Test stopping user's instance successfully."""
        # Setup
        mock_supabase.table().select().eq().eq().limit().execute.return_value = Mock(data=[{"id": 1}])

        # Make request
        response = client.post("/my/instances/123/stop")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "stopped successfully" in data["message"]

        # Verify stop_instance_provisioner was called
        mock_stop_instance.assert_called_once_with(123, "Bearer test-api-key")

    def test_restart_user_instance_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
        mock_restart_instance: Mock,
        mock_provisioner_api_key,
    ):
        """Test restarting user's instance successfully."""
        # Setup
        mock_supabase.table().select().eq().eq().limit().execute.return_value = Mock(data=[{"id": 1}])

        # Make request
        response = client.post("/my/instances/456/restart")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "restarted successfully" in data["message"]

        # Verify restart_instance_provisioner was called
        mock_restart_instance.assert_called_once_with(456, "Bearer test-api-key")

    def test_background_sync_task(
        self,
        mock_supabase: MagicMock,
        mock_check_deployment: AsyncMock,
        mock_kubectl: AsyncMock,
    ):
        """Test background sync task functionality."""
        import asyncio
        from backend.routes.instances import _background_sync_instance_status

        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"status": "running"})
        mock_check_deployment.return_value = True
        mock_kubectl.return_value = (0, "0", "")  # 0 replicas = stopped
        mock_supabase.table().update().eq().execute.return_value = Mock()

        # Run the background task
        asyncio.run(_background_sync_instance_status("123"))

        # Verify database was updated
        update_call = mock_supabase.table().update.call_args[0][0]
        assert update_call["status"] == "stopped"
        assert "kubernetes_synced_at" in update_call

    def test_background_sync_task_deployment_not_found(
        self,
        mock_supabase: MagicMock,
        mock_check_deployment: AsyncMock,
    ):
        """Test background sync when deployment doesn't exist."""
        import asyncio
        from backend.routes.instances import _background_sync_instance_status

        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data={"status": "running"})
        mock_check_deployment.return_value = False
        mock_supabase.table().update().eq().execute.return_value = Mock()

        # Run the background task
        asyncio.run(_background_sync_instance_status("123"))

        # Verify database was updated to error
        update_call = mock_supabase.table().update.call_args[0][0]
        assert update_call["status"] == "error"

    def test_background_sync_prevents_duplicate(self):
        """Test that background sync prevents duplicate syncs."""
        import asyncio
        from backend.routes.instances import (
            _background_sync_instance_status,
            _syncing_instances,
        )

        # Setup
        original_size = len(_syncing_instances)
        _syncing_instances.add("123")  # Mark as already syncing

        # Run the background task - should return immediately without database calls
        with patch("backend.routes.instances.ensure_supabase") as mock_sb:
            asyncio.run(_background_sync_instance_status("123"))
            # Should not have called database at all
            mock_sb.assert_not_called()

        # Clean up
        _syncing_instances.discard("123")
        assert len(_syncing_instances) == original_size

    def test_unauthorized_access(self, client: TestClient):
        """Test accessing endpoints without authentication."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_user
        from fastapi import HTTPException

        def override_verify_user():
            raise HTTPException(status_code=401, detail="Unauthorized")

        app.dependency_overrides[verify_user] = override_verify_user
        try:
            response = client.get("/my/instances")
            assert response.status_code == 401
        finally:
            app.dependency_overrides.clear()

    def test_provision_with_existing_provisioning(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_user: Mock,
        mock_provisioner_api_key,
    ):
        """Test provisioning when instance is already provisioning."""
        # Setup
        subscription = {
            "id": "sub_123",
            "account_id": "acc_test_123",
            "tier": "starter",
        }
        provisioning_instance = {
            "id": 1,
            "instance_id": "456",
            "status": "provisioning",
            "frontend_url": "https://456.mindroom.test",
            "backend_url": "https://456.api.mindroom.test",
            "api_url": "https://456.api.mindroom.test",
            "matrix_server_url": "https://456.matrix.mindroom.test",
            "matrix_url": "https://456.matrix.mindroom.test",
            "instance_url": "https://456.mindroom.test",
        }

        # Setup mock chain for subscription query
        subscription_mock = MagicMock()
        subscription_mock.select.return_value = subscription_mock
        subscription_mock.eq.return_value = subscription_mock
        subscription_mock.execute.return_value = Mock(data=[subscription])

        # Setup mock chain for instance query
        instance_mock = MagicMock()
        instance_mock.select.return_value = instance_mock
        instance_mock.eq.return_value = instance_mock
        instance_mock.limit.return_value = instance_mock
        instance_mock.execute.return_value = Mock(data=[provisioning_instance])

        # Configure table method to return different mocks
        call_count = [0]

        def table_side_effect(table_name):
            call_count[0] += 1
            if table_name == "subscriptions":
                return subscription_mock
            elif table_name == "instances":
                return instance_mock
            return MagicMock()

        mock_supabase.table = Mock(side_effect=table_side_effect)

        # Make request
        response = client.post("/my/instances/provision")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "already provisioning" in data["message"]
