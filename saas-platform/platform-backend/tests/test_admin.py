"""Comprehensive HTTP API tests for admin endpoints."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import app
from tests.fake_supabase import FakeQuery, FakeResult, FakeSupabase


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
    def fake_db(self):
        """In-memory Supabase tables behind the admin routes."""
        db = FakeSupabase()
        with patch("backend.routes.admin.ensure_supabase", return_value=db):
            yield db

    @pytest.fixture
    def mock_verify_admin(self):
        """Mock admin verification."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_admin

        def override_verify_admin():
            return {"user_id": "admin_123", "email": "admin@example.com", "is_admin": True}

        app.dependency_overrides[verify_admin] = override_verify_admin
        yield
        app.dependency_overrides.clear()

    def test_admin_dashboard_metrics_unauthorized(self, client: TestClient):
        """Test accessing admin dashboard metrics without authorization."""
        from main import app  # noqa: PLC0415
        from backend.deps import verify_admin
        from fastapi import HTTPException

        def override_verify_admin():
            raise HTTPException(status_code=401, detail="Unauthorized")

        app.dependency_overrides[verify_admin] = override_verify_admin
        try:
            response = client.get("/admin/metrics/dashboard")
            assert response.status_code == 401
        finally:
            app.dependency_overrides.clear()

    def test_admin_start_instance(self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock):
        """Test admin starting an instance."""
        # Setup - mock the provisioner function
        with patch("backend.services.provisioner_service.start_instance") as mock_start:
            mock_start.return_value = {"success": True, "message": "Instance started"}

            # Make request
            response = client.post("/admin/instances/123/start")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert "started" in data["message"]
            mock_start.assert_called_once_with(123)

    def test_admin_start_instance_reaches_kubernetes(self, client: TestClient, mock_verify_admin: Mock):
        """Admin start should drive the service down to kubectl without any provisioner bearer token."""
        from unittest.mock import call  # noqa: PLC0415

        with (
            patch("backend.services.provisioner_service.check_deployment_exists", return_value=True),
            patch("backend.services.provisioner_service.run_kubectl") as mock_kubectl,
            patch("backend.services.provisioner_service.update_instance_status", return_value=True) as mock_update,
        ):
            mock_kubectl.return_value = (0, "scaled", "")

            response = client.post("/admin/instances/123/start")

        assert response.status_code == 200
        assert response.json()["success"] is True
        mock_kubectl.assert_has_awaits(
            [
                call(["scale", "deployment/tuwunel-123", "--replicas=1"], namespace="mindroom-instances"),
                call(["scale", "deployment/mindroom-123", "--replicas=1"], namespace="mindroom-instances"),
            ]
        )
        mock_update.assert_called_once_with(123, "running")

    def test_admin_stop_instance(self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock):
        """Test admin stopping an instance."""
        # Setup - mock the provisioner function
        with patch("backend.services.provisioner_service.stop_instance") as mock_stop:
            mock_stop.return_value = {"success": True, "message": "Instance stopped"}

            # Make request
            response = client.post("/admin/instances/456/stop")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert "stopped" in data["message"]
            mock_stop.assert_called_once_with(456)

    def test_admin_restart_instance(self, client: TestClient, mock_verify_admin: Mock):
        """Test admin restarting an instance."""
        # Setup - mock the provisioner function
        with patch("backend.services.provisioner_service.restart_instance") as mock_restart:
            mock_restart.return_value = {"success": True, "message": "Instance restarted"}

            # Make request
            response = client.post("/admin/instances/789/restart")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert "restarted" in data["message"]
            mock_restart.assert_called_once_with(789)

    def test_admin_uninstall_instance(self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock):
        """Test admin uninstalling an instance."""
        with patch("backend.services.provisioner_service.uninstall_instance") as mock_uninstall:
            mock_uninstall.return_value = {"success": True, "message": "Instance uninstalled"}

            # Make request
            response = client.delete("/admin/instances/123/uninstall")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert "uninstalled" in data["message"]
            mock_uninstall.assert_called_once_with(123)

    def test_admin_provision_instance(self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock):
        """Test admin provisioning an instance."""
        # Setup - Mock instance query
        mock_supabase.table().select().eq().execute.return_value = Mock(
            data=[
                {
                    "instance_id": "123",
                    "status": "deprovisioned",
                    "account_id": "acc_123",
                    "subscription_id": "sub_123",
                    "tier": "pro",
                }
            ]
        )

        # The subscription moved to byok since the instance was last deployed as pro.
        mock_supabase.table().select().eq().limit().execute.return_value = Mock(data=[{"tier": "byok"}])

        with patch("backend.services.provisioner_service.provision_instance") as mock_provision:
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
            assert mock_provision.call_args.kwargs["data"]["tier"] == "byok"

    def test_admin_sync_instances(self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock):
        """Test admin syncing instances."""
        with patch("backend.services.provisioner_service.sync_instances") as mock_sync:
            mock_sync.return_value = {"total": 5, "synced": 2, "errors": 0, "updates": []}

            # Make request (admin auth is mocked via fixture)
            response = client.post("/admin/sync-instances")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 5
            assert data["synced"] == 2

    def test_admin_get_account_details(self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock):
        """Test admin getting account details."""
        # Setup
        account_data = {
            "id": "acc_123",
            "email": "user@example.com",
            "status": "active",
            "created_at": datetime.now(UTC).isoformat(),
        }
        subscription_data = {"id": "sub_123", "account_id": "acc_123", "tier": "pro", "status": "active"}
        instance_data = {"id": "inst_123", "instance_id": "123", "account_id": "acc_123", "status": "running"}

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
        def table_side_effect(table_name):
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
        assert data["subscription"]["tier"] == "pro"
        assert len(data["instances"]) == 1

    def test_admin_update_account_status(self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock):
        """Test admin updating account status."""
        # Setup
        mock_supabase.table().update().eq().execute.return_value = Mock(data=[{"id": "acc_123", "status": "suspended"}])

        # Make request
        response = client.put(
            "/admin/accounts/acc_123/status", json={"status": "suspended", "reason": "Payment failed"}
        )

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["account_id"] == "acc_123"
        assert data["new_status"] == "suspended"

    def test_admin_refuses_to_activate_an_account_awaiting_deletion(
        self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock
    ):
        """Setting active would leave deleted_at set, so the admin is pointed at the owner's cancel-deletion."""
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[{"deleted_at": "2026-09-01T00:00:00Z"}])

        response = client.put("/admin/accounts/acc_123/status", json={"status": "active"})

        assert response.status_code == 409
        assert response.json()["detail"] == (
            "Account is awaiting deletion. Set its status to deleted so the owner can cancel the deletion."
        )
        mock_supabase.table().update.assert_not_called()

    def test_admin_activates_an_account_not_awaiting_deletion(
        self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock
    ):
        """Reactivating a suspended account still works."""
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[{"deleted_at": None}])
        mock_supabase.table().update().eq().execute.return_value = Mock(data=[{"id": "acc_123", "status": "active"}])

        response = client.put("/admin/accounts/acc_123/status", json={"status": "active"})

        assert response.status_code == 200
        assert response.json()["new_status"] == "active"

    def test_admin_status_change_for_an_unknown_account_is_not_found(
        self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock
    ):
        """A missing account answers 404 instead of being reported as a server error."""
        mock_supabase.table().update().eq().execute.return_value = Mock(data=[])

        response = client.put("/admin/accounts/acc_missing/status", json={"status": "suspended"})

        assert response.status_code == 404

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("PUT", "/admin/accounts/5F0B2C1E-8C3D-4F7A-9B21-6E4D3A2C1B0F/status", {"status": "suspended"}),
            ("PUT", "/admin/accounts/5F0B2C1E-8C3D-4F7A-9B21-6E4D3A2C1B0F", {"status": "suspended"}),
            ("DELETE", "/admin/accounts/5F0B2C1E-8C3D-4F7A-9B21-6E4D3A2C1B0F/complete", None),
        ],
    )
    def test_admin_account_changes_drop_cached_auth(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_verify_admin: Mock,
        method: str,
        path: str,
        body: dict | None,
    ):
        """Admin account changes take effect on the account's next request, whatever spelling the path id uses."""
        from backend.deps import AuthCacheEntry, _auth_cache  # noqa: PLC0415

        account_id = "5f0b2c1e-8c3d-4f7a-9b21-6e4d3a2c1b0f"
        mock_supabase.table().update().eq().execute.return_value = Mock(
            data=[{"id": account_id, "status": "suspended"}]
        )
        mock_supabase.table().select().eq().execute.return_value = Mock(data=[{"id": account_id, "email": "u@x.test"}])
        _auth_cache.clear()
        expires_at = datetime.now(UTC) + timedelta(minutes=5)
        for cached_id in (account_id, "acc_other"):
            _auth_cache[cached_id] = AuthCacheEntry(
                expires_at=expires_at,
                account_id=cached_id,
                user_data={"user_id": cached_id, "account_id": cached_id, "account": {"status": "active"}},
            )

        with (
            patch("backend.routes.admin.instances_data.get_instances_for_account", return_value=[]),
            patch("backend.routes.admin.instance_lifecycle.tear_down_account", new=AsyncMock()),
            patch("backend.routes.admin.instance_lifecycle.delete_auth_user", new=AsyncMock()),
        ):
            response = client.request(method, path, json=body)

        assert response.status_code == 200
        assert set(_auth_cache) == {"acc_other"}
        _auth_cache.clear()

    def test_admin_list_holds_every_row_newest_first(
        self, client: TestClient, fake_db: FakeSupabase, mock_verify_admin: Mock
    ):
        """A list without ``limit`` holds every row past PostgREST's row limit, newest first, ties in id order."""
        accounts = [{"id": f"acc_{day:02d}", "created_at": f"2026-10-{day:02d}T00:00:00Z"} for day in range(1, 13)]
        accounts.append({"id": "acc_00", "created_at": "2026-10-12T00:00:00Z"})
        fake_db.tables["accounts"] = accounts[1::2] + accounts[::2]
        fake_db.max_rows = 5
        response = client.get("/admin/accounts")

        assert response.status_code == 200
        newest_first = ["acc_00", *(f"acc_{day:02d}" for day in range(12, 0, -1))]
        assert [row["id"] for row in response.json()["data"]] == newest_first
        assert response.json()["total"] == 13

    def test_admin_list_survives_a_row_deleted_while_it_pages(
        self, client: TestClient, fake_db: FakeSupabase, mock_verify_admin: Mock
    ):
        """A row deleted between two pages of a full listing does not fail the listing."""
        fake_db.tables["accounts"] = [
            {"id": f"acc_{day}", "created_at": f"2026-10-0{day}T00:00:00Z"} for day in (1, 2, 3)
        ]
        fake_db.max_rows = 2
        serve = FakeQuery.execute

        def serve_then_delete_after_the_last_page_of_rows(query: FakeQuery) -> FakeResult:
            result = serve(query)
            if query.row_range and query.row_range[0] == 2:
                fake_db.tables["accounts"] = fake_db.tables["accounts"][1:]
            return result

        with patch.object(FakeQuery, "execute", serve_then_delete_after_the_last_page_of_rows):
            response = client.get("/admin/accounts")

        assert response.status_code == 200
        assert [row["id"] for row in response.json()["data"]] == ["acc_3", "acc_2", "acc_1"]

    def test_admin_list_limit_holds_the_newest_rows_and_counts_every_row(
        self, client: TestClient, fake_db: FakeSupabase, mock_verify_admin: Mock
    ):
        """A list with ``limit`` holds that many of the newest rows, and total counts every row."""
        fake_db.tables["audit_logs"] = [
            {"id": f"log_{day}", "created_at": f"2026-10-0{day}T00:00:00Z"} for day in (4, 1, 5, 3, 2)
        ]
        response = client.get("/admin/audit_logs?limit=2")

        assert response.status_code == 200
        assert [row["id"] for row in response.json()["data"]] == ["log_5", "log_4"]
        assert response.json()["total"] == 5

    def test_admin_subscription_and_instance_lists_show_the_account(
        self, client: TestClient, fake_db: FakeSupabase, mock_verify_admin: Mock
    ):
        """Subscription and instance rows carry their account, which the admin pages show as the customer."""
        fake_db.tables |= {
            "accounts": [{"id": "acc_1", "email": "owner@example.com", "full_name": "Owner"}],
            "subscriptions": [{"id": "sub_1", "account_id": "acc_1", "created_at": "2026-10-01T00:00:00Z"}],
            "instances": [{"id": "inst_1", "account_id": "acc_1", "created_at": "2026-10-01T00:00:00Z"}],
        }
        listed = {resource: client.get(f"/admin/{resource}").json() for resource in ("subscriptions", "instances")}

        assert {resource: body["data"][0]["accounts"]["email"] for resource, body in listed.items()} == {
            "subscriptions": "owner@example.com",
            "instances": "owner@example.com",
        }

    def test_admin_list_failure_is_an_error_not_an_empty_list(self, mock_supabase: MagicMock, mock_verify_admin: Mock):
        """A failed listing answers 500, not a 200 with no rows."""
        mock_supabase.table.side_effect = RuntimeError("database unavailable")
        response = TestClient(app, raise_server_exceptions=False).get("/admin/accounts")

        assert response.status_code == 500

    def test_admin_get_single_resource(self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock):
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

    def test_admin_create_resource(self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock):
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

    def test_admin_update_resource(self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock):
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

    def test_admin_delete_resource(self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock):
        """Test admin deleting a resource."""
        # Setup
        mock_supabase.table().delete().eq().execute.return_value = Mock(data=[])

        # Make request to an allowed resource
        response = client.delete("/admin/instances/123")

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert "data" in data

    def test_admin_dashboard_metrics_cover_every_row(
        self, client: TestClient, fake_db: FakeSupabase, mock_verify_admin: Mock
    ):
        """Every dashboard figure counts all rows, past PostgREST's per-request row cap."""
        fake_db.max_rows = 2
        fake_db.tables = {
            "accounts": [{"id": f"account-{i}"} for i in range(5)],
            "subscriptions": [
                # MRR counts each subscription's recorded Stripe price per month, and nothing before a price is recorded.
                {"id": "sub-1", "status": "active", "tier": "byok", "billing_cycle": "monthly", "price_cents": 1000},
                {"id": "sub-2", "status": "active", "tier": "byok", "billing_cycle": "yearly", "price_cents": 9600},
                {"id": "sub-3", "status": "active", "tier": "hobby", "billing_cycle": "yearly", "price_cents": 19990},
                {"id": "sub-4", "status": "active", "tier": "pro", "billing_cycle": None, "price_cents": None},
                {"id": "sub-5", "status": "active", "tier": "free", "billing_cycle": "monthly", "price_cents": 0},
                {"id": "sub-6", "status": "cancelled", "tier": "pro", "billing_cycle": "monthly", "price_cents": 5000},
            ],
            "instances": [
                {"instance_id": i, "status": status}
                for i, status in enumerate(["running", "running", "running", "stopped"])
            ],
        }

        response = client.get("/admin/metrics/dashboard")

        assert response.status_code == 200
        data = response.json()
        assert data["total_accounts"] == 5
        assert data["active_subscriptions"] == 4
        assert data["subscription_revenue"] == 35.0  # 10 + 8 + 16.66 (199.90 / 12), rounded to whole dollars
        assert data["instances_by_status"] == {"running": 3, "stopped": 1}

    def test_admin_dashboard_metrics_fail_instead_of_reporting_zeros(
        self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock
    ):
        """A database failure is an error response, not a dashboard of zero figures."""
        mock_supabase.table.side_effect = RuntimeError("database unavailable")

        response = client.get("/admin/metrics/dashboard")

        assert response.status_code == 500
        assert response.json() == {"detail": "Failed to fetch dashboard metrics"}

    def test_admin_resource_not_in_allowlist(self, client: TestClient, mock_verify_admin: Mock):
        """Test admin accessing resource not in allowlist."""
        # Make request to a resource not in ADMIN_RESOURCE_ALLOWLIST
        response = client.get("/admin/dangerous_resource")

        # Verify - the route returns 400 for invalid resource
        assert response.status_code == 400
        assert "Invalid resource" in response.json()["detail"]

    def test_admin_instance_not_found(self, client: TestClient, mock_verify_admin: Mock):
        """Test admin operations on non-existent instance."""
        # Setup - mock the provisioner service start function to raise 404
        with patch("backend.services.provisioner_service.start_instance") as mock_start:
            mock_start.side_effect = HTTPException(status_code=404, detail="Deployment not found")

            # Make request (admin auth is mocked)
            response = client.post("/admin/instances/999/start")

            # Verify
            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()

    def test_admin_sync_instances_with_errors(
        self, client: TestClient, mock_supabase: MagicMock, mock_verify_admin: Mock
    ):
        """Test admin sync with some errors."""
        with patch("backend.services.provisioner_service.sync_instances") as mock_sync:
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

            # Make request (admin auth is mocked via fixture)
            response = client.post("/admin/sync-instances")

            # Verify
            assert response.status_code == 200
            data = response.json()
            assert data["errors"] == 3
            assert len(data["updates"]) == 1
