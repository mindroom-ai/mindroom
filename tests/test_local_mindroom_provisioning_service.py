"""Tests for the standalone local provisioning service script."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Self
from urllib.parse import urlparse

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import scripts.local_mindroom_provisioning_service as provisioning
from mindroom.cli import connect as cli_connect
from mindroom.matrix import provisioning as matrix_provisioning
from tests.test_cli_connect import _CONNECTED, _START, _fake_transport

if TYPE_CHECKING:
    from pathlib import Path


def _service_config(
    state_path: Path,
    *,
    google_oauth_client_id: str | None = None,
    google_oauth_client_secret: str | None = None,
) -> provisioning.ServiceConfig:
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
        google_oauth_client_id=google_oauth_client_id,
        google_oauth_client_secret=google_oauth_client_secret,
    )


@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [("google-client-id", None), (None, "google-client-secret")],
)
def test_service_config_rejects_partial_google_oauth_client(
    monkeypatch: pytest.MonkeyPatch,
    client_id: str | None,
    client_secret: str | None,
) -> None:
    """The provisioning service fails at startup when only half the Google client is configured."""
    monkeypatch.setenv("MATRIX_REGISTRATION_TOKEN", "server-secret-token")
    monkeypatch.delenv("MATRIX_REGISTRATION_TOKEN_FILE", raising=False)
    monkeypatch.delenv("MINDROOM_GOOGLE_OAUTH_CLIENT_SECRET_FILE", raising=False)
    if client_id is None:
        monkeypatch.delenv("MINDROOM_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    else:
        monkeypatch.setenv("MINDROOM_GOOGLE_OAUTH_CLIENT_ID", client_id)
    if client_secret is None:
        monkeypatch.delenv("MINDROOM_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    else:
        monkeypatch.setenv("MINDROOM_GOOGLE_OAUTH_CLIENT_SECRET", client_secret)

    with pytest.raises(ValueError, match="must be configured together"):
        provisioning._load_service_config_from_env()


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


def _managed_agent_username(entity_name: str, namespace: str) -> str:
    return f"{provisioning.MANAGED_AGENT_USERNAME_PREFIX}{entity_name}_{namespace}"


def _invalid_managed_agent_username(case: str, namespace: str) -> str:
    if case == "missing_entity_between_prefix_and_namespace":
        return _managed_agent_username("", namespace)
    if case == "wrong_namespace_suffix":
        return f"{_managed_agent_username('code', namespace)}x"
    if case == "wrong_prefix_for_namespace":
        return f"other_code_{namespace}"
    if case == "plain_username_without_namespace":
        return f"{provisioning.MANAGED_AGENT_USERNAME_PREFIX}foo"
    if case == "invalid_localpart_for_namespace":
        return _managed_agent_username("Foo", namespace)
    msg = f"Unknown invalid username case: {case}"
    raise ValueError(msg)


def _pair_local_client(client: TestClient) -> dict[str, str]:
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
    )
    assert complete.status_code == 200
    return complete.json()


def _set_connection_namespace(state_path: Path, connection_id: str, namespace: str | None) -> None:
    """Edit the persisted state file the way an operator would (service stopped)."""
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    for item in payload["connections"]:
        if item["id"] == connection_id:
            if namespace is None:
                item.pop("namespace", None)
            else:
                item["namespace"] = namespace
    state_path.write_text(json.dumps(payload), encoding="utf-8")


def _install_fake_register(monkeypatch: pytest.MonkeyPatch, register_calls: list[str]) -> None:
    async def _fake_register(
        config: provisioning.ServiceConfig,
        payload: provisioning.RegisterAgentRequest,
    ) -> provisioning.RegisterAgentResponse:
        del config
        register_calls.append(payload.username)
        return provisioning.RegisterAgentResponse(
            status="created",
            user_id=f"@{payload.username}:mindroom.chat",
        )

    monkeypatch.setattr(provisioning, "_register_agent_with_matrix", _fake_register)


def _post_register_agent(client: TestClient, complete: dict[str, str], username: str) -> httpx.Response:
    return client.post(
        "/v1/local-mindroom/register-agent",
        json={
            "homeserver": "https://mindroom.chat",
            "username": username,
            "password": "agent-pass-123",
            "display_name": "CodeAgent",
        },
        headers={
            "X-Local-MindRoom-Client-Id": complete["client_id"],
            "X-Local-MindRoom-Client-Secret": complete["client_secret"],
        },
    )


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
        start_payload = start.json()
        pair_code = start_payload["pair_code"]
        pair_session_id = start_payload["pair_session_id"]

        pending = client.get(
            "/v1/local-mindroom/pair/status",
            headers={
                "Authorization": "Bearer token-alice",
                provisioning.PAIR_STATUS_SESSION_HEADER: pair_session_id,
            },
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
        assert isinstance(payload["namespace"], str)
        assert len(payload["namespace"]) == 8
        assert payload["namespace"] == payload["connection"]["namespace"]
        agent_username = _managed_agent_username("code", payload["namespace"])

        connected = client.get(
            "/v1/local-mindroom/pair/status",
            headers={
                "Authorization": "Bearer token-alice",
                provisioning.PAIR_STATUS_SESSION_HEADER: pair_session_id,
            },
        )
        assert connected.status_code == 200
        assert connected.json()["status"] == "connected"

        register = client.post(
            "/v1/local-mindroom/register-agent",
            json={
                "homeserver": "https://mindroom.chat",
                "username": agent_username,
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
        assert register.json()["user_id"] == f"@{agent_username}:mindroom.chat"

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
                "username": _managed_agent_username("other", payload["namespace"]),
                "password": "agent-pass-123",
                "display_name": "OtherAgent",
            },
            headers={
                "X-Local-MindRoom-Client-Id": client_id,
                "X-Local-MindRoom-Client-Secret": client_secret,
            },
        )
        assert register_after_revoke.status_code == 403


def test_paired_client_fetches_google_oauth_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an authenticated paired runtime receives the installed-app client."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(
        _service_config(
            tmp_path / "state.json",
            google_oauth_client_id="google-client-id",
            google_oauth_client_secret="google-client-secret",  # noqa: S106
        ),
    )

    with TestClient(app) as client:
        complete = _pair_local_client(client)
        missing_auth = client.get("/v1/local-mindroom/oauth/google-client")
        response = client.get(
            "/v1/local-mindroom/oauth/google-client",
            headers={
                "X-Local-MindRoom-Client-Id": complete["client_id"],
                "X-Local-MindRoom-Client-Secret": complete["client_secret"],
            },
        )

    assert missing_auth.status_code == 401
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "client_id": "google-client-id",
        "client_secret": "google-client-secret",
    }


def test_google_oauth_client_endpoint_requires_server_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A paired client receives an explicit error when the server has no Google app."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        complete = _pair_local_client(client)
        response = client.get(
            "/v1/local-mindroom/oauth/google-client",
            headers={
                "X-Local-MindRoom-Client-Id": complete["client_id"],
                "X-Local-MindRoom-Client-Secret": complete["client_secret"],
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Google OAuth client is not configured"


def test_pair_status_accepts_session_header_without_pair_code_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pair status polling should not require putting the pair code in the URL."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        start = client.post(
            "/v1/local-mindroom/pair/start",
            headers={"Authorization": "Bearer token-alice"},
        )
        assert start.status_code == 200
        pair_session_id = start.json()["pair_session_id"]

        pending = client.get(
            "/v1/local-mindroom/pair/status",
            headers={
                "Authorization": "Bearer token-alice",
                provisioning.PAIR_STATUS_SESSION_HEADER: pair_session_id,
            },
        )

        assert pending.status_code == 200
        assert pending.json()["status"] == "pending"


def test_pair_status_rejects_pair_code_query_without_session_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pair status polling should not accept the short pair code in the URL."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        start = client.post(
            "/v1/local-mindroom/pair/start",
            headers={"Authorization": "Bearer token-alice"},
        )
        assert start.status_code == 200

        pending = client.get(
            "/v1/local-mindroom/pair/status",
            params={"pair_code": start.json()["pair_code"]},
            headers={"Authorization": "Bearer token-alice"},
        )

        assert pending.status_code == 400
        assert pending.json()["detail"] == "Missing pair session id"


def test_pair_status_rejects_missing_session_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pair status should require the opaque session header."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        result = client.get(
            "/v1/local-mindroom/pair/status",
            headers={"Authorization": "Bearer token-alice"},
        )

        assert result.status_code == 400
        assert result.json()["detail"] == "Missing pair session id"


@pytest.mark.parametrize(
    ("invalid_username_case", "expected_status"),
    [
        ("missing_entity_between_prefix_and_namespace", 403),
        ("wrong_namespace_suffix", 403),
        ("wrong_prefix_for_namespace", 403),
        ("plain_username_without_namespace", 403),
        ("invalid_localpart_for_namespace", 400),
    ],
)
def test_register_agent_rejects_username_outside_connection_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_username_case: str,
    expected_status: int,
) -> None:
    """A local client must not register Matrix users outside its assigned namespace."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))
    register_calls: list[str] = []

    async def _fake_register(
        config: provisioning.ServiceConfig,
        payload: provisioning.RegisterAgentRequest,
    ) -> provisioning.RegisterAgentResponse:
        del config
        register_calls.append(payload.username)
        return provisioning.RegisterAgentResponse(
            status="created",
            user_id=f"@{payload.username}:mindroom.chat",
        )

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
        namespace = complete["namespace"]

        register = client.post(
            "/v1/local-mindroom/register-agent",
            json={
                "homeserver": "https://mindroom.chat",
                "username": _invalid_managed_agent_username(invalid_username_case, namespace),
                "password": "agent-pass-123",
                "display_name": "CodeAgent",
            },
            headers={
                "X-Local-MindRoom-Client-Id": complete["client_id"],
                "X-Local-MindRoom-Client-Secret": complete["client_secret"],
            },
        )

        assert register.status_code == expected_status
        if expected_status == 403:
            assert register.json()["detail"] == "Requested username is outside this local connection namespace"
        else:
            assert "not a valid Matrix localpart" in register.json()["detail"]
        assert register_calls == []


