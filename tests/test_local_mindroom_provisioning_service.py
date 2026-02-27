"""Tests for the standalone local provisioning service script."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import scripts.local_mindroom_provisioning_service as provisioning

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


def _service_config(state_path: Path) -> provisioning.ServiceConfig:
    return provisioning.ServiceConfig(
        matrix_homeserver="https://mindroom.chat",
        matrix_ssl_verify=True,
        matrix_registration_token="server-secret-token",  # noqa: S106
        state_path=state_path,
        pair_code_ttl_seconds=600,
        registration_token_ttl_seconds=300,
        pair_poll_interval_seconds=3,
        cors_origins=["https://chat.mindroom.chat"],
        listen_host="127.0.0.1",
        listen_port=8776,
    )


@pytest.fixture(autouse=True)
def _reset_state() -> Generator[None, None, None]:
    provisioning._clear_state_unlocked()
    provisioning._rate_limit_buckets.clear()
    yield
    provisioning._clear_state_unlocked()
    provisioning._rate_limit_buckets.clear()


def _patch_matrix_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    token_to_user = {
        "token-alice": "@alice:mindroom.chat",
        "token-bob": "@bob:mindroom.chat",
    }

    async def _fake_matrix_whoami(config: provisioning.ServiceConfig, access_token: str) -> str:
        del config
        user_id = token_to_user.get(access_token)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid Matrix access token")
        return user_id

    monkeypatch.setattr(provisioning, "_matrix_whoami", _fake_matrix_whoami)


def test_pairing_and_token_issue_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end happy path: pair -> complete -> issue token -> revoke."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        start = client.post(
            "/v1/local-mindroom/pair/start",
            headers={"Authorization": "Bearer token-alice"},
        )
        assert start.status_code == 200
        pair_code = start.json()["pair_code"]

        pending = client.get(
            "/v1/local-mindroom/pair/status",
            params={"pair_code": pair_code},
            headers={"Authorization": "Bearer token-alice"},
        )
        assert pending.status_code == 200
        assert pending.json()["status"] == "pending"

        complete = client.post(
            "/v1/local-mindroom/pair/complete",
            json={
                "pair_code": pair_code,
                "client_name": "alice-macbook",
                "client_pubkey_or_fingerprint": "sha256:abc123",
            },
        )
        assert complete.status_code == 200
        payload = complete.json()
        client_id = payload["client_id"]
        client_secret = payload["client_secret"]

        connected = client.get(
            "/v1/local-mindroom/pair/status",
            params={"pair_code": pair_code},
            headers={"Authorization": "Bearer token-alice"},
        )
        assert connected.status_code == 200
        assert connected.json()["status"] == "connected"

        issue = client.post(
            "/v1/local-mindroom/tokens/issue",
            json={"purpose": "register_agent", "agent_hint": "code"},
            headers={
                "X-Local-MindRoom-Client-Id": client_id,
                "X-Local-MindRoom-Client-Secret": client_secret,
            },
        )
        assert issue.status_code == 200
        assert issue.json()["registration_token"] == "server-secret-token"  # noqa: S105

        revoke = client.delete(
            f"/v1/local-mindroom/connections/{client_id}",
            headers={"Authorization": "Bearer token-alice"},
        )
        assert revoke.status_code == 200
        assert revoke.json()["revoked"] is True

        issue_after_revoke = client.post(
            "/v1/local-mindroom/tokens/issue",
            json={"purpose": "register_agent"},
            headers={
                "X-Local-MindRoom-Client-Id": client_id,
                "X-Local-MindRoom-Client-Secret": client_secret,
            },
        )
        assert issue_after_revoke.status_code == 403


def test_browser_auth_required_for_pair_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pair start should reject requests without browser Matrix auth token."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        result = client.post("/v1/local-mindroom/pair/start")
        assert result.status_code == 401


def test_state_persists_between_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Connections should survive process restarts via JSON state file."""
    _patch_matrix_auth(monkeypatch)
    state_path = tmp_path / "state.json"

    with TestClient(provisioning.create_app(_service_config(state_path))) as client:
        start = client.post(
            "/v1/local-mindroom/pair/start",
            headers={"Authorization": "Bearer token-alice"},
        )
        pair_code = start.json()["pair_code"]
        complete = client.post(
            "/v1/local-mindroom/pair/complete",
            json={
                "pair_code": pair_code,
                "client_name": "alice-linux",
                "client_pubkey_or_fingerprint": "sha256:def456",
            },
        )
        assert complete.status_code == 200

    with TestClient(provisioning.create_app(_service_config(state_path))) as restarted_client:
        listed = restarted_client.get(
            "/v1/local-mindroom/connections",
            headers={"Authorization": "Bearer token-alice"},
        )
        assert listed.status_code == 200
        assert len(listed.json()["connections"]) == 1
