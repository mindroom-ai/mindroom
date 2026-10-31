"""Focused unit tests for the extracted provisioner service helpers."""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import yaml
from backend.openrouter import CreatedOpenRouterKey
from backend.routes.instances import _background_sync_instance_status
from backend.services import provisioner_service

from tests.fake_supabase import FakeSupabase

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class TestSecretDerivation:
    """Stable per-instance secret derivation."""

    def test_stable_instance_secret_is_deterministic_and_scoped(self):
        """Same purpose and instance always derive the same secret; any other input differs."""
        with patch.multiple(
            provisioner_service,
            INSTANCE_CREDENTIALS_ENCRYPTION_SECRET="root-secret",
            PROVISIONER_API_KEY="fallback-secret",
        ):
            first = provisioner_service._stable_instance_secret("instance-credentials", "123")
            second = provisioner_service._stable_instance_secret("instance-credentials", "123")
            other_instance = provisioner_service._stable_instance_secret("instance-credentials", "456")
            other_purpose = provisioner_service._stable_instance_secret("matrix-registration", "123")

        assert first == second
        assert first != other_instance
        assert first != other_purpose
        assert len(base64.urlsafe_b64decode(f"{first}=")) == 32

    def test_stable_instance_secret_falls_back_to_provisioner_api_key(self):
        """Without a dedicated root secret, derivation falls back to the provisioner API key."""
        with patch.multiple(
            provisioner_service,
            INSTANCE_CREDENTIALS_ENCRYPTION_SECRET="",
            PROVISIONER_API_KEY="fallback-secret",
        ):
            from_fallback = provisioner_service._instance_credentials_encryption_key("123")
        with patch.multiple(
            provisioner_service,
            INSTANCE_CREDENTIALS_ENCRYPTION_SECRET="root-secret",
            PROVISIONER_API_KEY="fallback-secret",
        ):
            from_root = provisioner_service._instance_credentials_encryption_key("123")

        assert from_fallback
        assert from_root != from_fallback