def test_register_agent_allows_plain_username_for_namespace_exempt_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection with operator-set namespace "" may register plain mindroom_<entity> usernames."""
    _patch_matrix_auth(monkeypatch)
    state_path = tmp_path / "state.json"
    register_calls: list[str] = []
    _install_fake_register(monkeypatch, register_calls)

    with TestClient(provisioning.create_app(_service_config(state_path))) as client:
        complete = _pair_local_client(client)

    _set_connection_namespace(state_path, complete["client_id"], "")

    with TestClient(provisioning.create_app(_service_config(state_path))) as client:
        register = _post_register_agent(client, complete, "mindroom_foo")

        assert register.status_code == 200
        assert register.json()["status"] == "created"
        assert register.json()["user_id"] == "@mindroom_foo:mindroom.chat"
        assert register_calls == ["mindroom_foo"]


@pytest.mark.parametrize(
    ("username", "expected_status"),
    [
        ("other_foo", 403),  # missing managed prefix
        ("mindroom_", 403),  # bare prefix without entity
        ("mindroom_Foo", 400),  # invalid Matrix localpart (uppercase)
    ],
)
def test_register_agent_namespace_exempt_connection_still_requires_managed_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    username: str,
    expected_status: int,
) -> None:
    """Namespace-exempt connections still only get mindroom_-prefixed valid localparts."""
    _patch_matrix_auth(monkeypatch)
    state_path = tmp_path / "state.json"
    register_calls: list[str] = []
    _install_fake_register(monkeypatch, register_calls)

    with TestClient(provisioning.create_app(_service_config(state_path))) as client:
        complete = _pair_local_client(client)

    _set_connection_namespace(state_path, complete["client_id"], "")

    with TestClient(provisioning.create_app(_service_config(state_path))) as client:
        register = _post_register_agent(client, complete, username)

        assert register.status_code == expected_status
        assert register_calls == []


@pytest.mark.parametrize("corrupt_namespace", [None, "null", " "], ids=["missing_key", "json_null", "whitespace"])
def test_state_load_fails_closed_for_missing_or_blank_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt_namespace: str | None,
) -> None:
    """Only a literal "" exempts; missing, null, or whitespace namespaces must fail closed to a derived one."""
    _patch_matrix_auth(monkeypatch)
    state_path = tmp_path / "state.json"
    register_calls: list[str] = []
    _install_fake_register(monkeypatch, register_calls)

    with TestClient(provisioning.create_app(_service_config(state_path))) as client:
        complete = _pair_local_client(client)

    if corrupt_namespace == "null":
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload["connections"][0]["namespace"] = None
        state_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        _set_connection_namespace(state_path, complete["client_id"], corrupt_namespace)

    with TestClient(provisioning.create_app(_service_config(state_path))) as client:
        register = _post_register_agent(client, complete, "mindroom_foo")
        assert register.status_code == 403
        assert register_calls == []

        listed = client.get(
            "/v1/local-mindroom/connections",
            headers={"Authorization": "Bearer token-alice"},
        )
        derived = listed.json()["connections"][0]["namespace"]
        assert isinstance(derived, str)
        assert derived.strip() != ""


def test_state_round_trip_preserves_empty_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator-set empty namespace must survive load and re-persist cycles."""
    _patch_matrix_auth(monkeypatch)
    state_path = tmp_path / "state.json"
    register_calls: list[str] = []
    _install_fake_register(monkeypatch, register_calls)

    with TestClient(provisioning.create_app(_service_config(state_path))) as client:
        complete = _pair_local_client(client)

    _set_connection_namespace(state_path, complete["client_id"], "")

    with TestClient(provisioning.create_app(_service_config(state_path))) as client:
        listed = client.get(
            "/v1/local-mindroom/connections",
            headers={"Authorization": "Bearer token-alice"},
        )
        assert listed.status_code == 200
        assert listed.json()["connections"][0]["namespace"] == ""

        # Registering updates last_seen_at and re-persists state to disk.
        assert _post_register_agent(client, complete, "mindroom_foo").status_code == 200

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert [item["namespace"] for item in persisted["connections"]] == [""]

    with TestClient(provisioning.create_app(_service_config(state_path))) as client:
        assert _post_register_agent(client, complete, "mindroom_bar").status_code == 200


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
        assert isinstance(listed.json()["connections"][0]["namespace"], str)


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


