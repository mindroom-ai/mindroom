"""Comprehensive HTTP API tests for provisioner endpoints."""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient


class TestProvisionerEndpoints:
    """Test provisioner endpoints via HTTP API."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    @pytest.fixture
    def mock_supabase(self):
        """Mock Supabase client."""
        with patch("backend.routes.provisioner.ensure_supabase") as mock:
            sb = MagicMock()
            mock.return_value = sb
            yield sb

    @pytest.fixture
    def mock_kubectl(self):
        """Mock kubectl commands."""
        with patch("backend.routes.provisioner.run_kubectl") as mock:
            mock.return_value = (0, "Success", "")  # Default success response
            yield mock

    @pytest.fixture
    def mock_helm(self):
        """Mock helm commands."""
        with patch("backend.routes.provisioner.run_helm") as mock:
            mock.return_value = (0, "Success", "")  # Default success response
            yield mock

    @pytest.fixture
    def mock_check_deployment(self):
        """Mock deployment existence check."""
        with patch("backend.routes.provisioner.check_deployment_exists") as mock:
            mock.return_value = True  # Default exists
            yield mock

    @pytest.fixture
    def mock_wait_for_deployment(self):
        """Mock deployment readiness check."""
        with patch("backend.routes.provisioner.wait_for_deployment_ready") as mock:
            mock.return_value = True  # Default ready
            yield mock

    @pytest.fixture
    def mock_update_status(self):
        """Mock update instance status."""
        with patch("backend.routes.provisioner.update_instance_status") as mock:
            mock.return_value = True  # Default success
            yield mock

    @pytest.fixture
    def mock_ensure_secret(self):
        """Mock docker registry secret creation."""
        with patch("backend.routes.provisioner.ensure_docker_registry_secret") as mock:
            mock.return_value = True  # Default success
            yield mock

    @pytest.fixture
    def valid_auth_header(self):
        """Get valid authorization header."""
        # Need to patch where it's used, not where it's defined
        with patch("backend.routes.provisioner.PROVISIONER_API_KEY", "test-api-key"):
            yield {"Authorization": "Bearer test-api-key"}

    @pytest.fixture
    def mock_config(self):
        """Mock configuration values."""
        with patch.multiple(
            "backend.routes.provisioner",
            PLATFORM_DOMAIN="mindroom.test",
            SUPABASE_URL="https://supabase.test",
            SUPABASE_ANON_KEY="test-anon-key",
            OPENROUTER_API_KEY="test-openrouter",
            OPENAI_API_KEY="test-openai",
            GITEA_USER="test-user",
            GITEA_TOKEN="test-token",
        ):
            yield

    def test_provision_unauthorized(self, client: TestClient):
        """Test provision endpoint without authorization."""
        response = client.post("/system/provision", json={})
        assert response.status_code == 401
        assert response.json()["detail"] == "Unauthorized"

    def test_provision_invalid_auth(self, client: TestClient):
        """Test provision endpoint with invalid authorization."""
        with patch("backend.routes.provisioner.PROVISIONER_API_KEY", "real-key"):
            response = client.post(
                "/system/provision",
                json={},
                headers={"Authorization": "Bearer wrong-key"},
            )
            assert response.status_code == 401
            assert response.json()["detail"] == "Unauthorized"

    def test_provision_new_instance_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        mock_wait_for_deployment: AsyncMock,
        mock_ensure_secret: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """Test successful provisioning of a new instance."""
        # Setup
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "123"}])
        mock_supabase.table().update().eq().execute.return_value = Mock()

        provision_data = {
            "subscription_id": "sub_test_123",
            "account_id": "acc_test_123",
            "tier": "starter",
        }

        # Make request
        response = client.post("/system/provision", json=provision_data, headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["customer_id"] == "123"
        assert data["frontend_url"] == "https://123.mindroom.test"
        assert data["api_url"] == "https://123.api.mindroom.test"
        assert data["matrix_url"] == "https://123.matrix.mindroom.test"
        assert "provisioned successfully" in data["message"]

        # Verify calls
        mock_kubectl.assert_called()
        mock_helm.assert_called()
        mock_ensure_secret.assert_called()

    def test_provision_re_provision_existing(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        mock_wait_for_deployment: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """Test re-provisioning an existing instance."""
        # Setup
        mock_supabase.table().update().eq().execute.return_value = Mock(data=[{"instance_id": "456"}])

        provision_data = {
            "subscription_id": "sub_test_123",
            "account_id": "acc_test_123",
            "tier": "professional",
            "instance_id": "456",  # Existing instance
        }

        # Make request
        response = client.post("/system/provision", json=provision_data, headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["customer_id"] == "456"

    def test_provision_instance_not_found_for_reprovision(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        valid_auth_header: dict,
    ):
        """Test re-provisioning with non-existent instance."""
        # Setup
        mock_supabase.table().update().eq().execute.return_value = Mock(data=[])

        provision_data = {"instance_id": "999"}  # Non-existent

        # Make request
        response = client.post("/system/provision", json=provision_data, headers=valid_auth_header)

        # Verify
        assert response.status_code == 404
        assert response.json()["detail"] == "Instance 999 not found"

    def test_provision_kubectl_not_found(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        valid_auth_header: dict,
    ):
        """Test provisioning when kubectl is not available."""
        # Setup
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "123"}])
        mock_kubectl.side_effect = FileNotFoundError()

        provision_data = {
            "subscription_id": "sub_test_123",
            "account_id": "acc_test_123",
        }

        # Make request
        response = client.post("/system/provision", json=provision_data, headers=valid_auth_header)

        # Verify
        assert response.status_code == 503
        assert "Kubectl command not found" in response.json()["detail"]

    def test_provision_helm_failure(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """Test provisioning when helm deployment fails."""
        # Setup
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "123"}])
        mock_helm.return_value = (1, "", "Helm error")  # Failure

        provision_data = {
            "subscription_id": "sub_test_123",
            "account_id": "acc_test_123",
        }

        # Make request
        response = client.post("/system/provision", json=provision_data, headers=valid_auth_header)

        # Verify
        assert response.status_code == 500
        assert "Helm install failed" in response.json()["detail"]

    def test_provision_not_ready_with_background_task(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        mock_wait_for_deployment: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """Test provisioning when deployment is not immediately ready."""
        # Setup
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "123"}])
        mock_wait_for_deployment.return_value = False  # Not ready

        provision_data = {
            "subscription_id": "sub_test_123",
            "account_id": "acc_test_123",
        }

        # Make request
        response = client.post("/system/provision", json=provision_data, headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "getting ready" in data["message"]

    def test_start_instance_success(
        self,
        client: TestClient,
        mock_kubectl: AsyncMock,
        mock_check_deployment: AsyncMock,
        mock_update_status: Mock,
        valid_auth_header: dict,
    ):
        """Test starting an instance successfully."""
        # Make request
        response = client.post("/system/instances/123/start", headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "started successfully" in data["message"]

        # Verify kubectl was called with scale command
        mock_kubectl.assert_called_with(
            ["scale", "deployment/mindroom-backend-123", "--replicas=1"],
            namespace="mindroom-instances",
        )
        mock_update_status.assert_called_with(123, "running")

    def test_start_instance_not_found(
        self,
        client: TestClient,
        mock_check_deployment: AsyncMock,
        valid_auth_header: dict,
    ):
        """Test starting a non-existent instance."""
        # Setup
        mock_check_deployment.return_value = False

        # Make request
        response = client.post("/system/instances/999/start", headers=valid_auth_header)

        # Verify
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_start_instance_kubectl_failure(
        self,
        client: TestClient,
        mock_kubectl: AsyncMock,
        mock_check_deployment: AsyncMock,
        valid_auth_header: dict,
    ):
        """Test starting instance when kubectl fails."""
        # Setup
        mock_kubectl.return_value = (1, "", "kubectl error")

        # Make request
        response = client.post("/system/instances/123/start", headers=valid_auth_header)

        # Verify
        assert response.status_code == 500
        assert "Failed to start instance" in response.json()["detail"]

    def test_stop_instance_success(
        self,
        client: TestClient,
        mock_kubectl: AsyncMock,
        mock_check_deployment: AsyncMock,
        mock_update_status: Mock,
        valid_auth_header: dict,
    ):
        """Test stopping an instance successfully."""
        # Make request
        response = client.post("/system/instances/123/stop", headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "stopped successfully" in data["message"]

        # Verify kubectl was called with scale command
        mock_kubectl.assert_called_with(
            ["scale", "deployment/mindroom-backend-123", "--replicas=0"],
            namespace="mindroom-instances",
        )
        mock_update_status.assert_called_with(123, "stopped")

    def test_stop_instance_not_found(
        self,
        client: TestClient,
        mock_check_deployment: AsyncMock,
        valid_auth_header: dict,
    ):
        """Test stopping a non-existent instance."""
        # Setup
        mock_check_deployment.return_value = False

        # Make request
        response = client.post("/system/instances/999/stop", headers=valid_auth_header)

        # Verify
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_restart_instance_success(
        self,
        client: TestClient,
        mock_kubectl: AsyncMock,
        mock_check_deployment: AsyncMock,
        valid_auth_header: dict,
    ):
        """Test restarting an instance successfully."""
        # Make request
        response = client.post("/system/instances/123/restart", headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "restarted successfully" in data["message"]

        # Verify kubectl was called with rollout restart command
        mock_kubectl.assert_called_with(
            ["rollout", "restart", "deployment/mindroom-backend-123"],
            namespace="mindroom-instances",
        )

    def test_restart_instance_not_found(
        self,
        client: TestClient,
        mock_check_deployment: AsyncMock,
        valid_auth_header: dict,
    ):
        """Test restarting a non-existent instance."""
        # Setup
        mock_check_deployment.return_value = False

        # Make request
        response = client.post("/system/instances/999/restart", headers=valid_auth_header)

        # Verify
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_uninstall_instance_success(
        self,
        client: TestClient,
        mock_helm: AsyncMock,
        mock_update_status: Mock,
        valid_auth_header: dict,
    ):
        """Test uninstalling an instance successfully."""
        # Make request
        response = client.delete("/system/instances/123/uninstall", headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "uninstalled successfully" in data["message"]
        # Note: instance_id is not in ActionResult model, so it won't be in response

        # Verify helm was called
        mock_helm.assert_called_with(["uninstall", "instance-123", "--namespace=mindroom-instances"])
        mock_update_status.assert_called_with(123, "deprovisioned")

    def test_uninstall_instance_already_uninstalled(
        self,
        client: TestClient,
        mock_helm: AsyncMock,
        mock_update_status: Mock,
        valid_auth_header: dict,
    ):
        """Test uninstalling an already uninstalled instance."""
        # Setup
        mock_helm.return_value = (1, "", "release not found")

        # Make request
        response = client.delete("/system/instances/123/uninstall", headers=valid_auth_header)

        # Verify - should succeed
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_uninstall_instance_failure(
        self,
        client: TestClient,
        mock_helm: AsyncMock,
        valid_auth_header: dict,
    ):
        """Test uninstalling when helm fails."""
        # Setup
        mock_helm.return_value = (1, "", "some other error")

        # Make request
        response = client.delete("/system/instances/123/uninstall", headers=valid_auth_header)

        # Verify
        assert response.status_code == 500
        assert "Failed to uninstall instance" in response.json()["detail"]

    def test_sync_instances_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_check_deployment: AsyncMock,
        valid_auth_header: dict,
    ):
        """Test syncing instances successfully."""
        # Setup
        mock_supabase.table().select().execute.return_value = Mock(
            data=[
                {"id": 1, "instance_id": "123", "status": "running"},
                {"id": 2, "instance_id": "456", "status": "stopped"},
                {"id": 3, "instance_id": "789", "status": "running"},
            ]
        )

        # Mock deployment checks
        async def check_deployment_side_effect(instance_id):
            return instance_id != "789"  # 789 doesn't exist

        mock_check_deployment.side_effect = check_deployment_side_effect

        # Mock kubectl responses for replica checks
        async def kubectl_side_effect(cmd, namespace=None):
            if "123" in str(cmd):
                return (0, "1", "")  # 1 replica = running
            elif "456" in str(cmd):
                return (0, "0", "")  # 0 replicas = stopped
            return (1, "", "error")

        mock_kubectl.side_effect = kubectl_side_effect

        # Make request
        response = client.post("/system/sync-instances", headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert data["synced"] == 1  # Instance 789 marked as error
        assert data["errors"] == 0
        assert len(data["updates"]) == 1

        # Verify update for missing instance
        update = data["updates"][0]
        assert update["instance_id"] == "789"
        assert update["old_status"] == "running"
        assert update["new_status"] == "error"
        assert update["reason"] == "deployment_not_found"

    def test_sync_instances_status_mismatch(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_check_deployment: AsyncMock,
        valid_auth_header: dict,
    ):
        """Test syncing instances with status mismatches."""
        # Setup
        mock_supabase.table().select().execute.return_value = Mock(
            data=[
                {
                    "id": 1,
                    "instance_id": "123",
                    "status": "stopped",
                },  # Actually running
            ]
        )

        mock_check_deployment.return_value = True
        mock_kubectl.return_value = (0, "1", "")  # 1 replica = running

        # Make request
        response = client.post("/system/sync-instances", headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["synced"] == 1
        assert len(data["updates"]) == 1

        # Verify update
        update = data["updates"][0]
        assert update["instance_id"] == "123"
        assert update["old_status"] == "stopped"
        assert update["new_status"] == "running"
        assert update["reason"] == "status_mismatch"

    def test_sync_instances_no_instances(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        valid_auth_header: dict,
    ):
        """Test syncing with no instances."""
        # Setup
        mock_supabase.table().select().execute.return_value = Mock(data=[])

        # Make request
        response = client.post("/system/sync-instances", headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["synced"] == 0
        assert data["errors"] == 0
        assert data["updates"] == []

    def test_sync_instances_missing_instance_id(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        valid_auth_header: dict,
    ):
        """Test syncing with instance missing instance_id."""
        # Setup
        mock_supabase.table().select().execute.return_value = Mock(
            data=[
                {"id": 1, "status": "running"},  # No instance_id or subdomain
            ]
        )

        # Make request
        response = client.post("/system/sync-instances", headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["errors"] == 1
        assert data["synced"] == 0

    def test_sync_instances_kubectl_error(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_check_deployment: AsyncMock,
        valid_auth_header: dict,
    ):
        """Test syncing when kubectl check fails."""
        # Setup
        mock_supabase.table().select().execute.return_value = Mock(
            data=[
                {"id": 1, "instance_id": "123", "status": "running"},
            ]
        )

        mock_check_deployment.return_value = True
        mock_kubectl.side_effect = Exception("kubectl error")

        # Make request
        response = client.post("/system/sync-instances", headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["errors"] == 1

    def test_rate_limiting(self, client: TestClient, valid_auth_header: dict):
        """Test rate limiting on provisioner endpoints."""
        # The provision endpoint has a limit of 5/minute
        responses = []
        for _ in range(7):
            response = client.post("/system/provision", json={}, headers=valid_auth_header)
            responses.append(response.status_code)

        # At least one should be rate limited (429)
        assert 429 in responses

    def test_provision_database_insert_failure(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        valid_auth_header: dict,
    ):
        """Test provisioning when database insert fails."""
        # Setup
        mock_supabase.table().insert().execute.return_value = Mock(data=[])

        provision_data = {
            "subscription_id": "sub_test_123",
            "account_id": "acc_test_123",
        }

        # Make request
        response = client.post("/system/provision", json=provision_data, headers=valid_auth_header)

        # Verify
        assert response.status_code == 500
        assert "Failed to insert instance" in response.json()["detail"]

    def test_provision_with_free_tier(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        mock_wait_for_deployment: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """Test provisioning with free tier."""
        # Setup
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "123"}])

        provision_data = {
            "subscription_id": None,  # Free tier might not have subscription
            "account_id": "acc_test_123",
            "tier": "free",
        }

        # Make request
        response = client.post("/system/provision", json=provision_data, headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["customer_id"] == "123"