class TestHelmArgsAssembly:
    """Helm argument assembly for OIDC and resource profiles."""

    def test_matrix_oidc_helm_args_enabled(self):
        """Enabled hosted OIDC forwards SSO, room access, and auto-join settings."""
        helm_args: list[str] = []
        with patch.multiple(
            provisioner_service,
            INSTANCE_MATRIX_OIDC_ENABLED="true",
            INSTANCE_MATRIX_OIDC_ISSUER="https://api.mindroom.test/matrix-oidc",
            INSTANCE_MATRIX_OIDC_CLIENT_ID="mindroom-matrix",
        ):
            provisioner_service._append_matrix_oidc_helm_args(helm_args)

        set_pairs = [helm_args[i + 1] for i, arg in enumerate(helm_args) if arg == "--set"]
        set_string_pairs = [helm_args[i + 1] for i, arg in enumerate(helm_args) if arg == "--set-string"]
        assert "matrixOidc.enabled=true" in set_pairs
        assert "matrixOidc.issuer=https://api.mindroom.test/matrix-oidc" in set_pairs
        assert "matrixOidc.clientId=mindroom-matrix" in set_pairs
        assert "roomDefaults.joinPolicy=public" in set_pairs
        assert "roomDefaults.listed=false" in set_pairs
        assert set_string_pairs[0] == "matrixAutoJoinRoomKeys[0]=personal"
        assert len(set_string_pairs) == len(provisioner_service._HOSTED_MATRIX_AUTO_JOIN_ROOM_KEYS)

    def test_matrix_oidc_helm_args_normalize_enabled_flag_for_chart(self):
        """Any enabled flag spelling renders the chart's OIDC branch, matching the owner authorization gate."""
        helm_args: list[str] = []
        with patch.multiple(
            provisioner_service,
            INSTANCE_MATRIX_OIDC_ENABLED="1",
            INSTANCE_MATRIX_OIDC_ISSUER="",
            INSTANCE_MATRIX_OIDC_CLIENT_ID="",
        ):
            provisioner_service._append_matrix_oidc_helm_args(helm_args)

        set_pairs = [helm_args[i + 1] for i, arg in enumerate(helm_args) if arg == "--set"]
        assert "matrixOidc.enabled=true" in set_pairs
        assert "matrixOidc.enabled=1" not in set_pairs

    def test_matrix_oidc_helm_args_disabled(self):
        """Disabled hosted OIDC adds no Helm arguments."""
        helm_args: list[str] = []
        with patch.multiple(
            provisioner_service,
            INSTANCE_MATRIX_OIDC_ENABLED="",
            INSTANCE_MATRIX_OIDC_ISSUER="",
            INSTANCE_MATRIX_OIDC_CLIENT_ID="",
        ):
            provisioner_service._append_matrix_oidc_helm_args(helm_args)
        assert helm_args == []

    def test_resource_profile_helm_args_pro(self):
        """The pro resource profile forwards every configured override."""
        helm_args: list[str] = []
        provisioner_service._append_resource_profile_helm_args(helm_args, "pro")

        set_pairs = dict(helm_args[i + 1].split("=", 1) for i, arg in enumerate(helm_args) if arg == "--set")
        assert set_pairs == provisioner_service._RESOURCE_PROFILE_HELM_VALUES["pro"]

    def test_pro_resource_profile_overrides_every_default_chart_resource_quantity(self):
        """A larger plan must not keep a default quantity, such as an ephemeral-storage limit, by omission."""
        chart_values = yaml.safe_load((_REPOSITORY_ROOT / "cluster/k8s/instance/values.yaml").read_text())
        default_keys = {
            f"{component}.{kind}.{resource}"
            for component in ("mindroomResources", "tuwunelResources", "sandboxRunnerResources")
            for kind, quantities in chart_values[component].items()
            for resource in quantities
        }

        assert "sandboxRunnerResources.limits.ephemeral-storage" in default_keys
        assert default_keys <= set(provisioner_service._RESOURCE_PROFILE_HELM_VALUES["pro"])

    def test_pro_resource_profile_doubles_default_ephemeral_storage_limits(self):
        """Pro containers get twice the default disk headroom, including the primary's room for runtime tool extras."""
        chart_values = yaml.safe_load((_REPOSITORY_ROOT / "cluster/k8s/instance/values.yaml").read_text())
        pro = provisioner_service._RESOURCE_PROFILE_HELM_VALUES["pro"]

        assert chart_values["mindroomResources"]["limits"]["ephemeral-storage"] == "16Gi"
        for component in ("mindroomResources", "tuwunelResources", "sandboxRunnerResources"):
            default_gib = int(chart_values[component]["limits"]["ephemeral-storage"].removesuffix("Gi"))
            assert pro[f"{component}.limits.ephemeral-storage"] == f"{2 * default_gib}Gi"

    def test_pro_resource_profile_keeps_ephemeral_storage_requests_small(self):
        """Every tenant's requests count against the shared node's allocatable ephemeral storage."""
        pro = provisioner_service._RESOURCE_PROFILE_HELM_VALUES["pro"]
        requests = {key: value for key, value in pro.items() if key.endswith(".requests.ephemeral-storage")}

        assert requests == {
            "mindroomResources.requests.ephemeral-storage": "64Mi",
            "tuwunelResources.requests.ephemeral-storage": "64Mi",
            "sandboxRunnerResources.requests.ephemeral-storage": "64Mi",
        }

    def test_resource_profile_helm_args_unknown_profile_is_noop(self):
        """Unknown resource profiles add no Helm arguments."""
        helm_args: list[str] = []
        provisioner_service._append_resource_profile_helm_args(helm_args, "free")
        assert helm_args == []