def test_client_error_detail_constants_match_service() -> None:
    """The runtime client classifies register-agent 403s by these exact strings."""
    assert matrix_provisioning._CONNECTION_REVOKED_DETAIL == provisioning.CONNECTION_REVOKED_DETAIL
    assert matrix_provisioning._NAMESPACE_MISMATCH_DETAIL == provisioning.NAMESPACE_MISMATCH_DETAIL


def test_cli_device_pairing_messages_match_service_models(tmp_path: Path) -> None:
    """The CLI's device-pairing requests and its response fixtures satisfy the service schemas."""
    calls: list[tuple[str, dict[str, object]]] = []
    post = _fake_transport([httpx.Response(200, json=_START), httpx.Response(200, json=_CONNECTED)], calls)

    cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint=cli_connect.local_client_fingerprint(config_path=tmp_path / "config.yaml"),
        matrix_ssl_verify=True,
        announce=lambda _session: None,
        post_request=post,
        sleep=lambda _seconds: None,
    )

    (start_url, start_payload), (poll_url, poll_payload) = calls
    service_paths = {route.path for route in provisioning.create_app(_service_config(tmp_path / "state.json")).routes}
    assert urlparse(start_url).path in service_paths
    assert urlparse(poll_url).path in service_paths
    provisioning.DevicePairStartRequest.model_validate(start_payload)
    provisioning.DevicePairPollRequest.model_validate(poll_payload)
    provisioning.DevicePairStartResponse.model_validate(_START)
    provisioning.DevicePairPollResponse.model_validate(_CONNECTED)


