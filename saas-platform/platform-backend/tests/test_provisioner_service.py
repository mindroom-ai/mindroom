"""Focused unit tests for the extracted provisioner service helpers."""

import base64
from unittest.mock import MagicMock, patch

import pytest
from backend.openrouter import CreatedOpenRouterKey
from backend.services import provisioner_service
from fastapi import HTTPException


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
            INSTANCE_MATRIX_OIDC_CLIENT_ID="mindroom-synapse",
        ):
            provisioner_service._append_matrix_oidc_helm_args(helm_args)

        set_pairs = [helm_args[i + 1] for i, arg in enumerate(helm_args) if arg == "--set"]
        set_string_pairs = [helm_args[i + 1] for i, arg in enumerate(helm_args) if arg == "--set-string"]
        assert "matrixOidc.enabled=true" in set_pairs
        assert "matrixOidc.issuer=https://api.mindroom.test/matrix-oidc" in set_pairs
        assert "matrixOidc.clientId=mindroom-synapse" in set_pairs
        assert "roomDefaults.joinPolicy=public" in set_pairs
        assert "roomDefaults.listed=false" in set_pairs
        assert set_string_pairs[0] == "matrixAutoJoinRoomKeys[0]=analysis"
        assert len(set_string_pairs) == len(provisioner_service._HOSTED_MATRIX_AUTO_JOIN_ROOM_KEYS)

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


class TestInstanceDashboardAuthGuard:
    """Tenant instances are never provisioned without a dashboard auth configuration."""

    def test_incomplete_supabase_configuration_blocks_provisioning(self):
        """A platform missing the anon key would provision unauthenticated tenant dashboards."""
        with (
            patch.multiple(
                provisioner_service,
                SUPABASE_URL="https://supabase.test",
                SUPABASE_ANON_KEY="",
                INSTANCE_TRUSTED_UPSTREAM_AUTH_ENABLED="",
            ),
            pytest.raises(HTTPException) as err,
        ):
            provisioner_service._require_instance_dashboard_auth()

        assert err.value.status_code == 503
        assert "SUPABASE_ANON_KEY" in err.value.detail

    @pytest.mark.asyncio
    async def test_provision_instance_rejects_before_any_database_or_helm_work(self):
        """The guard runs first, so an unauthenticatable tenant is never created or deployed."""
        sb = MagicMock()
        with (
            patch.multiple(provisioner_service, SUPABASE_ANON_KEY="", INSTANCE_TRUSTED_UPSTREAM_AUTH_ENABLED=""),
            patch.object(provisioner_service, "create_instance") as create_instance,
            patch.object(provisioner_service, "update_instance") as update_instance,
            patch.object(provisioner_service, "run_helm") as run_helm,
            pytest.raises(HTTPException) as err,
        ):
            await provisioner_service.provision_instance(
                sb,
                data={"subscription_id": "sub-1", "account_id": "acc-1", "tier": "byok"},
                background_tasks=None,
            )

        assert err.value.status_code == 503
        create_instance.assert_not_called()
        update_instance.assert_not_called()
        run_helm.assert_not_called()

    def test_complete_auth_configurations_allow_provisioning(self):
        """Either a full Supabase pair or trusted upstream auth is enough to provision."""
        with patch.multiple(
            provisioner_service,
            SUPABASE_URL="https://supabase.test",
            SUPABASE_ANON_KEY="anon-key",
            INSTANCE_TRUSTED_UPSTREAM_AUTH_ENABLED="",
        ):
            provisioner_service._require_instance_dashboard_auth()

        with patch.multiple(
            provisioner_service,
            SUPABASE_URL="",
            SUPABASE_ANON_KEY="",
            INSTANCE_TRUSTED_UPSTREAM_AUTH_ENABLED="true",
        ):
            provisioner_service._require_instance_dashboard_auth()

    def test_trusted_upstream_flag_parses_like_the_instance_chart(self):
        """A value the chart would not treat as enabled must not unlock provisioning either."""
        with (
            patch.multiple(
                provisioner_service,
                SUPABASE_URL="",
                SUPABASE_ANON_KEY="",
                INSTANCE_TRUSTED_UPSTREAM_AUTH_ENABLED=" true ",
            ),
            pytest.raises(HTTPException),
        ):
            provisioner_service._require_instance_dashboard_auth()
