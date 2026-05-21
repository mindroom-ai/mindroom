"""Comprehensive HTTP API tests for provisioner endpoints."""

import base64
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import pytest
from backend.openrouter import CreatedOpenRouterKey
from fastapi.testclient import TestClient


def _helm_set_args(helm_args: list[str]) -> dict[str, str]:
    """Return Helm --set key/value pairs from a captured command."""
    set_args = {}
    for i, arg in enumerate(helm_args):
        if arg == "--set":
            key, value = helm_args[i + 1].split("=", 1)
            set_args[key] = value
    return set_args


def _helm_set_file_args(helm_args: list[str]) -> dict[str, str]:
    """Return Helm --set-file key/path pairs from a captured command."""
    set_file_args = {}
    for i, arg in enumerate(helm_args):
        if arg == "--set-file":
            key, path = helm_args[i + 1].split("=", 1)
            set_file_args[key] = path
    return set_file_args


def _helm_set_string_args(helm_args: list[str]) -> dict[str, str]:
    """Return Helm --set-string key/value pairs from a captured command."""
    set_string_args = {}
    for i, arg in enumerate(helm_args):
        if arg == "--set-string":
            key, value = helm_args[i + 1].split("=", 1)
            set_string_args[key] = value
    return set_string_args


def _assert_helm_uses_external_instance_secret(helm_args: list[str]) -> None:
    """Assert Helm only receives the external instance Secret reference."""
    set_args = _helm_set_args(helm_args)
    set_file_args = _helm_set_file_args(helm_args)
    set_string_args = _helm_set_string_args(helm_args)

    assert set_args["instanceSecrets.create"] == "false"
    assert set_args["instanceSecrets.name"].startswith("mindroom-api-keys-")
    assert "instanceSecrets.hash" in set_string_args
    assert "credentials_encryption_key" not in set_args
    assert "credentials_encryption_key" not in set_file_args


def test_owner_matrix_user_id_from_email_matches_synapse_oidc_template() -> None:
    """Hosted owner MXIDs should match Synapse's OIDC email localpart mapping."""
    from backend.routes.provisioner import _owner_matrix_user_id_from_email

    assert (
        _owner_matrix_user_id_from_email(
            "Owner.User+Test@Example.COM",
            instance_id="42",
            base_domain="mindroom.chat",
        )
        == "@owner.user+test:42.mindroom.chat"
    )
    assert (
        _owner_matrix_user_id_from_email(
            "_Owner=Test@example.com",
            instance_id="42",
            base_domain="mindroom.chat",
        )
        == "@=5fowner=3dtest:42.mindroom.chat"
    )


async def _kubectl_without_credentials_encryption_secret(args: list[str], namespace: str | None = None):
    """Return an empty response for a missing existing instance API key Secret."""
    _ = namespace
    if args[:2] == ["get", "pvc"]:
        return (0, '{"items":[]}', "")
    if args[:3] == ["get", "secret", "mindroom-api-keys-456"]:
        assert "--ignore-not-found" in args
        return (0, "", "")
    return (0, "Success", "")


async def _kubectl_with_credentials_encryption_secret(args: list[str], namespace: str | None = None):
    """Return a non-empty existing credential encryption key for the instance API key Secret."""
    _ = namespace
    if args[:2] == ["get", "pvc"]:
        return (0, '{"items":[]}', "")
    if args[:3] == ["get", "secret", "mindroom-api-keys-456"]:
        assert "--ignore-not-found" in args
        existing_key = base64.urlsafe_b64encode(b"1" * 32).decode("ascii").rstrip("=")
        return (0, base64.b64encode(existing_key.encode("utf-8")).decode("ascii"), "")
    return (0, "Success", "")