def test_device_start_retries_colliding_pair_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A generated pair code already held by a live session is replaced before it is handed out."""
    codes = iter(["AAAA-BBBB", "AAAA-BBBB", "CCCC-DDDD"])
    monkeypatch.setattr(provisioning, "_generate_pair_code", lambda: next(codes))
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        first = _start_device_pairing(client)
        second = _start_device_pairing(client)

    assert first["pair_code"] == "AAAA-BBBB"
    assert second["pair_code"] == "CCCC-DDDD"


def _start_device_pairing(client: TestClient, client_name: str = "alice-macbook") -> dict[str, object]:
    response = client.post(
        "/v1/local-mindroom/pair/device/start",
        json={"client_name": client_name, "client_pubkey_or_fingerprint": "sha256:abc123"},
    )
    assert response.status_code == 200
    return response.json()


def test_device_pairing_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A local client starts, a browser user approves, and the first poll returns credentials once."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        pair_code = started["pair_code"]
        assert started["approve_url"] == f"https://chat.mindroom.chat/connect?code={pair_code}"
        assert started["poll_interval_seconds"] == 3

        pending = client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": started["device_secret"]})
        assert pending.json()["status"] == "pending"

        inspected = client.post(
            "/v1/local-mindroom/pair/device/inspect",
            json={"pair_code": pair_code},
            headers={"Authorization": "Bearer token-alice"},
        )
        assert inspected.status_code == 200
        assert inspected.json()["client_name"] == "alice-macbook"
        assert inspected.json()["status"] == "pending"

        approved = client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": pair_code},
            headers={"Authorization": "Bearer token-alice"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        connected = client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": started["device_secret"]})
        body = connected.json()
        assert body["status"] == "connected"
        assert body["owner_user_id"] == "@alice:mindroom.chat"
        assert body["client_id"] == body["connection"]["id"]
        assert body["connection"]["client_name"] == "alice-macbook"

        claimed_again = client.post(
            "/v1/local-mindroom/pair/device/poll",
            json={"device_secret": started["device_secret"]},
        )
        assert claimed_again.status_code == 410
        assert claimed_again.json()["detail"] == "Pair session already claimed"

        listed = client.get("/v1/local-mindroom/connections", headers={"Authorization": "Bearer token-alice"})
        assert [item["id"] for item in listed.json()["connections"]] == [body["client_id"]]


def test_device_pairing_credentials_register_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials from device pairing authenticate register-agent like browser-issued ones."""
    _patch_matrix_auth(monkeypatch)
    register_calls: list[str] = []
    _install_fake_register(monkeypatch, register_calls)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": started["pair_code"]},
            headers={"Authorization": "Bearer token-alice"},
        )
        body = client.post(
            "/v1/local-mindroom/pair/device/poll",
            json={"device_secret": started["device_secret"]},
        ).json()
        response = _post_register_agent(client, body, _managed_agent_username("code", body["namespace"]))

    assert response.status_code == 200
    assert register_calls == [_managed_agent_username("code", body["namespace"])]


