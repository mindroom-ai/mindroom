"""Comprehensive HTTP API tests for admin endpoints."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi import HTTPException
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
        with (
            patch("backend.routes.admin.PROVISIONER_API_KEY", "test-api-key"),
            patch("backend.routes.provisioner.PROVISIONER_API_KEY", "test-api-key"),
        ):
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
        accounts_mock.execute.return_value = Mock(data=[{}, {}, {}, {}, {}, {}, {}, {}, {}, {}])  # 10 accounts

        subscriptions_mock = MagicMock()
        subscriptions_mock.select.return_value = subscriptions_mock
        subscriptions_mock.eq.return_value = subscriptions_mock
        subscriptions_mock.execute.return_value = Mock(data=[{}, {}, {}, {}, {}, {}, {}, {}])  # 8 active

        instances_mock = MagicMock()
        instances_mock.select.return_value = instances_mock
        instances_mock.eq.return_value = instances_mock
        instances_mock.execute.return_value = Mock(data=[{}, {}, {}, {}, {}, {}, {}])  # 7 running

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
        # Setup - Mock instance query
        mock_supabase.table().select().eq().execute.return_value = Mock(
            data=[
                {
                    "instance_id": "123",
                    "status": "deprovisioned",
                    "account_id": "acc_123",
                }
            ]
        )

        # Mock subscription query for provision_instance
        mock_supabase.table().select().eq().single().execute.return_value = Mock(
            data={"id": "sub_123", "account_id": "acc_123", "tier": "starter"}
        )

        with patch("backend.routes.admin.provision_instance") as mock_provision:
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
        with patch("backend.routes.admin.sync_instances") as mock_sync:

            async def mock_sync_func(request, auth):
                return {
                    "total": 5,
                    "synced": 2,
                    "errors": 0,
                    "updates": [],
                }

            mock_sync.side_effect = mock_sync_func

            # Make request (admin auth is mocked via fixture)
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

        # Setup mocks for different queries
        # Account query (single)
        account_mock = MagicMock()
        account_mock.select.return_value = account_mock
        account_mock.eq.return_value = account_mock
        account_mock.single.return_value = account_mock
        account_mock.execute.return_value = Mock(data=account_data)

        # Subscription query (with ordering)
        subscription_mock = MagicMock()
        subscription_mock.select.return_value = subscription_mock
        subscription_mock.eq.return_value = subscription_mock
        subscription_mock.order.return_value = subscription_mock
        subscription_mock.limit.return_value = subscription_mock
        subscription_mock.execute.return_value = Mock(data=[subscription_data])

        # Instances query (with ordering)
        instances_mock = MagicMock()
        instances_mock.select.return_value = instances_mock
        instances_mock.eq.return_value = instances_mock
        instances_mock.order.return_value = instances_mock
        instances_mock.execute.return_value = Mock(data=[instance_data])

        # Configure table calls
        call_count = [0]

        def table_side_effect(table_name):
            call_count[0] += 1
            if table_name == "accounts":
                return account_mock
            elif table_name == "subscriptions":
                return subscription_mock
            elif table_name == "instances":
                return instances_mock
            return MagicMock()

        mock_supabase.table = Mock(side_effect=table_side_effect)

        # Make request
        response = client.get("/admin/accounts/acc_123")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["account"]["id"] == "acc_123"
        assert data["account"]["email"] == "user@example.com"
        assert data["subscription"]["tier"] == "professional"
        assert len(data["instances"]) == 1

    def test_admin_update_account_status(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
    ):
        """Test admin updating account status."""
        # Setup
        mock_supabase.table().update().eq().execute.return_value = Mock(data=[{"id": "acc_123", "status": "suspended"}])

        # Make request
        response = client.put(
            "/admin/accounts/acc_123/status",
            json={"status": "suspended", "reason": "Payment failed"},
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["account_id"] == "acc_123"
        assert data["new_status"] == "suspended"

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
        # Setup mock with query chaining for accounts
        mock_query = MagicMock()
        mock_query.select.return_value = mock_query
        mock_query.range.return_value = mock_query
        mock_query.execute.return_value = Mock(
            data=[
                {"id": "1", "name": "Resource 1"},
                {"id": "2", "name": "Resource 2"},
            ],
            count=2,
        )
        mock_supabase.table.return_value = mock_query

        # Make request
        response = client.get("/admin/accounts")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert "total" in data
        assert len(data["data"]) == 2
        assert data["total"] == 2

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
        assert "data" in data
        assert data["data"]["id"] == "123"

    def test_admin_create_resource(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
    ):
        """Test admin creating a resource."""
        # Setup
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"id": "new_123", "name": "New Resource"}])

        # Make request to an allowed resource
        response = client.post("/admin/accounts", json={"name": "New Resource"})

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
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

        # Make request to an allowed resource
        response = client.put("/admin/subscriptions/123", json={"name": "Updated Resource"})

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
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

        # Make request to an allowed resource
        response = client.delete("/admin/instances/123")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert "data" in data

    def test_admin_dashboard_metrics(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
    ):
        """Test admin dashboard metrics."""
        # Setup mock queries for each specific table call
        # Mock accounts query
        accounts_mock = MagicMock()
        accounts_mock.select = MagicMock(return_value=accounts_mock)
        accounts_result = Mock()
        accounts_result.count = 100
        accounts_mock.execute.return_value = accounts_result

        # Mock active subscriptions query
        active_subs_mock = MagicMock()
        active_subs_mock.select = MagicMock(return_value=active_subs_mock)
        active_subs_mock.eq.return_value = active_subs_mock
        active_subs_result = Mock()
        active_subs_result.count = 70
        active_subs_mock.execute.return_value = active_subs_result

        # Mock running instances query
        running_instances_mock = MagicMock()
        running_instances_mock.select = MagicMock(return_value=running_instances_mock)
        running_instances_mock.eq.return_value = running_instances_mock
        running_instances_result = Mock()
        running_instances_result.count = 45
        running_instances_mock.execute.return_value = running_instances_result

        # Mock subscriptions data for MRR
        subs_data_mock = MagicMock()
        subs_data_mock.select.return_value = subs_data_mock
        subs_data_mock.eq.return_value = subs_data_mock
        subs_data_mock.execute.return_value = Mock(
            data=[
                {"tier": "starter"},
                {"tier": "professional"},
            ]
        )

        # Mock usage metrics for messages
        usage_mock = MagicMock()
        usage_mock.select.return_value = usage_mock
        usage_mock.gte.return_value = usage_mock
        usage_mock.order.return_value = usage_mock
        usage_mock.execute.return_value = Mock(data=[])

        # Mock all instances for status counts
        all_instances_mock = MagicMock()
        all_instances_mock.select.return_value = all_instances_mock
        all_instances_mock.execute.return_value = Mock(
            data=[
                {"status": "running"},
                {"status": "stopped"},
            ]
        )

        # Mock audit logs
        audit_mock = MagicMock()
        audit_mock.select.return_value = audit_mock
        audit_mock.order.return_value = audit_mock
        audit_mock.limit.return_value = audit_mock
        audit_mock.execute.return_value = Mock(data=[])

        # Set up table method to return the right mock for each table
        subscription_calls = [0]

        def table_side_effect(table_name):
            if table_name == "accounts":
                return accounts_mock
            elif table_name == "subscriptions":
                # Track subscription calls to distinguish between different queries
                subscription_calls[0] += 1
                if subscription_calls[0] == 1:  # First call is for active subs count
                    return active_subs_mock
                else:  # Second call is for tier data
                    return subs_data_mock
            elif table_name == "instances":
                # For instances, we need to return different mocks based on what's being queried
                # First call has .eq("status", "running"), second doesn't
                mock = MagicMock()
                # Create a mock that can handle the chained calls
                mock.select = MagicMock(return_value=mock)
                mock.eq = MagicMock(return_value=mock)

                # Determine which result to return based on whether eq() was called
                def execute_side_effect():
                    # If eq was called with "status", "running", return running instances
                    if (
                        mock.eq.called
                        and mock.eq.call_args
                        and len(mock.eq.call_args[0]) > 1
                        and mock.eq.call_args[0][1] == "running"
                    ):
                        result = Mock()
                        result.count = 45
                        return result
                    else:
                        # Otherwise return all instances
                        return Mock(
                            data=[
                                {"status": "running"},
                                {"status": "stopped"},
                            ]
                        )

                mock.execute = MagicMock(side_effect=execute_side_effect)
                return mock
            elif table_name == "usage_metrics":
                return usage_mock
            elif table_name == "audit_logs":
                return audit_mock
            return MagicMock()

        mock_supabase.table = Mock(side_effect=table_side_effect)

        # Make request
        response = client.get("/admin/metrics/dashboard")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert "total_accounts" in data
        assert "active_subscriptions" in data
        assert "total_instances" in data
        assert "subscription_revenue" in data
        assert data["total_accounts"] == 100
        assert data["active_subscriptions"] == 70
        assert data["total_instances"] == 2  # We have 2 instances total in the mock

    def test_admin_resource_not_in_allowlist(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
    ):
        """Test admin accessing resource not in allowlist."""
        # Make request to a resource not in ADMIN_RESOURCE_ALLOWLIST
        response = client.get("/admin/dangerous_resource")

        # Verify - the route returns 400 for invalid resource
        assert response.status_code == 400
        assert "Invalid resource" in response.json()["detail"]

    def test_admin_instance_not_found(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
        mock_provisioner_api_key,
    ):
        """Test admin operations on non-existent instance."""
        # Setup - mock the start_instance_provisioner function to raise 404
        with patch("backend.routes.admin.start_instance_provisioner") as mock_start:

            async def mock_start_func(request, instance_id, auth):
                raise HTTPException(status_code=404, detail="Deployment not found")

            mock_start.side_effect = mock_start_func

            # Make request (admin auth is mocked)
            response = client.post("/admin/instances/999/start")

            # Verify
            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()

    def test_admin_sync_instances_with_errors(
        self,
        client: TestClient,
        mock_verify_admin: Mock,
        mock_provisioner_api_key,
    ):
        """Test admin sync with some errors."""
        with patch("backend.routes.admin.sync_instances") as mock_sync:

            async def mock_sync_func(request, auth):
                return {
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

            mock_sync.side_effect = mock_sync_func

            # Make request (admin auth is mocked via fixture)
            response = client.post("/admin/sync-instances")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["errors"] == 3
            assert len(data["updates"]) == 1
