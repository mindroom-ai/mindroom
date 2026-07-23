"""Focused unit tests for the extracted provisioner service helpers."""

import base64
from unittest.mock import MagicMock, patch

from backend.openrouter import CreatedOpenRouterKey
from backend.services import provisioner_service


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
        assert "matrixRoomAccess.mode=multi_user" in set_pairs
        assert "matrixRoomAccess.multiUserJoinRule=public" in set_pairs
        assert "matrixRoomAccess.publishToRoomDirectory=false" in set_pairs
        assert "matrixRoomAccess.reconcileExistingRooms=true" in set_pairs
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