def test_device_approve_requires_matrix_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a signed-in Matrix user may inspect or approve a device code."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        for path in ("inspect", "approve"):
            response = client.post(f"/v1/local-mindroom/pair/device/{path}", json={"pair_code": started["pair_code"]})
            assert response.status_code == 401


def test_device_code_approved_by_one_user_cannot_be_taken_by_another(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second user cannot re-approve a code another user already approved."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        code = {"pair_code": started["pair_code"]}
        assert (
            client.post(
                "/v1/local-mindroom/pair/device/approve",
                json=code,
                headers={"Authorization": "Bearer token-alice"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/local-mindroom/pair/device/approve",
                json=code,
                headers={"Authorization": "Bearer token-alice"},
            ).status_code
            == 200
        )
        taken = client.post(
            "/v1/local-mindroom/pair/device/approve",
            json=code,
            headers={"Authorization": "Bearer token-bob"},
        )
        assert taken.status_code == 409
        assert taken.json()["detail"] == "Pair code already approved"


def test_device_code_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired device codes can neither be approved nor polled into credentials."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        later = provisioning._now_utc() + provisioning.timedelta(seconds=601)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: later)

        approve = client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": started["pair_code"]},
            headers={"Authorization": "Bearer token-alice"},
        )
        assert approve.status_code == 410
        poll = client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": started["device_secret"]})
        assert poll.json()["status"] == "expired"


def test_device_code_cannot_complete_through_browser_initiated_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy pair/complete endpoint must not hand out credentials for a device code."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        complete = client.post(
            "/v1/local-mindroom/pair/complete",
            json={
                "pair_code": started["pair_code"],
                "client_name": "attacker",
                "client_pubkey_or_fingerprint": "sha256:evil",
            },
        )
        assert complete.status_code == 404


