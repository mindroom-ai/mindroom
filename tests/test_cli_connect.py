"""Tests for CLI connect helper functions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom import cli_connect
from mindroom.constants import OWNER_MATRIX_USER_ID_PLACEHOLDER

if TYPE_CHECKING:
    from pathlib import Path


def test_complete_local_pairing_accepts_owner_user_id_with_server_port() -> None:
    """Pair-complete should accept valid MXIDs that include a server port."""

    def _fake_post(url: str, **_kwargs: object) -> httpx.Response:
        assert url == "https://provisioning.example/v1/local-mindroom/pair/complete"
        return httpx.Response(
            200,
            json={
                "client_id": "client-123",
                "client_secret": "secret-123",
                "owner_user_id": "@alice:mindroom.chat:8448",
            },
        )

    result = cli_connect.complete_local_pairing(
        provisioning_url="https://provisioning.example",
        pair_code="ABCD-EFGH",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        post_request=_fake_post,
    )

    assert result.client_id == "client-123"
    assert result.client_secret == "secret-123"  # noqa: S105
    assert result.owner_user_id == "@alice:mindroom.chat:8448"
    assert result.owner_user_id_invalid is False


def test_persist_local_provisioning_env_writes_credentials_only(tmp_path: Path) -> None:
    """Persisted .env should contain provisioning credentials but not owner-user config."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\nagents: {}\nrouter:\n  model: default\n")

    env_path = cli_connect.persist_local_provisioning_env(
        provisioning_url="https://provisioning.example",
        client_id="client-123",
        client_secret="secret-123",  # noqa: S106
        config_path=config_path,
    )

    assert env_path == tmp_path / ".env"
    content = env_path.read_text()
    assert "MINDROOM_PROVISIONING_URL=https://provisioning.example" in content
    assert "MINDROOM_LOCAL_CLIENT_ID=client-123" in content
    assert "MINDROOM_LOCAL_CLIENT_SECRET=secret-123" in content
    assert "MINDROOM_OWNER_USER_ID=" not in content


def test_replace_owner_placeholders_in_config_accepts_server_port(tmp_path: Path) -> None:
    """Placeholder replacement should work for valid MXIDs with server ports."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "authorization:\n"
        "  global_users:\n"
        f"    - {OWNER_MATRIX_USER_ID_PLACEHOLDER}\n"
        "  agent_reply_permissions:\n"
        '    "*":\n'
        "      - __PLACEHOLDER__\n",
    )

    replaced = cli_connect.replace_owner_placeholders_in_config(
        config_path=config_path,
        owner_user_id="@alice:mindroom.chat:8448",
    )

    assert replaced is True
    updated = config_path.read_text()
    assert OWNER_MATRIX_USER_ID_PLACEHOLDER not in updated
    assert "__PLACEHOLDER__" not in updated
    assert "@alice:mindroom.chat:8448" in updated


def test_complete_local_pairing_rejects_non_json_response() -> None:
    """Pair-complete should fail with a clear error when response JSON is invalid."""

    def _fake_post(_url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    with pytest.raises(ValueError, match="invalid JSON"):
        cli_connect.complete_local_pairing(
            provisioning_url="https://provisioning.example",
            pair_code="ABCD-EFGH",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            post_request=_fake_post,
        )


def test_complete_local_pairing_rejects_non_object_json_response() -> None:
    """Pair-complete should reject JSON payloads that are not objects."""

    def _fake_post(_url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    with pytest.raises(TypeError, match="unexpected response"):
        cli_connect.complete_local_pairing(
            provisioning_url="https://provisioning.example",
            pair_code="ABCD-EFGH",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            post_request=_fake_post,
        )


def test_complete_local_pairing_flags_malformed_owner_user_id() -> None:
    """Malformed owner_user_id should be ignored and flagged for caller warnings."""

    def _fake_post(_url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "client_id": "client-123",
                "client_secret": "secret-123",
                "owner_user_id": "not-a-mxid",
            },
        )

    result = cli_connect.complete_local_pairing(
        provisioning_url="https://provisioning.example",
        pair_code="ABCD-EFGH",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        post_request=_fake_post,
    )

    assert result.owner_user_id is None
    assert result.owner_user_id_invalid is True
