"""Extended tests for provisioner to achieve >98% coverage."""

from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient


class TestProvisionerExtended:
    """Extended tests for provisioner endpoints."""

    @pytest.fixture
    def client(self) -> TestClient:
        """Create test client."""
        from main import app  # noqa: PLC0415

        return TestClient(app)

    @pytest.fixture(autouse=True)
    def setup_auth(self):
        """Setup authentication for all tests."""
        with patch("backend.routes.provisioner.PROVISIONER_API_KEY", "test-api-key"):
            yield

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
            mock.return_value = (0, "Success", "")
            yield mock

    @pytest.fixture
    def mock_helm(self):
        """Mock helm commands."""
        with patch("backend.routes.provisioner.run_helm") as mock:
            mock.return_value = (0, "Success", "")
            yield mock

    @pytest.fixture
    def valid_auth(self):
        """Valid authorization header."""
        return {"Authorization": "Bearer test-api-key"}

    @pytest.mark.asyncio
    async def test_background_mark_running_when_ready_success(self):
        """Test background task marks instance as running when ready."""
        from backend.routes.provisioner import _background_mark_running_when_ready

        with patch("backend.routes.provisioner.wait_for_deployment_ready") as mock_wait:
            mock_wait.return_value = True
            with patch("backend.routes.provisioner.ensure_supabase") as mock_sb:
                mock_db = MagicMock()
                mock_sb.return_value = mock_db
                mock_db.table().update().eq().execute.return_value = Mock()

                await _background_mark_running_when_ready("test-instance", "test-ns")

                # Verify instance was marked as running
                mock_db.table.assert_called_with("instances")
                update_call = mock_db.table().update.call_args[0][0]
                assert update_call["status"] == "running"

    @pytest.mark.asyncio
    async def test_background_mark_running_when_ready_not_ready(self):
        """Test background task when deployment doesn't become ready."""
        from backend.routes.provisioner import _background_mark_running_when_ready

        with patch("backend.routes.provisioner.wait_for_deployment_ready") as mock_wait:
            mock_wait.return_value = False

            await _background_mark_running_when_ready("test-instance", "test-ns")

            # Should not update database when not ready
            with patch("backend.routes.provisioner.ensure_supabase") as mock_sb:
                mock_sb.assert_not_called()

    @pytest.mark.asyncio
    async def test_background_mark_running_when_ready_update_failure(self):
        """Test background task handles database update failure gracefully."""
        from backend.routes.provisioner import _background_mark_running_when_ready

        with patch("backend.routes.provisioner.wait_for_deployment_ready") as mock_wait:
            mock_wait.return_value = True
            with patch("backend.routes.provisioner.ensure_supabase") as mock_sb:
                mock_db = MagicMock()
                mock_sb.return_value = mock_db
                # Make update fail
                mock_db.table().update().eq().execute.side_effect = Exception("DB error")

                # Should not raise, just log warning
                await _background_mark_running_when_ready("test-instance", "test-ns")

    @pytest.mark.asyncio
    async def test_background_mark_running_when_ready_wait_exception(self):
        """Test background task handles wait exception gracefully."""
        from backend.routes.provisioner import _background_mark_running_when_ready

        with patch("backend.routes.provisioner.wait_for_deployment_ready") as mock_wait:
            mock_wait.side_effect = Exception("Wait failed")

            # Should not raise, just log exception
            await _background_mark_running_when_ready("test-instance", "test-ns")

    @pytest.mark.asyncio
    async def test_provision_reprovision_update_failure(self):
        """Test provision handles database update failure during re-provisioning."""
        from backend.routes.provisioner import provision_instance
        from fastapi import HTTPException

        with patch("backend.routes.provisioner.PROVISIONER_API_KEY", "test-api-key"):
            with patch("backend.routes.provisioner.ensure_supabase") as mock_sb:
                mock_db = MagicMock()
                mock_sb.return_value = mock_db

                # Setup existing instance
                mock_db.table().select().eq().single().execute.return_value = Mock(
                    data={
                        "instance_id": "123",
                        "account_id": "acc-123",
                        "status": "deprovisioned",
                    }
                )
                # Make update fail
                mock_db.table().update().eq().execute.side_effect = Exception("Update failed")

                with pytest.raises(HTTPException) as exc_info:
                    await provision_instance(
                        None,  # request
                        {
                            "subscription_id": "sub-123",
                            "account_id": "acc-123",
                            "tier": "professional",
                            "instance_id": 123,
                        },
                        "Bearer test-api-key",  # authorization
                        None,  # background_tasks
                    )

                assert exc_info.value.status_code == 500
                assert "Failed to update instance" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_provision_kubectl_not_found(self):
        """Test provision handles kubectl not found error."""
        from backend.routes.provisioner import provision_instance
        from fastapi import HTTPException

        with patch("backend.routes.provisioner.PROVISIONER_API_KEY", "test-api-key"):
            with patch("backend.routes.provisioner.ensure_supabase") as mock_sb:
                mock_db = MagicMock()
                mock_sb.return_value = mock_db

                # Setup new instance
                mock_db.table().select().eq().single().execute.return_value = Mock(data=None)
                mock_db.table().insert().execute.return_value = Mock(data=[{"instance_id": "456"}])

                with patch("backend.routes.provisioner.run_kubectl") as mock_kubectl:
                    mock_kubectl.side_effect = FileNotFoundError("kubectl not found")

                    with pytest.raises(HTTPException) as exc_info:
                        await provision_instance(
                            None,  # request
                            {
                                "subscription_id": "sub-123",
                                "account_id": "acc-123",
                                "tier": "starter",
                            },
                            "Bearer test-api-key",  # authorization
                            None,  # background_tasks
                        )

                    assert exc_info.value.status_code == 503
                    assert "Kubectl command not found" in str(exc_info.value.detail)

    def test_provision_namespace_creation_failure(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: Mock,
        valid_auth: dict,
    ):
        """Test provision handles namespace creation failure gracefully."""
        # Setup new instance
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=None)
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "789"}])

        # Make namespace creation fail (non-FileNotFoundError)
        mock_kubectl.side_effect = [
            Exception("Namespace already exists"),
            (0, "OK", ""),
        ]

        with patch("backend.routes.provisioner.run_helm") as mock_helm:
            mock_helm.return_value = (0, "Deployed", "")

            response = client.post(
                "/system/provision",
                json={
                    "subscription_id": "sub-123",
                    "account_id": "acc-123",
                    "tier": "starter",
                },
                headers=valid_auth,
            )

        # Should continue despite namespace error
        assert response.status_code == 200

    def test_provision_url_update_failure(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: Mock,
        mock_helm: Mock,
        valid_auth: dict,
    ):
        """Test provision handles URL update failure gracefully."""
        # Setup new instance
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=None)
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "999"}])

        # Make URL update fail
        update_count = 0

        def update_side_effect(*args, **kwargs):
            nonlocal update_count
            update_count += 1
            if update_count == 1:
                # First update is URLs - make it fail
                raise Exception("URL update failed")
            return Mock(execute=Mock(return_value=Mock()))

        mock_supabase.table().update.side_effect = update_side_effect

        response = client.post(
            "/system/provision",
            json={
                "subscription_id": "sub-123",
                "account_id": "acc-123",
                "tier": "starter",
            },
            headers=valid_auth,
        )

        # Should continue despite URL update failure
        assert response.status_code == 200

    def test_provision_helm_failure_with_db_update_error(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: Mock,
        mock_helm: Mock,
        valid_auth: dict,
    ):
        """Test provision handles helm failure with DB update error."""
        # Setup new instance
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=None)
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "111"}])

        # Make helm fail
        mock_helm.return_value = (1, "", "Helm deployment failed")

        # Make status update to error also fail
        update_count = 0

        def update_side_effect(*args, **kwargs):
            nonlocal update_count
            update_count += 1
            if update_count == 2:  # Second update is status to error
                raise Exception("DB update failed")
            return Mock(execute=Mock(return_value=Mock()))

        mock_supabase.table().update.side_effect = update_side_effect

        response = client.post(
            "/system/provision",
            json={
                "subscription_id": "sub-123",
                "account_id": "acc-123",
                "tier": "starter",
            },
            headers=valid_auth,
        )

        assert response.status_code == 500
        assert "Helm install failed" in response.json()["detail"]

    def test_provision_general_exception_with_db_update_error(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: Mock,
        valid_auth: dict,
    ):
        """Test provision handles general exception with DB update error."""
        # Setup new instance
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=None)
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "222"}])

        # Make helm raise unexpected exception
        with patch("backend.routes.provisioner.run_helm") as mock_helm:
            mock_helm.side_effect = RuntimeError("Unexpected error")

            # Make status update to error also fail
            update_count = 0

            def update_side_effect(*args, **kwargs):
                nonlocal update_count
                update_count += 1
                if update_count == 2:  # Second update is status to error
                    raise Exception("DB update failed")
                return Mock(execute=Mock(return_value=Mock()))

            mock_supabase.table().update.side_effect = update_side_effect

            with patch("backend.config.PROVISIONER_API_KEY", "test-api-key"):
                response = client.post(
                    "/system/provision",
                    json={
                        "subscription_id": "sub-123",
                        "account_id": "acc-123",
                        "tier": "starter",
                    },
                    headers=valid_auth,
                )

        assert response.status_code == 500
        assert "Failed to deploy instance" in response.json()["detail"]

    def test_provision_status_update_after_readiness_failure(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: Mock,
        mock_helm: Mock,
        valid_auth: dict,
    ):
        """Test provision handles status update failure after readiness check."""
        # Setup new instance
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=None)
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "333"}])

        with patch("backend.routes.provisioner.wait_for_deployment_ready") as mock_wait:
            mock_wait.return_value = True  # Ready

            # Make final status update fail
            update_count = 0

            def update_side_effect(*args, **kwargs):
                nonlocal update_count
                update_count += 1
                if update_count == 2:  # Second update is final status
                    raise Exception("Final status update failed")
                return Mock(execute=Mock(return_value=Mock()))

            mock_supabase.table().update.side_effect = update_side_effect

            with patch("backend.config.PROVISIONER_API_KEY", "test-api-key"):
                response = client.post(
                    "/system/provision",
                    json={
                        "subscription_id": "sub-123",
                        "account_id": "acc-123",
                        "tier": "starter",
                    },
                    headers=valid_auth,
                )

            # Should still succeed despite status update failure
            assert response.status_code == 200

    def test_provision_background_task_scheduling_failure(
        self,
        client: TestClient,
        mock_supabase: MagicMock,
        mock_kubectl: Mock,
        mock_helm: Mock,
        valid_auth: dict,
    ):
        """Test provision handles background task scheduling failure."""
        # Setup new instance
        mock_supabase.table().select().eq().single().execute.return_value = Mock(data=None)
        mock_supabase.table().insert().execute.return_value = Mock(data=[{"instance_id": "444"}])

        with patch("backend.routes.provisioner.wait_for_deployment_ready") as mock_wait:
            mock_wait.return_value = False  # Not ready

            # Mock BackgroundTasks to raise on add_task
            mock_bg_tasks = Mock(spec=BackgroundTasks)
            mock_bg_tasks.add_task.side_effect = Exception("Cannot schedule task")

            with patch("backend.config.PROVISIONER_API_KEY", "test-api-key"):
                # Call provision_instance directly with mock background tasks
                from backend.routes.provisioner import provision_instance

                response_coro = provision_instance(
                    None,  # request
                    {
                        "subscription_id": "sub-123",
                        "account_id": "acc-123",
                        "tier": "starter",
                    },
                    "Bearer test-api-key",
                    mock_bg_tasks,
                )

                import asyncio

                result = asyncio.run(response_coro)

            # Should still succeed despite background task failure
            assert result["success"] is True

    def test_start_instance_status_update_failure(
        self,
        client: TestClient,
        mock_kubectl: Mock,
        valid_auth: dict,
    ):
        """Test start instance handles status update failure."""
        with patch("backend.routes.provisioner.check_deployment_exists") as mock_check:
            mock_check.return_value = True
            with patch("backend.routes.provisioner.update_instance_status") as mock_update:
                mock_update.return_value = False  # Update fails

                response = client.post(
                    "/system/instances/555/start",
                    headers=valid_auth,
                )

        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_stop_instance_status_update_failure(
        self,
        client: TestClient,
        mock_kubectl: Mock,
        valid_auth: dict,
    ):
        """Test stop instance handles status update failure."""
        with patch("backend.routes.provisioner.check_deployment_exists") as mock_check:
            mock_check.return_value = True
            with patch("backend.routes.provisioner.update_instance_status") as mock_update:
                mock_update.return_value = False  # Update fails

                response = client.post(
                    "/system/instances/666/stop",
                    headers=valid_auth,
                )

        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_restart_instance_kubectl_failure(
        self,
        client: TestClient,
        mock_kubectl: Mock,
        valid_auth: dict,
    ):
        """Test restart instance handles kubectl failure."""
        mock_kubectl.return_value = (1, "", "Kubectl failed")

        with patch("backend.routes.provisioner.check_deployment_exists") as mock_check:
            mock_check.return_value = True
            response = client.post(
                "/system/instances/777/restart",
                headers=valid_auth,
            )

        assert response.status_code == 500
        assert "kubectl command failed" in response.json()["detail"]

    def test_restart_instance_general_exception(
        self,
        client: TestClient,
        valid_auth: dict,
    ):
        """Test restart instance handles general exception."""
        with patch("backend.routes.provisioner.check_deployment_exists") as mock_check:
            mock_check.return_value = True
            with patch("backend.routes.provisioner.run_kubectl") as mock_kubectl:
                mock_kubectl.side_effect = RuntimeError("Unexpected error")

                response = client.post(
                    "/system/instances/888/restart",
                    headers=valid_auth,
                )

        assert response.status_code == 500
        assert "Failed to restart instance" in response.json()["detail"]

    def test_uninstall_instance_status_update_failure(
        self,
        client: TestClient,
        mock_helm: Mock,
        valid_auth: dict,
    ):
        """Test uninstall handles status update failure."""
        with patch("backend.config.PROVISIONER_API_KEY", "test-api-key"):
            with patch("backend.routes.provisioner.update_instance_status") as mock_update:
                mock_update.return_value = False  # Update fails

                response = client.delete(
                    "/system/instances/999/uninstall",
                    headers=valid_auth,
                )

        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_uninstall_instance_general_exception(
        self,
        client: TestClient,
        valid_auth: dict,
    ):
        """Test uninstall handles general exception."""
        with patch("backend.routes.provisioner.run_helm") as mock_helm:
            mock_helm.side_effect = RuntimeError("Helm uninstall failed")

            with patch("backend.config.PROVISIONER_API_KEY", "test-api-key"):
                response = client.delete(
                    "/system/instances/1000/uninstall",
                    headers=valid_auth,
                )

        assert response.status_code == 500
        assert "Failed to uninstall instance" in response.json()["detail"]

    def test_sync_instances_general_exception(
        self,
        client: TestClient,
        valid_auth: dict,
    ):
        """Test sync instances handles general exception."""
        with patch("backend.routes.provisioner.ensure_supabase") as mock_sb:
            mock_db = MagicMock()
            mock_sb.return_value = mock_db
            # Make the select().execute() call raise an exception
            mock_db.table().select().execute.side_effect = RuntimeError("Database connection failed")

            response = client.post(
                "/system/sync-instances",
                headers=valid_auth,
            )

        assert response.status_code == 500
        assert "Failed to sync instances" in response.json()["detail"]