def test_browser_initiated_code_cannot_be_approved_as_device_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Device approval only accepts codes created by the device flow."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        pair_code = client.post(
            "/v1/local-mindroom/pair/start",
            headers={"Authorization": "Bearer token-alice"},
        ).json()["pair_code"]
        response = client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": pair_code},
            headers={"Authorization": "Bearer token-alice"},
        )
        assert response.status_code == 404


def test_device_poll_rejects_unknown_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Polling with a secret the service never issued fails without revealing sessions."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        response = client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": "not-issued"})
        assert response.status_code == 404


def test_device_start_is_rate_limited_per_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unauthenticated device starts are limited per client address."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        for _ in range(10):
            _start_device_pairing(client)
        response = client.post(
            "/v1/local-mindroom/pair/device/start",
            json={"client_name": "x", "client_pubkey_or_fingerprint": "sha256:x"},
        )
        assert response.status_code == 429


def test_approved_device_session_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An approval persisted before a restart can still be claimed after it."""
    _patch_matrix_auth(monkeypatch)
    state_path = tmp_path / "state.json"

    with TestClient(provisioning.create_app(_service_config(state_path))) as client:
        started = _start_device_pairing(client)
        client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": started["pair_code"]},
            headers={"Authorization": "Bearer token-alice"},
        )

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert started["device_secret"] not in json.dumps(persisted)

    with TestClient(provisioning.create_app(_service_config(state_path))) as restarted:
        body = restarted.post(
            "/v1/local-mindroom/pair/device/poll",
            json={"device_secret": started["device_secret"]},
        ).json()
        assert body["status"] == "connected"
        assert body["owner_user_id"] == "@alice:mindroom.chat"


def test_service_config_reads_approve_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators can point approval links at their own chat client."""
    monkeypatch.setenv("MATRIX_REGISTRATION_TOKEN", "server-secret-token")
    monkeypatch.setenv("MINDROOM_PROVISIONING_APPROVE_URL", "https://chat.example.org/connect/")
    for name in ("MINDROOM_GOOGLE_OAUTH_CLIENT_ID", "MINDROOM_GOOGLE_OAUTH_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)

    assert provisioning._load_service_config_from_env().approve_url == "https://chat.example.org/connect"


def test_expired_pair_sessions_are_pruned_on_new_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Old expired sessions are removed when new pairing starts."""
    _patch_matrix_auth(monkeypatch)
    state_path = tmp_path / "state.json"
    app = provisioning.create_app(_service_config(state_path))

    with TestClient(app) as client:
        old_browser = client.post(
            "/v1/local-mindroom/pair/start",
            headers={"Authorization": "Bearer token-alice"},
        ).json()
        old_device = _start_device_pairing(client, "old-machine")

        later = provisioning._now_utc() + provisioning.timedelta(seconds=601)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: later)

        _start_device_pairing(client, "new-machine")

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    session_ids = [s["id"] for s in persisted["pair_sessions"]]
    assert old_browser["pair_session_id"] not in session_ids
    assert len(persisted["pair_sessions"]) == 1
    assert persisted["pair_sessions"][0]["client_name"] == "new-machine"

    with TestClient(app) as restarted:
        assert (
            restarted.post(
                "/v1/local-mindroom/pair/complete",
                json={"pair_code": old_browser["pair_code"], "client_name": "x", "client_pubkey_or_fingerprint": "x"},
            ).status_code
            == 404
        )
        assert (
            restarted.post(
                "/v1/local-mindroom/pair/device/poll",
                json={"device_secret": old_device["device_secret"]},
            ).status_code
            == 404
        )


def test_approve_extends_claim_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Approval near expiry extends the window so CLI can still claim credentials."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        near_expiry = provisioning._now_utc() + provisioning.timedelta(seconds=595)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: near_expiry)

        client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": started["pair_code"]},
            headers={"Authorization": "Bearer token-alice"},
        )

        after_original_ttl = near_expiry + provisioning.timedelta(seconds=10)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: after_original_ttl)

        body = client.post(
            "/v1/local-mindroom/pair/device/poll",
            json={"device_secret": started["device_secret"]},
        ).json()
        assert body["status"] == "connected"


def test_poll_expires_after_grace_period(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll after grace period returns expired."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        near_expiry = provisioning._now_utc() + provisioning.timedelta(seconds=595)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: near_expiry)

        client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": started["pair_code"]},
            headers={"Authorization": "Bearer token-alice"},
        )

        past_grace = near_expiry + provisioning.timedelta(seconds=70)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: past_grace)

        body = client.post(
            "/v1/local-mindroom/pair/device/poll",
            json={"device_secret": started["device_secret"]},
        ).json()
        assert body["status"] == "expired"


def test_approve_and_inspect_reject_connected_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After credentials are claimed, approve and inspect return 409."""
    _patch_matrix_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": started["pair_code"]},
            headers={"Authorization": "Bearer token-alice"},
        )
        client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": started["device_secret"]})

        for endpoint in ("approve", "inspect"):
            response = client.post(
                f"/v1/local-mindroom/pair/device/{endpoint}",
                json={"pair_code": started["pair_code"]},
                headers={"Authorization": "Bearer token-alice"},
            )
            assert response.status_code == 409
            assert response.json()["detail"] == "Pair code already used"


def test_legacy_state_loads_browser_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """State files written before device pairing load sessions as browser-initiated."""
    _patch_matrix_auth(monkeypatch)
    state_path = tmp_path / "state.json"

    legacy_payload = {
        "pair_sessions": [
            {
                "id": "legacy-session-id",
                "user_id": "@alice:mindroom.chat",
                "pair_code_hash": provisioning._hash_token("AAAA-BBBB"),
                "status": "pending",
                "created_at": provisioning._as_utc_iso(provisioning._now_utc()),
                "expires_at": provisioning._as_utc_iso(provisioning._now_utc() + provisioning.timedelta(seconds=600)),
                "completed_at": None,
                "connection_id": None,
            },
        ],
        "connections": [],
    }
    state_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    app = provisioning.create_app(_service_config(state_path))
    with TestClient(app):

        async def _get_state() -> provisioning.ProvisioningState:
            return app.state.runtime_state

        state = asyncio.run(_get_state())
        session = state.pair_sessions["legacy-session-id"]
        assert session.user_id == "@alice:mindroom.chat"
        assert session.device_secret_hash is None
        assert session.client_name is None
        assert session.fingerprint is None
        assert session.approved_at is None
