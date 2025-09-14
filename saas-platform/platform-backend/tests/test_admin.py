"""Comprehensive HTTP API tests for admin endpoints."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient


class TestAdminEndpoints:
    """Test admin endpoints via HTTP API."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    @pytest.fixture
    def mock_supabase(self):
        """Mock Supabase client."""
        with patch("backend.routes.admin.ensure_supabase") as mock:
            sb = MagicMock()
            mock.return_value = sb
            yield sb

    @pytest.fixture
    def mock_verify_admin(self):
        """Mock admin verification."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_admin

        def override_verify_admin():
            return {
                "user_id": "admin_123",
                "email": "admin@example.com",
                "is_admin": True,
            }

        app.dependency_overrides[verify_admin] = override_verify_admin
        yield
        app.dependency_overrides.clear()

    @pytest.fixture
    def mock_check_deployment(self):
        """Mock deployment existence check."""
        with patch("backend.k8s.check_deployment_exists") as mock:
            mock.return_value = True
            yield mock

    @pytest.fixture
    def mock_kubectl(self):
        """Mock kubectl commands."""
        with patch("backend.k8s.run_kubectl") as mock:
            mock.return_value = (0, "1", "")  # Default success with 1 replica
            yield mock

    @pytest.fixture
    def mock_provisioner_api_key(self):
        """Mock PROVISIONER_API_KEY."""
        with patch("backend.routes.admin.PROVISIONER_API_KEY", "test-api-key"):
            yield

    def test_admin_stats_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
    ):
        """Test getting admin statistics successfully."""
        # Setup - create separate mock chains for each table query
        accounts_mock = MagicMock()
        accounts_mock.select.return_value = accounts_mock
        accounts_mock.execute.return_value = Mock(
            data=[{}, {}, {}, {}, {}, {}, {}, {}, {}, {}]
        )  # 10 accounts

        subscriptions_mock = MagicMock()
        subscriptions_mock.select.return_value = subscriptions_mock
        subscriptions_mock.eq.return_value = subscriptions_mock
        subscriptions_mock.execute.return_value = Mock(
            data=[{}, {}, {}, {}, {}, {}, {}, {}]
        )  # 8 active

        instances_mock = MagicMock()
        instances_mock.select.return_value = instances_mock
        instances_mock.eq.return_value = instances_mock
        instances_mock.execute.return_value = Mock(
            data=[{}, {}, {}, {}, {}, {}, {}]
        )  # 7 running

        audit_mock = MagicMock()
        audit_mock.select.return_value = audit_mock
        audit_mock.order.return_value = audit_mock
        audit_mock.limit.return_value = audit_mock
        audit_mock.execute.return_value = Mock(data=[])  # no recent logs

        # Configure table method to return different mocks
        def table_side_effect(table_name):
            if table_name == "accounts":
                return accounts_mock
            elif table_name == "subscriptions":
                return subscriptions_mock
            elif table_name == "instances":
                return instances_mock
            elif table_name == "audit_logs":
                return audit_mock
            return MagicMock()

        mock_supabase.table = Mock(side_effect=table_side_effect)

        # Make request
        response = client.get("/admin/stats")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["accounts"] == 10
        assert data["active_subscriptions"] == 8
        assert data["running_instances"] == 7

    def test_admin_stats_unauthorized(self, client: TestClient):
        """Test accessing admin stats without authorization."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_admin
        from fastapi import HTTPException

        def override_verify_admin():
            raise HTTPException(status_code=401, detail="Unauthorized")

        app.dependency_overrides[verify_admin] = override_verify_admin
        try:
            response = client.get("/admin/stats")
            assert response.status_code == 401
        finally:
            app.dependency_overrides.clear()

    def test_admin_start_instance(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
        mock_provisioner_api_key,
    ):
        """Test admin starting an instance."""
        # Setup - mock the provisioner function
        with patch("backend.routes.admin.start_instance_provisioner") as mock_start:
            mock_start.return_value = {
                "success": True,
                "message": "Instance started",
            }

            # Make request
            response = client.post("/admin/instances/123/start")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert "started" in data["message"]

    def test_admin_stop_instance(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
        mock_provisioner_api_key,
    ):
        """Test admin stopping an instance."""
        # Setup - mock the provisioner function
        with patch("backend.routes.admin.stop_instance_provisioner") as mock_stop:
            mock_stop.return_value = {
                "success": True,
                "message": "Instance stopped",
            }

            # Make request
            response = client.post("/admin/instances/456/stop")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert "stopped" in data["message"]

    def test_admin_restart_instance(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
        mock_provisioner_api_key,
    ):
        """Test admin restarting an instance."""
        # Setup - mock the provisioner function
        with patch("backend.routes.admin.restart_instance_provisioner") as mock_restart:
            mock_restart.return_value = {
                "success": True,
                "message": "Instance restarted",
            }

            # Make request
            response = client.post("/admin/instances/789/restart")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert "restarted" in data["message"]

    def test_admin_uninstall_instance(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
        mock_provisioner_api_key,
    ):
        """Test admin uninstalling an instance."""
        with patch("backend.routes.admin.uninstall_instance") as mock_uninstall:
            mock_uninstall.return_value = {
                "success": True,
                "message": "Instance uninstalled",
            }

            # Make request
            response = client.delete("/admin/instances/123/uninstall")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert "uninstalled" in data["message"]

    def test_admin_provision_instance(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
        mock_provisioner_api_key,
    ):
        """Test admin provisioning an instance."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"id": "sub_123", "account_id": "acc_123", "tier": "starter"}
        )

        with patch("backend.routes.provisioner.provision_instance") as mock_provision:
            mock_provision.return_value = {
                "success": True,
                "message": "Instance provisioned",
                "customer_id": "123",
                "frontend_url": "https://123.mindroom.test",
                "api_url": "https://123.api.mindroom.test",
                "matrix_url": "https://123.matrix.mindroom.test",
            }

            # Make request
            response = client.post("/admin/instances/123/provision")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True

    def test_admin_sync_instances(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
        mock_provisioner_api_key,
    ):
        """Test admin syncing instances."""
        with patch("backend.routes.provisioner.sync_instances") as mock_sync:
            mock_sync.return_value = {
                "total": 5,
                "synced": 2,
                "errors": 0,
                "updates": [],
            }

            # Make request
            response = client.post("/admin/sync-instances")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 5
            assert data["synced"] == 2

    def test_admin_get_account_details(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
    ):
        """Test admin getting account details."""
        # Setup
        account_data = {
            "id": "acc_123",
            "email": "user@example.com",
            "status": "active",
            "created_at": datetime.now(UTC).isoformat(),
        }
        subscription_data = {
            "id": "sub_123",
            "account_id": "acc_123",
            "tier": "professional",
            "status": "active",
        }
        instance_data = {
            "id": "inst_123",
            "instance_id": "123",
            "account_id": "acc_123",
            "status": "running",
        }

        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data=account_data
        )
        mock_supabase.table().select().eq().execute.side_effect = [
            Mock(data=[subscription_data]),  # subscriptions
            Mock(data=[instance_data]),  # instances
            Mock(data=[]),  # payments
        ]

        # Make request
        response = client.get("/admin/accounts/acc_123")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["account"]["id"] == "acc_123"
        assert len(data["subscriptions"]) == 1
        assert len(data["instances"]) == 1

    def test_admin_update_account_status(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
    ):
        """Test admin updating account status."""
        # Setup
        mock_supabase.table().update().eq().execute.return_value = Mock(
            data=[{"id": "acc_123", "status": "suspended"}]
        )

        # Make request
        response = client.put(
            "/admin/accounts/acc_123/status",
            json={"status": "suspended", "reason": "Payment failed"},
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["account"]["status"] == "suspended"

    def test_admin_logout(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
    ):
        """Test admin logout."""
        # Make request
        response = client.post("/admin/auth/logout")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_admin_list_resources(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
    ):
        """Test admin listing resources."""
        # Setup
        mock_supabase.table().select().execute.return_value = Mock(
            data=[
                {"id": "1", "name": "Resource 1"},
                {"id": "2", "name": "Resource 2"},
            ]
        )

        # Make request
        response = client.get("/admin/accounts")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["data"]) == 2

    def test_admin_get_single_resource(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
    ):
        """Test admin getting a single resource."""
        # Setup
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"id": "123", "name": "Test Resource"}
        )

        # Make request
        response = client.get("/admin/subscriptions/123")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["id"] == "123"

    def test_admin_create_resource(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
    ):
        """Test admin creating a resource."""
        # Setup
        mock_supabase.table().insert().execute.return_value = Mock(
            data=[{"id": "new_123", "name": "New Resource"}]
        )

        # Make request
        response = client.post("/admin/test_resource", json={"name": "New Resource"})

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["id"] == "new_123"

    def test_admin_update_resource(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
    ):
        """Test admin updating a resource."""
        # Setup
        mock_supabase.table().update().eq().execute.return_value = Mock(
            data=[{"id": "123", "name": "Updated Resource"}]
        )

        # Make request
        response = client.put(
            "/admin/test_resource/123", json={"name": "Updated Resource"}
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["name"] == "Updated Resource"

    def test_admin_delete_resource(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
    ):
        """Test admin deleting a resource."""
        # Setup
        mock_supabase.table().delete().eq().execute.return_value = Mock(data=[])

        # Make request
        response = client.delete("/admin/test_resource/123")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["message"] == "Resource deleted successfully"

    def test_admin_dashboard_metrics(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
    ):
        """Test admin dashboard metrics."""
        # Setup
        mock_supabase.table().select().execute.side_effect = [
            Mock(data=[{"count": 100}]),  # total_users
            Mock(data=[{"count": 50}]),  # active_users_30d
            Mock(data=[{"count": 25}]),  # new_users_7d
            Mock(data=[{"count": 80}]),  # total_subscriptions
            Mock(data=[{"count": 70}]),  # active_subscriptions
            Mock(data=[{"count": 10}]),  # cancelled_subscriptions_30d
            Mock(data=[{"count": 60}]),  # total_instances
            Mock(data=[{"count": 45}]),  # running_instances
            Mock(data=[{"count": 15}]),  # stopped_instances
            Mock(data=[{"sum": 10000.0}]),  # revenue_mtd
            Mock(data=[{"sum": 50000.0}]),  # revenue_ytd
            Mock(data=[{"avg": 125.0}]),  # avg_revenue_per_user
        ]

        # Make request
        response = client.get("/admin/metrics/dashboard")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert "user_metrics" in data
        assert "subscription_metrics" in data
        assert "instance_metrics" in data
        assert "revenue_metrics" in data
        assert data["user_metrics"]["total_users"] == 100

    def test_admin_resource_not_in_allowlist(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
    ):
        """Test admin accessing resource not in allowlist."""
        # Make request to a resource not in ADMIN_RESOURCE_ALLOWLIST
        response = client.get("/admin/dangerous_resource")

        # Verify
        assert response.status_code == 403
        assert "not allowed" in response.json()["detail"]

    def test_admin_instance_not_found(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
        mock_check_deployment: Mock,
    ):
        """Test admin operations on non-existent instance."""
        # Setup
        mock_check_deployment.return_value = False

        # Make request
        response = client.post("/admin/instances/999/start")

        # Verify
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_admin_sync_instances_with_errors(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
        mock_provisioner_api_key,
    ):
        """Test admin sync with some errors."""
        with patch("backend.routes.provisioner.sync_instances") as mock_sync:
            mock_sync.return_value = {
                "total": 10,
                "synced": 7,
                "errors": 3,
                "updates": [
                    {
                        "instance_id": "123",
                        "old_status": "running",
                        "new_status": "stopped",
                        "reason": "status_mismatch",
                    }
                ],
            }

            # Make request
            response = client.post("/admin/sync-instances")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["errors"] == 3
            assert len(data["updates"]) == 1