class TestOpenRouterMetadataRoundTrip:
    """Persisted OpenRouter key metadata round-trips through the lookup helpers."""

    def test_persisted_metadata_matches_and_exposes_hash(self):
        """The row written by persist matches the budget check and hash lookup."""
        created_key = CreatedOpenRouterKey(
            key="sk-or-v1-customer",
            hash="key_hash_123",
            label="MindRoom hobby instance 123",
            limit_usd=15,
            limit_reset="monthly",
        )
        sb = MagicMock()

        provisioner_service._persist_openrouter_key_metadata(sb, "123", created_key)

        persisted_row = sb.table.return_value.update.call_args.args[0]
        sb.table.assert_called_with("instances")
        sb.table.return_value.update.return_value.eq.assert_called_with("instance_id", "123")

        assert provisioner_service._matching_openrouter_metadata(persisted_row, 15) is True
        assert provisioner_service._matching_openrouter_metadata(persisted_row, 150) is False
        assert provisioner_service._stored_openrouter_key_hash(persisted_row) == "key_hash_123"

    def test_stored_hash_ignores_blank_and_missing_values(self):
        """Blank or absent stored hashes are not usable for lifecycle cleanup."""
        assert provisioner_service._stored_openrouter_key_hash(None) is None
        assert provisioner_service._stored_openrouter_key_hash({}) is None
        assert provisioner_service._stored_openrouter_key_hash({"openrouter_key_hash": "   "}) is None
        assert provisioner_service._stored_openrouter_key_hash({"openrouter_key_hash": " h1 "}) == "h1"


def _fake_kubectl(deployments: dict[str, dict[str, Any]], *, unreachable: frozenset[str] = frozenset()):
    """Return a `run_cmd` stand-in answering `kubectl get` for the given MindRoom deployments by instance ID.

    Instances in `unreachable` get the failure kubectl reports when it cannot reach the API server.
    """

    async def run_cmd(cmd: list[str], **_kwargs: Any) -> tuple[int, str, str]:  # noqa: ANN401
        assert cmd[:2] == ["kubectl", "get"]
        assert "-o=json" in cmd
        assert "--ignore-not-found" in cmd
        instance_id = cmd[2].removeprefix("deployment/mindroom-")
        if instance_id in unreachable:
            return 1, "", "The connection to the server was refused - did you specify the right host or port?"
        deployment = deployments.get(instance_id)
        return 0, "" if deployment is None else json.dumps(deployment), ""

    return run_cmd


def _deployment(
    *, desired: int, ready: int, available: int | None = None, updated: int | None = None
) -> dict[str, Any]:
    """Return deployment JSON as kubectl prints it, which omits replica counts that are zero."""
    counts = {
        "readyReplicas": ready,
        "availableReplicas": ready if available is None else available,
        "updatedReplicas": desired if updated is None else updated,
    }
    return {"spec": {"replicas": desired}, "status": {k: v for k, v in counts.items() if v}}


