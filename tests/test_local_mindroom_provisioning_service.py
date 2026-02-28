"""Tests for the standalone local provisioning service script."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import scripts.local_mindroom_provisioning_service as provisioning

if TYPE_CHECKING:
    from pathlib import Path


def _service_config(state_path: Path) -> provisioning.ServiceConfig:
    return provisioning.ServiceConfig(
        matrix_homeserver="https://mindroom.chat",
        matrix_server_name="mindroom.chat",
        matrix_ssl_verify=True,
        matrix_registration_token="server-secret-token",  # noqa: S106
        state_path=state_path,
        pair_code_ttl_seconds=600,
        pair_poll_interval_seconds=3,
        cors_origins=["https://chat.mindroom.chat"],
        listen_host="127.0.0.1",
        listen_port=8776,
    )


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


def test_pairing_and_register_agent_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end happy path: pair -> complete -> register agent -> revoke."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    async def _fake_register(
        config: provisioning.ServiceConfig,
        payload: provisioning.RegisterAgentRequest,
    ) -> provisioning.RegisterAgentResponse:
        del config
        return provisioning.RegisterAgentResponse(
            status="created",
            user_id=f"@{payload.username}:mindroom.chat",
        )

    monkeypatch.setattr(provisioning, "_register_agent_with_matrix", _fake_register)

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
        assert payload["owner_user_id"] == "@alice:mindroom.chat"

        connected = client.get(
            "/v1/local-mindroom/pair/status",
            params={"pair_code": pair_code},
            headers={"Authorization": "Bearer token-alice"},
        )
        assert connected.status_code == 200
        assert connected.json()["status"] == "connected"

        register = client.post(
            "/v1/local-mindroom/register-agent",
            json={
                "homeserver": "https://mindroom.chat",
                "username": "mindroom_code",
                "password": "agent-pass-123",
                "display_name": "CodeAgent",
            },
            headers={
                "X-Local-MindRoom-Client-Id": client_id,
                "X-Local-MindRoom-Client-Secret": client_secret,
            },
        )
        assert register.status_code == 200
        assert register.json()["status"] == "created"
        assert register.json()["user_id"] == "@mindroom_code:mindroom.chat"

        revoke = client.delete(
            f"/v1/local-mindroom/connections/{client_id}",
            headers={"Authorization": "Bearer token-alice"},
        )
        assert revoke.status_code == 200
        assert revoke.json()["revoked"] is True

        register_after_revoke = client.post(
            "/v1/local-mindroom/register-agent",
            json={
                "homeserver": "https://mindroom.chat",
                "username": "mindroom_other",
                "password": "agent-pass-123",
                "display_name": "OtherAgent",
            },
            headers={
                "X-Local-MindRoom-Client-Id": client_id,
                "X-Local-MindRoom-Client-Secret": client_secret,
            },
        )
        assert register_after_revoke.status_code == 403


def test_register_agent_validates_homeserver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Register-agent should reject homeserver mismatches."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    async def _fake_register(
        config: provisioning.ServiceConfig,
        payload: provisioning.RegisterAgentRequest,
    ) -> provisioning.RegisterAgentResponse:
        del config, payload
        return provisioning.RegisterAgentResponse(status="created", user_id="@mindroom_code:mindroom.chat")

    monkeypatch.setattr(provisioning, "_register_agent_with_matrix", _fake_register)

    with TestClient(app) as client:
        pair_code = client.post(
            "/v1/local-mindroom/pair/start",
            headers={"Authorization": "Bearer token-alice"},
        ).json()["pair_code"]
        complete = client.post(
            "/v1/local-mindroom/pair/complete",
            json={
                "pair_code": pair_code,
                "client_name": "alice-macbook",
                "client_pubkey_or_fingerprint": "sha256:abc123",
            },
        ).json()

        register = client.post(
            "/v1/local-mindroom/register-agent",
            json={
                "homeserver": "https://other.example",
                "username": "mindroom_code",
                "password": "agent-pass-123",
                "display_name": "CodeAgent",
            },
            headers={
                "X-Local-MindRoom-Client-Id": complete["client_id"],
                "X-Local-MindRoom-Client-Secret": complete["client_secret"],
            },
        )
        assert register.status_code == 400


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


@pytest.mark.asyncio
async def test_register_agent_user_in_use_respects_matrix_server_name_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-in-use should return user_id on configured MATRIX_SERVER_NAME domain."""
    config = provisioning.ServiceConfig(
        matrix_homeserver="https://internal-matrix:8448",
        matrix_server_name="mindroom.chat",
        matrix_ssl_verify=True,
        matrix_registration_token="server-secret-token",  # noqa: S106
        state_path=tmp_path / "state.json",
        pair_code_ttl_seconds=600,
        pair_poll_interval_seconds=3,
        cors_origins=["https://chat.mindroom.chat"],
        listen_host="127.0.0.1",
        listen_port=8776,
    )

    class _FakeResponse:
        status_code = 400
        is_success = False
        text = "M_USER_IN_USE"

        @staticmethod
        def json() -> dict[str, str]:
            return {
                "errcode": "M_USER_IN_USE",
                "error": "User ID already taken",
            }

    class _FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            del exc_type, exc, tb

        async def post(
            self,
            url: str,
            *,
            json: dict[str, object],
            headers: dict[str, str] | None = None,
        ) -> _FakeResponse:
            del url, json, headers
            return _FakeResponse()

    monkeypatch.setattr(provisioning.httpx, "AsyncClient", _FakeAsyncClient)
    payload = provisioning.RegisterAgentRequest(
        homeserver="https://internal-matrix:8448",
        username="mindroom_code",
        password="agent-pass",  # noqa: S106
        display_name="CodeAgent",
    )

    result = await provisioning._register_agent_with_matrix(config, payload)
    assert result.status == "user_in_use"
    assert result.user_id == "@mindroom_code:mindroom.chat"
