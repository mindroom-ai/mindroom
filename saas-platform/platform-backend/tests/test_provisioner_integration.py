"""Integration tests for provisioner with realistic scenarios."""

import asyncio
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient


class TestProvisionerIntegration:
    """Realistic integration tests for provisioner."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    @pytest.fixture(autouse=True)
    def setup_auth(self):
        """Setup authentication for all tests."""
        with (
            patch("backend.routes.provisioner.PROVISIONER_API_KEY", "test-api-key"),
            patch("backend.services.provisioner_service.PROVISIONER_API_KEY", "test-api-key"),
        ):
            yield

    @pytest.fixture
    def valid_auth(self):
        """Valid authorization header."""
        return {"Authorization": "Bearer test-api-key"}

    def test_provisioning_failure_with_rollback(self, client: TestClient, valid_auth: dict):
        """Test provisioning failure triggers proper error handling and status updates."""
        with patch("backend.routes.provisioner.ensure_supabase") as mock_sb:
            mock_db = MagicMock()
            mock_sb.return_value = mock_db

            mock_db.table().insert().execute.return_value = Mock(data=[{"instance_id": "99"}])

            # Track status updates
            status_updates = []

            def update_side_effect(data):
                status_updates.append(data.get("status"))
                return Mock(eq=Mock(return_value=Mock(execute=Mock(return_value=Mock()))))

            mock_db.table().update.side_effect = update_side_effect

            with patch("backend.services.provisioner_service.run_kubectl") as mock_kubectl:
                mock_kubectl.return_value = (0, "Namespace created", "")

                with patch("backend.services.provisioner_service.run_helm") as mock_helm:
                    # Helm deployment fails
                    mock_helm.return_value = (1, "", "Error: timed out waiting for the condition")

                    response = client.post(
                        "/system/provision",
                        json={"subscription_id": "sub-789", "account_id": "acc-111", "tier": "byok"},
                        headers=valid_auth,
                    )

            assert response.status_code == 500
            assert "Helm install failed" in response.json()["detail"]

            # Verify status was updated to error
            assert "error" in status_updates

    def test_instance_lifecycle_start_stop_restart(
        self,
        client: TestClient,
        valid_auth: dict,
        stub_uninstall_cleanup: Mock,  # noqa: ARG002
    ):
        """Test complete instance lifecycle: provision -> stop -> start -> restart -> uninstall."""
        with patch("backend.routes.provisioner.ensure_supabase") as mock_sb:
            mock_db = MagicMock()
            mock_sb.return_value = mock_db

            # Step 1: Provision instance
            mock_db.table().insert().execute.return_value = Mock(data=[{"instance_id": "777"}])
            mock_db.table().update().eq().execute.return_value = Mock()

            with patch("backend.services.provisioner_service.run_kubectl") as mock_kubectl:
                mock_kubectl.return_value = (0, "Success", "")

                with patch("backend.services.provisioner_service.run_helm") as mock_helm:
                    mock_helm.return_value = (0, "Deployed", "")

                    with patch("backend.services.provisioner_service.wait_for_deployment_ready") as mock_wait:
                        mock_wait.return_value = True  # Ready immediately

                        provision_response = client.post(
                            "/system/provision",
                            json={"subscription_id": "sub-lifecycle", "account_id": "acc-lifecycle", "tier": "byok"},
                            headers=valid_auth,
                        )

            assert provision_response.status_code == 200
            instance_id = provision_response.json()["customer_id"]

            # Step 2: Stop the instance
            with patch("backend.services.provisioner_service.check_deployment_exists") as mock_check:
                mock_check.return_value = True
                with patch("backend.services.provisioner_service.run_kubectl") as mock_kubectl:
                    mock_kubectl.return_value = (0, "Deployment scaled to 0", "")

                    with patch("backend.services.provisioner_service.update_instance_status") as mock_update:
                        mock_update.return_value = True

                        stop_response = client.post(f"/system/instances/{instance_id}/stop", headers=valid_auth)

            assert stop_response.status_code == 200
            assert stop_response.json()["success"] is True

            # Step 3: Start the instance
            with patch("backend.services.provisioner_service.check_deployment_exists") as mock_check:
                mock_check.return_value = True
                with patch("backend.services.provisioner_service.run_kubectl") as mock_kubectl:
                    mock_kubectl.return_value = (0, "Deployment scaled to 1", "")

                    with patch("backend.services.provisioner_service.update_instance_status") as mock_update:
                        mock_update.return_value = True

                        start_response = client.post(f"/system/instances/{instance_id}/start", headers=valid_auth)

            assert start_response.status_code == 200
            assert start_response.json()["success"] is True

            # Step 4: Restart the instance
            with patch("backend.services.provisioner_service.check_deployment_exists") as mock_check:
                mock_check.return_value = True
                with patch("backend.services.provisioner_service.run_kubectl") as mock_kubectl:
                    mock_kubectl.return_value = (0, "Deployment restarted", "")

                    restart_response = client.post(f"/system/instances/{instance_id}/restart", headers=valid_auth)

            assert restart_response.status_code == 200
            assert restart_response.json()["success"] is True

            # Step 5: Uninstall the instance
            with patch("backend.services.provisioner_service.run_helm") as mock_helm:
                mock_helm.return_value = (0, "Release uninstalled", "")

                with patch("backend.services.provisioner_service.update_instance_status") as mock_update:
                    mock_update.return_value = True

                    uninstall_response = client.delete(f"/system/instances/{instance_id}/uninstall", headers=valid_auth)

            assert uninstall_response.status_code == 200
            assert uninstall_response.json()["success"] is True

    def test_provisioning_requests_for_different_accounts_get_unique_instances(
        self, client: TestClient, valid_auth: dict
    ):
        """Successive provisioning requests for different accounts each get their own instance ID."""
        with patch("backend.routes.provisioner.ensure_supabase") as mock_sb:
            mock_db = MagicMock()
            mock_sb.return_value = mock_db

            # Each request gets a different instance ID
            instance_counter = [1000]

            def insert_side_effect(data):
                instance_id = str(instance_counter[0])
                instance_counter[0] += 1
                return Mock(execute=Mock(return_value=Mock(data=[{"instance_id": instance_id}])))

            mock_db.table().insert.side_effect = insert_side_effect
            mock_db.table().update().eq().execute.return_value = Mock()

            with patch("backend.services.provisioner_service.run_kubectl") as mock_kubectl:
                mock_kubectl.return_value = (0, "Success", "")

                with patch("backend.services.provisioner_service.run_helm") as mock_helm:
                    mock_helm.return_value = (0, "Deployed", "")

                    with patch("backend.services.provisioner_service.wait_for_deployment_ready") as mock_wait:
                        mock_wait.return_value = False  # Not immediately ready

                        responses = []
                        for i in range(3):
                            response = client.post(
                                "/system/provision",
                                json={
                                    "subscription_id": f"sub-concurrent-{i}",
                                    "account_id": f"acc-concurrent-{i}",
                                    "tier": "byok",
                                },
                                headers=valid_auth,
                            )
                            responses.append(response)

            # All should succeed with unique instance IDs
            instance_ids = set()
            for response in responses:
                assert response.status_code == 200
                result = response.json()
                assert result["success"] is True
                instance_ids.add(result["customer_id"])

            assert len(instance_ids) == 3  # All unique

    def test_reprovision_deprovisioned_instance(self, client: TestClient, valid_auth: dict):
        """Test re-provisioning a previously deprovisioned instance."""
        with patch("backend.routes.provisioner.ensure_supabase") as mock_sb:
            mock_db = MagicMock()
            mock_sb.return_value = mock_db

            # Existing deprovisioned instance
            existing_instance = {
                "instance_id": "555",
                "account_id": "acc-reprov",
                "status": "deprovisioned",
                "subdomain": "old-subdomain",
            }

            # Track updates
            updates = []

            def update_side_effect(data):
                updates.append(data)
                return Mock(eq=Mock(return_value=Mock(execute=Mock(return_value=Mock(data=[existing_instance])))))

            mock_db.table().update.side_effect = update_side_effect

            with patch("backend.services.provisioner_service.run_kubectl") as mock_kubectl:

                async def kubectl_side_effect(args, **kwargs):
                    if args[:2] == ["get", "pvc"]:
                        return (0, '{"items":[]}', "")
                    if args[:2] == ["get", "secret"]:
                        return (0, "", "")
                    return (0, "Success", "")

                mock_kubectl.side_effect = kubectl_side_effect

                with patch("backend.services.provisioner_service.run_helm") as mock_helm:
                    mock_helm.return_value = (0, "Release upgraded", "")

                    with patch("backend.services.provisioner_service.wait_for_deployment_ready") as mock_wait:
                        mock_wait.return_value = True

                        response = client.post(
                            "/system/provision",
                            json={
                                "subscription_id": "sub-reprov",
                                "account_id": "acc-reprov",
                                "tier": "byok",
                                "instance_id": 555,  # Re-use existing ID
                            },
                            headers=valid_auth,
                        )

            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True
            assert result["customer_id"] == "555"  # Same ID

            # Verify status was updated from deprovisioned to provisioning/running
            status_values = [u.get("status") for u in updates if "status" in u]
            assert "provisioning" in status_values or "running" in status_values

    def test_provision_with_background_readiness_check(self, client: TestClient, valid_auth: dict):
        """Test provisioning with background task for delayed readiness."""
        with patch("backend.routes.provisioner.ensure_supabase") as mock_sb:
            mock_db = MagicMock()
            mock_sb.return_value = mock_db

            mock_db.table().insert().execute.return_value = Mock(data=[{"instance_id": "888"}])
            mock_db.table().update().eq().execute.return_value = Mock()

            with patch("backend.services.provisioner_service.run_kubectl") as mock_kubectl:
                mock_kubectl.return_value = (0, "Success", "")

                with patch("backend.services.provisioner_service.run_helm") as mock_helm:
                    mock_helm.return_value = (0, "Deployed", "")

                    with patch("backend.services.provisioner_service.wait_for_deployment_ready") as mock_wait:
                        # Not ready initially
                        mock_wait.return_value = False

                        # Direct function call with background tasks
                        from backend.routes.provisioner import provision_instance

                        result = asyncio.run(
                            provision_instance(
                                None,  # request
                                {"subscription_id": "sub-bg", "account_id": "acc-bg", "tier": "byok"},
                                "Bearer test-api-key",
                                BackgroundTasks(),
                            )
                        )

            assert result["success"] is True
            assert "customer_id" in result

    def test_error_recovery_during_provisioning(self, client: TestClient, valid_auth: dict):
        """Test error recovery mechanisms during various provisioning stages."""
        with patch("backend.routes.provisioner.ensure_supabase") as mock_sb:
            mock_db = MagicMock()
            mock_sb.return_value = mock_db

            mock_db.table().insert().execute.return_value = Mock(data=[{"instance_id": "666"}])

            # Some updates fail but provisioning continues
            update_count = [0]

            def update_side_effect(data):
                update_count[0] += 1
                if update_count[0] == 1 and "instance_url" in data:
                    # URL update fails
                    raise Exception("Database temporarily unavailable")
                return Mock(eq=Mock(return_value=Mock(execute=Mock(return_value=Mock()))))

            mock_db.table().update.side_effect = update_side_effect

            with patch("backend.services.provisioner_service.run_kubectl") as mock_kubectl:
                mock_kubectl.return_value = (0, "Success", "")

                with patch("backend.services.provisioner_service.run_helm") as mock_helm:
                    mock_helm.return_value = (0, "Deployed", "")

                    with patch("backend.services.provisioner_service.wait_for_deployment_ready") as mock_wait:
                        mock_wait.return_value = True

                        response = client.post(
                            "/system/provision",
                            json={"subscription_id": "sub-recovery", "account_id": "acc-recovery", "tier": "byok"},
                            headers=valid_auth,
                        )

            # Should succeed despite URL update failure
            assert response.status_code == 200
            result = response.json()
            assert result["success"] is True

    def test_stop_instance_kubectl_error(self, client: TestClient, valid_auth: dict):
        """Test handling of a kubectl error while stopping an instance."""
        with patch("backend.services.provisioner_service.check_deployment_exists") as mock_check:
            mock_check.return_value = True
            with patch("backend.services.provisioner_service.run_kubectl") as mock_kubectl:
                # Kubectl returns error
                mock_kubectl.return_value = (1, "", "Error: deployment 'mindroom-stop' not found")

                response = client.post("/system/instances/101/stop", headers=valid_auth)

        assert response.status_code == 500
        assert "kubectl command failed" in response.json()["detail"]

    def test_uninstall_with_helm_errors(
        self,
        client: TestClient,
        valid_auth: dict,
        stub_uninstall_cleanup: Mock,  # noqa: ARG002
    ):
        """Test uninstall handling when helm encounters various errors."""
        with patch("backend.services.provisioner_service.run_helm") as mock_helm:
            # Helm uninstall fails with specific error
            mock_helm.return_value = (1, "", "Error: release mindroom-999 not found")

            response = client.delete("/system/instances/999/uninstall", headers=valid_auth)

            # Should handle gracefully
            assert response.status_code == 200
            assert "uninstalled successfully" in response.json()["message"].lower()