class TestInstanceStatusSync:
    """Kubernetes-to-database instance status reconciliation."""

    def test_bulk_sync_keeps_an_unready_instance_provisioning(self):
        """An instance whose deployment wants a pod that is not ready yet is not reported running."""
        db = FakeSupabase({"instances": [{"id": "1", "instance_id": "7", "status": "provisioning"}]})

        with patch("backend.k8s.run_cmd", _fake_kubectl({"7": _deployment(desired=1, ready=0)})):
            result = asyncio.run(provisioner_service.sync_instances(db))

        assert db.row("instances", instance_id="7")["status"] == "provisioning"
        assert result == {"total": 1, "synced": 0, "errors": 0, "updates": []}

    @pytest.mark.parametrize(
        ("deployment", "expected"),
        [
            (_deployment(desired=0, ready=0), "stopped"),
            (_deployment(desired=1, ready=1), "running"),
            (_deployment(desired=2, ready=1), "provisioning"),
            (_deployment(desired=1, ready=1, available=0), "provisioning"),
            (_deployment(desired=1, ready=1, updated=0), "provisioning"),
            ({"spec": {"replicas": 1}}, "provisioning"),
        ],
    )
    def test_running_needs_every_desired_pod_ready_available_and_updated(
        self, deployment: dict[str, Any], expected: str
    ):
        """A rollout whose ready pods are still the old ones, or not yet available, is not running."""
        assert provisioner_service._deployment_status(deployment) == expected

    @pytest.mark.parametrize(
        ("stored", "deployment", "expected"),
        [
            ("stopped", _deployment(desired=1, ready=1), "running"),
            ("error", _deployment(desired=1, ready=1), "running"),
            ("running", _deployment(desired=1, ready=0), "provisioning"),
            ("running", _deployment(desired=0, ready=0), "stopped"),
            ("running", None, "error"),
            ("stopped", None, "error"),
            ("deprovisioned", None, "deprovisioned"),
            ("provisioning", None, "provisioning"),
        ],
    )
    def test_sync_records_the_status_kubernetes_shows(
        self, stored: str, deployment: dict[str, Any] | None, expected: str
    ):
        """Each sync stamps the row with the status its deployment, or the lack of one, shows."""
        db = FakeSupabase({"instances": [{"id": "1", "instance_id": "7", "status": stored}]})
        deployments = {} if deployment is None else {"7": deployment}

        with patch("backend.k8s.run_cmd", _fake_kubectl(deployments)):
            asyncio.run(provisioner_service.sync_instance_status(db, "7", stored))

        row = db.row("instances", instance_id="7")
        assert row["status"] == expected
        assert row["kubernetes_synced_at"]

    @pytest.mark.parametrize("trigger", ["dashboard", "bulk"])
    def test_kubectl_failure_leaves_the_row_for_the_next_sync(self, trigger: str):
        """A failed observation changes nothing, and the unstamped row is checked again on the next dashboard poll."""
        db = FakeSupabase({"instances": [{"id": "1", "instance_id": "7", "status": "running"}]})

        with (
            patch("backend.k8s.run_cmd", _fake_kubectl({}, unreachable=frozenset({"7"}))),
            patch("backend.routes.instances.ensure_supabase", return_value=db),
        ):
            if trigger == "dashboard":
                asyncio.run(_background_sync_instance_status("7"))
            else:
                result = asyncio.run(provisioner_service.sync_instances(db))
                assert result == {"total": 1, "synced": 0, "errors": 1, "updates": []}

        assert db.row("instances", instance_id="7") == {"id": "1", "instance_id": "7", "status": "running"}

    @pytest.mark.parametrize(
        ("stored", "deployment", "written_meanwhile"),
        [
            # A provision claims a torn-down instance.
            ("deprovisioned", None, "provisioning"),
            # The owner stops an instance whose first boot just finished.
            ("provisioning", _deployment(desired=1, ready=1), "stopped"),
        ],
    )
    @pytest.mark.parametrize("trigger", ["dashboard", "bulk"])
    def test_status_written_during_the_kubernetes_check_is_kept(
        self, trigger: str, stored: str, deployment: dict[str, Any] | None, written_meanwhile: str
    ):
        """A status another writer records while the sync waits on Kubernetes is neither undone nor reported."""
        db = FakeSupabase({"instances": [{"id": "1", "instance_id": "7", "status": stored}]})
        kubectl = _fake_kubectl({} if deployment is None else {"7": deployment})

        async def write_during_check(cmd: list[str], **kwargs: Any) -> tuple[int, str, str]:  # noqa: ANN401
            db.row("instances", instance_id="7")["status"] = written_meanwhile
            return await kubectl(cmd, **kwargs)

        with (
            patch("backend.k8s.run_cmd", write_during_check),
            patch("backend.routes.instances.ensure_supabase", return_value=db),
        ):
            if trigger == "dashboard":
                asyncio.run(_background_sync_instance_status("7"))
            else:
                result = asyncio.run(provisioner_service.sync_instances(db))
                assert result == {"total": 1, "synced": 0, "errors": 0, "updates": []}

        assert db.row("instances", instance_id="7")["status"] == written_meanwhile

    def test_bulk_sync_reports_each_recorded_change(self):
        """The admin and system sync report the changes they recorded and count the instances they could not check."""
        db = FakeSupabase(
            {
                "instances": [
                    {"id": "1", "instance_id": 1, "status": "stopped"},
                    {"id": "2", "instance_id": 2, "status": "running"},
                    {"id": "3", "instance_id": 3, "status": "running"},
                    {"id": "4", "instance_id": 4, "status": "running"},
                ]
            }
        )
        kubectl = _fake_kubectl(
            {"1": _deployment(desired=1, ready=1), "2": _deployment(desired=1, ready=1)},
            unreachable=frozenset({"4"}),
        )

        with patch("backend.k8s.run_cmd", kubectl):
            result = asyncio.run(provisioner_service.sync_instances(db))

        assert result == {
            "total": 4,
            "synced": 2,
            "errors": 1,
            "updates": [
                {"instance_id": 1, "old_status": "stopped", "new_status": "running", "reason": "status_mismatch"},
                {"instance_id": 3, "old_status": "running", "new_status": "error", "reason": "deployment_not_found"},
            ],
        }