async def _kubectl_credentials_encryption_secret_lookup_fails(args: list[str], namespace: str | None = None):
    """Return a real kubectl failure for the existing instance API key Secret lookup."""
    _ = namespace
    if args[:2] == ["get", "pvc"]:
        return (0, '{"items":[]}', "")
    if args[:3] == ["get", "secret", "mindroom-api-keys-456"]:
        assert "--ignore-not-found" in args
        return (1, "", "Forbidden")
    return (0, "Success", "")


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
            INSTANCE_BASE_DOMAIN="mindroom.test",
            PLATFORM_DOMAIN="mindroom.test",
            SUPABASE_URL="https://supabase.test",
            SUPABASE_ANON_KEY="test-anon-key",
            OPENROUTER_API_KEY="test-openrouter",
            OPENAI_API_KEY="test-openai",
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
            response = client.post("/system/provision", json={}, headers={"Authorization": "Bearer wrong-key"})
            assert response.status_code == 401
            assert response.json()["detail"] == "Unauthorized"

    def test_provision_new_instance_success(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        mock_wait_for_deployment: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """Test successful provisioning of a new instance."""
        # Setup
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "123"}])
        mock_supabase.table().update().eq().execute.return_value = Mock()

        provision_data = {"subscription_id": "sub_test_123", "account_id": "acc_test_123", "tier": "starter"}

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

    def test_byok_provisioning_does_not_inject_platform_openrouter_key(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        mock_wait_for_deployment: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """BYOK instances should not receive a shared platform OpenRouter key."""
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "123"}])
        mock_supabase.table().update().eq().execute.return_value = Mock()

        with (
            patch("backend.routes.provisioner._apply_instance_secret", new_callable=AsyncMock) as apply_secret,
            patch("backend.routes.provisioner.create_openrouter_key", create=True) as create_key,
        ):
            apply_secret.return_value = "hash"
            response = client.post(
                "/system/provision",
                json={"subscription_id": "sub_test_123", "account_id": "acc_test_123", "tier": "byok"},
                headers=valid_auth_header,
            )

        assert response.status_code == 200
        secret_data = apply_secret.call_args.args[2]
        assert secret_data["openrouter_key"] == ""
        create_key.assert_not_called()

    def test_hobby_provisioning_creates_monthly_limited_openrouter_key(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        mock_wait_for_deployment: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """Hobby instances should get a per-customer $15 monthly OpenRouter key."""
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "123"}])
        mock_supabase.table().update().eq().execute.return_value = Mock()
        created_key = CreatedOpenRouterKey(
            key="sk-or-v1-hobby-customer",
            hash="hobby_hash",
            label="MindRoom hobby instance 123",
            limit_usd=15,
            limit_reset="monthly",
        )

        with (
            patch("backend.routes.provisioner.OPENROUTER_PROVISIONING_API_KEY", "sk-or-v1-management", create=True),
            patch("backend.routes.provisioner.create_openrouter_key", return_value=created_key, create=True) as create_key,
            patch("backend.routes.provisioner._apply_instance_secret", new_callable=AsyncMock) as apply_secret,
        ):
            apply_secret.return_value = "hash"
            response = client.post(
                "/system/provision",
                json={"subscription_id": "sub_test_123", "account_id": "acc_test_123", "tier": "hobby"},
                headers=valid_auth_header,
            )

        assert response.status_code == 200
        plan = create_key.call_args.kwargs["plan"]
        assert plan.monthly_limit_usd == 15
        assert plan.name == "MindRoom hobby account acc_test_123 instance 123"
        secret_data = apply_secret.call_args.args[2]
        assert secret_data["openrouter_key"] == "sk-or-v1-hobby-customer"
        update_payloads = [call_.args[0] for call_ in mock_supabase.table().update.call_args_list if call_.args]
        assert any(payload.get("openrouter_key_hash") == "hobby_hash" for payload in update_payloads)

    def test_pro_provisioning_uses_larger_resource_profile_and_openrouter_limit(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        mock_wait_for_deployment: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """Pro instances should get a $150 OpenRouter limit and larger Kubernetes resources."""
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "123"}])
        mock_supabase.table().update().eq().execute.return_value = Mock()
        created_key = CreatedOpenRouterKey(
            key="sk-or-v1-pro-customer",
            hash="pro_hash",
            label="MindRoom pro instance 123",
            limit_usd=150,
            limit_reset="monthly",
        )

        with (
            patch("backend.routes.provisioner.OPENROUTER_PROVISIONING_API_KEY", "sk-or-v1-management", create=True),
            patch("backend.routes.provisioner.create_openrouter_key", return_value=created_key, create=True) as create_key,
            patch("backend.routes.provisioner._apply_instance_secret", new_callable=AsyncMock) as apply_secret,
        ):
            apply_secret.return_value = "hash"
            response = client.post(
                "/system/provision",
                json={"subscription_id": "sub_test_123", "account_id": "acc_test_123", "tier": "pro"},
                headers=valid_auth_header,
            )

        assert response.status_code == 200
        plan = create_key.call_args.kwargs["plan"]
        assert plan.monthly_limit_usd == 150
        secret_data = apply_secret.call_args.args[2]
        assert secret_data["openrouter_key"] == "sk-or-v1-pro-customer"
        set_args = _helm_set_args(mock_helm.call_args.args[0])
        assert set_args["storage"] == "25Gi"
        assert set_args["mindroomResources.requests.memory"] == "1Gi"
        assert set_args["mindroomResources.limits.memory"] == "4Gi"
        assert set_args["synapseResources.requests.memory"] == "1Gi"
        assert set_args["sandboxRunnerResources.limits.memory"] == "2Gi"

    def test_provision_passes_owner_matrix_user_to_instance_chart(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        mock_wait_for_deployment: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """Provisioning should authorize the platform owner inside the hosted Matrix tenant."""
        account_id = "11111111-1111-4111-8111-111111111111"
        instances_table = MagicMock()
        instances_table.insert.return_value.execute.return_value = Mock(data=[{"instance_id": "123"}])
        instances_table.update.return_value.eq.return_value.execute.return_value = Mock()
        accounts_table = MagicMock()
        accounts_table.select.return_value.eq.return_value.limit.return_value.execute.return_value = Mock(
            data=[{"email": "Owner.User+Test@example.com"}]
        )
        mock_supabase.table.side_effect = lambda table: {
            "accounts": accounts_table,
            "instances": instances_table,
        }[table]

        with patch.multiple(
            "backend.routes.provisioner",
            INSTANCE_MATRIX_OIDC_ENABLED="true",
            INSTANCE_MATRIX_OIDC_ISSUER="https://api.mindroom.test/matrix-oidc",
            INSTANCE_MATRIX_OIDC_CLIENT_ID="mindroom-synapse",
        ):
            response = client.post(
                "/system/provision",
                json={"subscription_id": "sub_test_123", "account_id": account_id, "tier": "starter"},
                headers=valid_auth_header,
            )

        assert response.status_code == 200
        helm_args = mock_helm.call_args.args[0]
        set_args = _helm_set_args(helm_args)
        set_string_args = _helm_set_string_args(helm_args)
        assert set_args["matrixRoomAccess.mode"] == "multi_user"
        assert set_args["matrixRoomAccess.multiUserJoinRule"] == "public"
        assert set_args["matrixRoomAccess.publishToRoomDirectory"] == "false"
        assert set_args["matrixRoomAccess.reconcileExistingRooms"] == "true"
        assert set_string_args["matrixAutoJoinRoomKeys[0]"] == "analysis"
        assert set_string_args["matrixAutoJoinRoomKeys[9]"] == "lobby"
        assert set_string_args["authorizationGlobalUsers[0]"] == "@owner.user+test:123.mindroom.test"
        _assert_helm_uses_external_instance_secret(helm_args)

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
        mock_kubectl.side_effect = _kubectl_without_credentials_encryption_secret

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
        helm_args = mock_helm.call_args.args[0]
        _assert_helm_uses_external_instance_secret(helm_args)
        assert any(call_.args[0][:2] == ["apply", "-f"] for call_ in mock_kubectl.call_args_list)

    def test_provision_re_provision_existing_preserves_credentials_encryption(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        mock_wait_for_deployment: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """Re-provisioning an encrypted instance should keep the stable credential key."""
        mock_supabase.table().update().eq().execute.return_value = Mock(data=[{"instance_id": "456"}])
        mock_kubectl.side_effect = _kubectl_with_credentials_encryption_secret

        response = client.post(
            "/system/provision",
            json={
                "subscription_id": "sub_test_123",
                "account_id": "acc_test_123",
                "tier": "professional",
                "instance_id": "456",
            },
            headers=valid_auth_header,
        )

        assert response.status_code == 200
        helm_args = mock_helm.call_args.args[0]
        expected_key = base64.urlsafe_b64encode(b"1" * 32).decode("ascii").rstrip("=")
        assert expected_key not in " ".join(helm_args)
        _assert_helm_uses_external_instance_secret(helm_args)
        assert any(call_.args[0][:2] == ["apply", "-f"] for call_ in mock_kubectl.call_args_list)

    def test_provision_re_provision_existing_opt_in_preserves_existing_credentials_encryption_key(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        mock_wait_for_deployment: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """Explicit encryption opt-in should not rotate an already-encrypted instance key."""
        mock_supabase.table().update().eq().execute.return_value = Mock(data=[{"instance_id": "456"}])
        mock_kubectl.side_effect = _kubectl_with_credentials_encryption_secret

        response = client.post(
            "/system/provision",
            json={
                "subscription_id": "sub_test_123",
                "account_id": "acc_test_123",
                "tier": "professional",
                "instance_id": "456",
                "enable_credentials_encryption": True,
            },
            headers=valid_auth_header,
        )

        assert response.status_code == 200
        helm_args = mock_helm.call_args.args[0]
        expected_key = base64.urlsafe_b64encode(b"1" * 32).decode("ascii").rstrip("=")
        assert expected_key not in " ".join(helm_args)
        _assert_helm_uses_external_instance_secret(helm_args)
        assert any(call_.args[0][:2] == ["apply", "-f"] for call_ in mock_kubectl.call_args_list)

    def test_provision_re_provision_existing_can_opt_into_credentials_encryption(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        mock_wait_for_deployment: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """Existing instances should only enable credential encryption explicitly."""
        mock_supabase.table().update().eq().execute.return_value = Mock(data=[{"instance_id": "456"}])
        mock_kubectl.side_effect = _kubectl_without_credentials_encryption_secret

        response = client.post(
            "/system/provision",
            json={
                "subscription_id": "sub_test_123",
                "account_id": "acc_test_123",
                "tier": "professional",
                "instance_id": "456",
                "enable_credentials_encryption": True,
            },
            headers=valid_auth_header,
        )

        assert response.status_code == 200
        helm_args = mock_helm.call_args.args[0]
        _assert_helm_uses_external_instance_secret(helm_args)
        assert any(call_.args[0][:2] == ["apply", "-f"] for call_ in mock_kubectl.call_args_list)

    def test_provision_re_provision_existing_fails_when_existing_key_lookup_fails(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: AsyncMock,
        mock_helm: AsyncMock,
        valid_auth_header: dict,
        mock_config,
    ):
        """A real kubectl failure while reading the existing key must not rotate it."""
        mock_supabase.table().update().eq().execute.return_value = Mock(data=[{"instance_id": "456"}])
        mock_kubectl.side_effect = _kubectl_credentials_encryption_secret_lookup_fails

        response = client.post(
            "/system/provision",
            json={
                "subscription_id": "sub_test_123",
                "account_id": "acc_test_123",
                "tier": "professional",
                "instance_id": "456",
            },
            headers=valid_auth_header,
        )

        assert response.status_code == 500
        assert "Failed to inspect existing credential encryption state" in response.json()["detail"]
        mock_helm.assert_not_called()

    def test_provision_instance_not_found_for_reprovision(
        self, client: TestClient, mock_supabase: MagicMock, valid_auth_header: dict
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
        self, client: TestClient, mock_supabase: MagicMock, mock_kubectl: AsyncMock, valid_auth_header: dict
    ):
        """Test provisioning when kubectl is not available."""
        # Setup
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "123"}])
        mock_kubectl.side_effect = FileNotFoundError()

        provision_data = {"subscription_id": "sub_test_123", "account_id": "acc_test_123"}

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

        provision_data = {"subscription_id": "sub_test_123", "account_id": "acc_test_123"}

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

        provision_data = {"subscription_id": "sub_test_123", "account_id": "acc_test_123"}

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

        # Verify the full tenant stack was started in dependency order.
        mock_kubectl.assert_has_awaits(
            [
                call(["scale", "deployment/synapse-123", "--replicas=1"], namespace="mindroom-instances"),
                call(["scale", "deployment/mindroom-123", "--replicas=1"], namespace="mindroom-instances"),
            ]
        )
        assert mock_kubectl.await_count == 2
        mock_update_status.assert_called_with(123, "running")

    def test_start_instance_not_found(
        self, client: TestClient, mock_check_deployment: AsyncMock, valid_auth_header: dict
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
        self, client: TestClient, mock_kubectl: AsyncMock, mock_check_deployment: AsyncMock, valid_auth_header: dict
    ):
        """Test starting instance when kubectl fails."""
        # Setup
        mock_kubectl.return_value = (1, "", "kubectl error")

        # Make request
        response = client.post("/system/instances/123/start", headers=valid_auth_header)

        # Verify
        assert response.status_code == 500
        assert "Failed to start instance" in response.json()["detail"]

    def test_start_instance_requires_full_stack_scale_success(
        self,
        client: TestClient,
        mock_kubectl: AsyncMock,
        mock_check_deployment: AsyncMock,
        mock_update_status: Mock,
        valid_auth_header: dict,
    ):
        """Test start only marks running after Synapse and MindRoom are both scaled."""
        # Setup
        mock_kubectl.side_effect = [(0, "synapse scaled", ""), (1, "", "mindroom error")]

        # Make request
        response = client.post("/system/instances/123/start", headers=valid_auth_header)

        # Verify
        assert response.status_code == 500
        assert "Failed to start instance" in response.json()["detail"]
        mock_kubectl.assert_has_awaits(
            [
                call(["scale", "deployment/synapse-123", "--replicas=1"], namespace="mindroom-instances"),
                call(["scale", "deployment/mindroom-123", "--replicas=1"], namespace="mindroom-instances"),
            ]
        )
        mock_update_status.assert_not_called()

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

        # Verify the app stops before the homeserver to avoid reconnect churn.
        mock_kubectl.assert_has_awaits(
            [
                call(["scale", "deployment/mindroom-123", "--replicas=0"], namespace="mindroom-instances"),
                call(["scale", "deployment/synapse-123", "--replicas=0"], namespace="mindroom-instances"),
            ]
        )
        assert mock_kubectl.await_count == 2
        mock_update_status.assert_called_with(123, "stopped")

    def test_stop_instance_not_found(
        self, client: TestClient, mock_check_deployment: AsyncMock, valid_auth_header: dict
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
        self, client: TestClient, mock_kubectl: AsyncMock, mock_check_deployment: AsyncMock, valid_auth_header: dict
    ):
        """Test restarting an instance successfully."""
        # Make request
        response = client.post("/system/instances/123/restart", headers=valid_auth_header)

        # Verify
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "restarted successfully" in data["message"]

        # Verify both tenant deployments were restarted.
        mock_kubectl.assert_has_awaits(
            [
                call(["rollout", "restart", "deployment/synapse-123"], namespace="mindroom-instances"),
                call(["rollout", "restart", "deployment/mindroom-123"], namespace="mindroom-instances"),
            ]
        )
        assert mock_kubectl.await_count == 2

    def test_restart_instance_not_found(
        self, client: TestClient, mock_check_deployment: AsyncMock, valid_auth_header: dict
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
        self, client: TestClient, mock_helm: AsyncMock, mock_update_status: Mock, valid_auth_header: dict
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
        self, client: TestClient, mock_helm: AsyncMock, mock_update_status: Mock, valid_auth_header: dict
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

    def test_uninstall_instance_failure(self, client: TestClient, mock_helm: AsyncMock, valid_auth_header: dict):
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
                {"id": 1, "instance_id": "123", "status": "stopped"}  # Actually running
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

    def test_sync_instances_no_instances(self, client: TestClient, mock_supabase: MagicMock, valid_auth_header: dict):
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
        self, client: TestClient, mock_supabase: MagicMock, valid_auth_header: dict
    ):
        """Test syncing with instance missing instance_id."""
        # Setup
        mock_supabase.table().select().execute.return_value = Mock(
            data=[
                {"id": 1, "status": "running"}  # No instance_id or subdomain
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
            data=[{"id": 1, "instance_id": "123", "status": "running"}]
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
        self, client: TestClient, mock_supabase: MagicMock, valid_auth_header: dict
    ):
        """Test provisioning when database insert fails."""
        # Setup
        mock_supabase.table().insert().execute.return_value = Mock(data=[])

        provision_data = {"subscription_id": "sub_test_123", "account_id": "acc_test_123"}

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
