"""Tests for the standalone local provisioning service script."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
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
    from collections.abc import Callable
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


OPENID_TOKEN_HEADER = "X-Matrix-OpenID-Token"  # noqa: S105
ALICE_OPENID_HEADERS = {OPENID_TOKEN_HEADER: "openid-alice"}
BOB_OPENID_HEADERS = {OPENID_TOKEN_HEADER: "openid-bob"}


def _patch_legacy_access_token_auth(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    token_to_user = {
        "token-alice": "@alice:mindroom.chat",
        "token-bob": "@bob:mindroom.chat",
    }
    calls: list[str] = []

    async def _fake_matrix_whoami(config: provisioning.ServiceConfig, access_token: str) -> str:
        del config
        calls.append(access_token)
        user_id = token_to_user.get(access_token)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid Matrix access token")
        return user_id

    monkeypatch.setattr(provisioning, "_matrix_whoami", _fake_matrix_whoami)
    return calls


def _patch_openid_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    token_to_user = {
        "openid-alice": "@alice:mindroom.chat",
        "openid-bob": "@bob:mindroom.chat",
    }

    async def _fake_matrix_openid_userinfo(config: provisioning.ServiceConfig, openid_token: str) -> str:
        del config
        user_id = token_to_user.get(openid_token)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid Matrix OpenID token")
        return user_id

    monkeypatch.setattr(provisioning, "_matrix_openid_userinfo", _fake_matrix_openid_userinfo)


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
    _patch_legacy_access_token_auth(monkeypatch)
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
    _patch_legacy_access_token_auth(monkeypatch)
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


def _post_heartbeat(client: TestClient, client_id: str, client_secret: str) -> httpx.Response:
    return client.post(
        "/v1/local-mindroom/heartbeat",
        headers={
            "X-Local-MindRoom-Client-Id": client_id,
            "X-Local-MindRoom-Client-Secret": client_secret,
        },
    )


def _listed_last_seen(client: TestClient) -> datetime:
    listed = client.get("/v1/local-mindroom/connections", headers={"Authorization": "Bearer token-alice"})
    assert listed.status_code == 200
    [connection] = listed.json()["connections"]
    return datetime.fromisoformat(connection["last_seen_at"])


def test_heartbeat_requires_paired_client_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Heartbeats authenticate exactly like register-agent."""
    _patch_legacy_access_token_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        complete = _pair_local_client(client)
        missing = client.post("/v1/local-mindroom/heartbeat")
        wrong_secret = _post_heartbeat(client, complete["client_id"], "wrong-secret")
        unknown_client = _post_heartbeat(client, "unknown-client", complete["client_secret"])
        accepted = _post_heartbeat(client, complete["client_id"], complete["client_secret"])

    assert missing.status_code == 401
    assert wrong_secret.status_code == 401
    assert unknown_client.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json() == {"status": "ok"}


def test_heartbeat_rejects_revoked_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A revoked install learns that its connection was revoked."""
    _patch_legacy_access_token_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        complete = _pair_local_client(client)
        client.delete(
            f"/v1/local-mindroom/connections/{complete['client_id']}",
            headers={"Authorization": "Bearer token-alice"},
        )
        response = _post_heartbeat(client, complete["client_id"], complete["client_secret"])

    assert response.status_code == 403
    assert response.json()["detail"] == provisioning.CONNECTION_REVOKED_DETAIL


def test_heartbeat_updates_last_seen_with_throttled_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heartbeats refresh the listed last-seen time but rewrite the state file at most every ten minutes."""
    _patch_legacy_access_token_auth(monkeypatch)
    state_path = tmp_path / "state.json"
    app = provisioning.create_app(_service_config(state_path))
    persisted: list[datetime] = []
    real_persist = provisioning._persist_state_unlocked

    def _counting_persist(state: provisioning.ProvisioningState, path: Path) -> None:
        persisted.append(state.connections[complete["client_id"]].last_seen_at)
        real_persist(state, path)

    paired_at = provisioning._now_utc()
    with TestClient(app) as client:
        complete = _pair_local_client(client)
        monkeypatch.setattr(provisioning, "_persist_state_unlocked", _counting_persist)

        first = paired_at + provisioning.timedelta(minutes=11)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: first)
        assert _post_heartbeat(client, complete["client_id"], complete["client_secret"]).status_code == 200
        assert _listed_last_seen(client) == first

        soon_after = first + provisioning.timedelta(minutes=5)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: soon_after)
        assert _post_heartbeat(client, complete["client_id"], complete["client_secret"]).status_code == 200
        assert _listed_last_seen(client) == first

        later = first + provisioning.timedelta(minutes=10)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: later)
        assert _post_heartbeat(client, complete["client_id"], complete["client_secret"]).status_code == 200
        assert _listed_last_seen(client) == later

    assert persisted == [first, later]
    [stored] = json.loads(state_path.read_text(encoding="utf-8"))["connections"]
    assert stored["last_seen_at"] == provisioning._as_utc_iso(later)


def test_heartbeat_is_rate_limited_per_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A misbehaving install cannot hammer the heartbeat endpoint."""
    _patch_legacy_access_token_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        complete = _pair_local_client(client)
        statuses = [
            _post_heartbeat(client, complete["client_id"], complete["client_secret"]).status_code for _ in range(11)
        ]

    assert statuses == [200] * 10 + [429]


def test_google_oauth_client_endpoint_requires_server_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A paired client receives an explicit error when the server has no Google app."""
    _patch_legacy_access_token_auth(monkeypatch)
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
    _patch_legacy_access_token_auth(monkeypatch)
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
    _patch_legacy_access_token_auth(monkeypatch)
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
    _patch_legacy_access_token_auth(monkeypatch)
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
    _patch_legacy_access_token_auth(monkeypatch)
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
    _patch_legacy_access_token_auth(monkeypatch)
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
    _patch_legacy_access_token_auth(monkeypatch)
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
    _patch_legacy_access_token_auth(monkeypatch)
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
    _patch_legacy_access_token_auth(monkeypatch)
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
    _patch_legacy_access_token_auth(monkeypatch)
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
    _patch_legacy_access_token_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        result = client.post("/v1/local-mindroom/pair/start")
        assert result.status_code == 401


def test_state_persists_between_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Connections should survive process restarts via JSON state file."""
    _patch_legacy_access_token_auth(monkeypatch)
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
    """The runtime client classifies register-agent and heartbeat 403s by these exact strings."""
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
    _patch_openid_auth(monkeypatch)
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
            headers=ALICE_OPENID_HEADERS,
        )
        assert inspected.status_code == 200
        assert inspected.json()["client_name"] == "alice-macbook"
        assert inspected.json()["status"] == "pending"

        approved = client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": pair_code},
            headers=ALICE_OPENID_HEADERS,
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

        listed = client.get("/v1/local-mindroom/connections", headers=ALICE_OPENID_HEADERS)
        assert [item["id"] for item in listed.json()["connections"]] == [body["client_id"]]


def test_device_pairing_credentials_register_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials from device pairing authenticate register-agent like browser-issued ones."""
    _patch_openid_auth(monkeypatch)
    register_calls: list[str] = []
    _install_fake_register(monkeypatch, register_calls)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": started["pair_code"]},
            headers=ALICE_OPENID_HEADERS,
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
    _patch_openid_auth(monkeypatch)
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
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        code = {"pair_code": started["pair_code"]}
        assert (
            client.post(
                "/v1/local-mindroom/pair/device/approve",
                json=code,
                headers=ALICE_OPENID_HEADERS,
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/local-mindroom/pair/device/approve",
                json=code,
                headers=ALICE_OPENID_HEADERS,
            ).status_code
            == 200
        )
        taken = client.post(
            "/v1/local-mindroom/pair/device/approve",
            json=code,
            headers=BOB_OPENID_HEADERS,
        )
        assert taken.status_code == 409
        assert taken.json()["detail"] == "Pair code already approved"


def test_device_code_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired device codes can neither be approved nor polled into credentials."""
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        later = provisioning._now_utc() + provisioning.timedelta(seconds=601)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: later)

        approve = client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": started["pair_code"]},
            headers=ALICE_OPENID_HEADERS,
        )
        assert approve.status_code == 410
        poll = client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": started["device_secret"]})
        assert poll.json()["status"] == "expired"


def test_device_code_cannot_complete_through_browser_initiated_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy pair/complete endpoint must not hand out credentials for a device code."""
    _patch_openid_auth(monkeypatch)
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
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        pair_code = client.post(
            "/v1/local-mindroom/pair/start",
            headers=ALICE_OPENID_HEADERS,
        ).json()["pair_code"]
        response = client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": pair_code},
            headers=ALICE_OPENID_HEADERS,
        )
        assert response.status_code == 404


def test_device_poll_rejects_unknown_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Polling with a secret the service never issued fails without revealing sessions."""
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        response = client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": "not-issued"})
        assert response.status_code == 404


def test_device_start_is_rate_limited_per_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unauthenticated device starts are limited per client address."""
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        for _ in range(10):
            _start_device_pairing(client)
        response = client.post(
            "/v1/local-mindroom/pair/device/start",
            json={"client_name": "x", "client_pubkey_or_fingerprint": "sha256:x"},
        )
        assert response.status_code == 429


def _count_homeserver_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    lookups: list[str] = []

    async def _fake_openid_userinfo(config: provisioning.ServiceConfig, openid_token: str) -> str:
        del config
        lookups.append(openid_token)
        raise HTTPException(status_code=401, detail="Invalid Matrix OpenID token")

    async def _fake_whoami(config: provisioning.ServiceConfig, access_token: str) -> str:
        del config
        lookups.append(access_token)
        raise HTTPException(status_code=401, detail="Invalid Matrix access token")

    monkeypatch.setattr(provisioning, "_matrix_openid_userinfo", _fake_openid_userinfo)
    monkeypatch.setattr(provisioning, "_matrix_whoami", _fake_whoami)
    return lookups


def _garbage_browser_request(client: TestClient, endpoint: str, headers: dict[str, str]) -> httpx.Response:
    if endpoint == "connections-list":
        return client.get("/v1/local-mindroom/connections", headers=headers)
    if endpoint == "connections-revoke":
        return client.delete("/v1/local-mindroom/connections/some-connection", headers=headers)
    return client.post(f"/v1/local-mindroom/pair/device/{endpoint}", json={"pair_code": "AAAA-BBBB"}, headers=headers)


@pytest.mark.parametrize(
    ("endpoint", "headers"),
    [
        ("inspect", {OPENID_TOKEN_HEADER: "garbage"}),
        ("approve", {OPENID_TOKEN_HEADER: "garbage"}),
        ("connections-list", {OPENID_TOKEN_HEADER: "garbage"}),
        ("connections-revoke", {OPENID_TOKEN_HEADER: "garbage"}),
        ("connections-list", {"Authorization": "Bearer garbage"}),
    ],
    ids=["inspect", "approve", "connections-list", "connections-revoke", "connections-list-legacy-token"],
)
def test_homeserver_token_lookups_are_rate_limited_per_client_before_the_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    headers: dict[str, str],
) -> None:
    """Unauthenticated callers cannot make the service call the homeserver more than the per-address limit."""
    lookups = _count_homeserver_lookups(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))
    limit = provisioning.HOMESERVER_TOKEN_LOOKUP_LIMIT_PER_MINUTE

    with TestClient(app) as client:
        statuses = [_garbage_browser_request(client, endpoint, headers).status_code for _ in range(limit + 1)]
    with TestClient(app, client=("203.0.113.9", 50000)) as other_client:
        other_status = _garbage_browser_request(other_client, endpoint, headers).status_code

    assert statuses == [401] * limit + [429]
    assert other_status == 401
    assert len(lookups) == limit + 1


def test_homeserver_token_lookup_limit_is_shared_across_browser_endpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One address shares a single lookup budget across every browser-authenticated endpoint."""
    lookups = _count_homeserver_lookups(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))
    headers = {OPENID_TOKEN_HEADER: "garbage"}
    limit = provisioning.HOMESERVER_TOKEN_LOOKUP_LIMIT_PER_MINUTE
    endpoints = ("inspect", "approve", "connections-list")

    with TestClient(app) as client:
        for index in range(limit):
            assert _garbage_browser_request(client, endpoints[index % len(endpoints)], headers).status_code == 401
        assert _garbage_browser_request(client, "connections-revoke", headers).status_code == 429

    assert len(lookups) == limit


def test_verified_users_behind_one_address_reach_their_own_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-address lookup limit leaves room for several users behind one NAT to reach their per-user limits."""
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        statuses = {
            user: [client.get("/v1/local-mindroom/connections", headers=headers).status_code for _ in range(61)]
            for user, headers in (("alice", ALICE_OPENID_HEADERS), ("bob", BOB_OPENID_HEADERS))
        }
        pair_statuses = [
            client.get(
                "/v1/local-mindroom/pair/status",
                headers={**ALICE_OPENID_HEADERS, provisioning.PAIR_STATUS_SESSION_HEADER: "unknown"},
            ).status_code
            for _ in range(61)
        ]

    assert statuses == {"alice": [200] * 60 + [429], "bob": [200] * 60 + [429]}
    assert pair_statuses == [404] * 60 + [429]


def test_device_poll_is_rate_limited_per_device_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One polling client cannot use up the budget of other clients behind the same address."""
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))
    per_secret = provisioning.DEVICE_POLL_LIMIT_PER_SECRET_PER_MINUTE
    per_address = provisioning.DEVICE_POLL_LIMIT_PER_ADDRESS_PER_MINUTE

    def _poll(device_secret: object) -> httpx.Response:
        return client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": device_secret})

    with TestClient(app) as client:
        greedy = _start_device_pairing(client, "greedy")
        polite = _start_device_pairing(client, "polite")
        greedy_statuses = [_poll(greedy["device_secret"]).status_code for _ in range(per_address)]
        polite_poll = _poll(polite["device_secret"])
        # Polls rejected by the device limit leave the shared address budget untouched.
        remaining = per_address - per_secret - 1
        other_statuses = [_poll(f"secret-{index}").status_code for index in range(remaining + 1)]

    assert greedy_statuses == [200] * per_secret + [429] * (per_address - per_secret)
    assert polite_poll.status_code == 200
    assert polite_poll.json()["status"] == "pending"
    assert other_statuses == [404] * remaining + [429]


def test_device_poll_allows_many_clients_behind_one_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-address poll limit leaves room for many CLIs polling every few seconds behind one NAT."""
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        statuses = [
            client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": f"secret-{index}"}).status_code
            for index in range(301)
        ]

    assert statuses == [404] * 300 + [429]


def test_approved_device_session_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An approval persisted before a restart can still be claimed after it."""
    _patch_openid_auth(monkeypatch)
    state_path = tmp_path / "state.json"

    with TestClient(provisioning.create_app(_service_config(state_path))) as client:
        started = _start_device_pairing(client)
        client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": started["pair_code"]},
            headers=ALICE_OPENID_HEADERS,
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
    """Sessions expired for longer than one more code lifetime are removed when new pairing starts."""
    _patch_openid_auth(monkeypatch)
    state_path = tmp_path / "state.json"
    app = provisioning.create_app(_service_config(state_path))

    with TestClient(app) as client:
        old_browser = client.post(
            "/v1/local-mindroom/pair/start",
            headers=ALICE_OPENID_HEADERS,
        ).json()
        old_device = _start_device_pairing(client, "old-machine")

        later = provisioning._now_utc() + provisioning.timedelta(seconds=2 * 600 + 1)
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


def test_recently_expired_device_code_still_reports_expired_after_another_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renewed CLI start keeps the old code answering 410 and the old poll answering expired for one more TTL."""
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))
    started_at = provisioning._now_utc()

    with TestClient(app) as client:
        old = _start_device_pairing(client, "old-code")

        just_expired = started_at + provisioning.timedelta(seconds=601)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: just_expired)
        _start_device_pairing(client, "renewed-code")

        for action in ("inspect", "approve"):
            response = client.post(
                f"/v1/local-mindroom/pair/device/{action}",
                json={"pair_code": old["pair_code"]},
                headers=ALICE_OPENID_HEADERS,
            )
            assert response.status_code == 410
            assert response.json()["detail"] == "Pair code expired"
        poll = client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": old["device_secret"]})
        assert poll.status_code == 200
        assert poll.json()["status"] == "expired"

        long_expired = started_at + provisioning.timedelta(seconds=2 * 600 + 1)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: long_expired)
        _start_device_pairing(client, "renewed-again")

        inspect = client.post(
            "/v1/local-mindroom/pair/device/inspect",
            json={"pair_code": old["pair_code"]},
            headers=ALICE_OPENID_HEADERS,
        )
        assert inspect.status_code == 404
        poll = client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": old["device_secret"]})
        assert poll.status_code == 404


def test_connected_pair_sessions_are_pruned_after_one_more_code_lifetime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claimed sessions keep answering 410 for one code lifetime, then disappear while their connections stay."""
    _patch_openid_auth(monkeypatch)
    state_path = tmp_path / "state.json"
    app = provisioning.create_app(_service_config(state_path))
    started_at = provisioning._now_utc()

    with TestClient(app) as client:
        browser = client.post("/v1/local-mindroom/pair/start", headers=ALICE_OPENID_HEADERS).json()
        browser_complete = client.post(
            "/v1/local-mindroom/pair/complete",
            json={"pair_code": browser["pair_code"], "client_name": "browser", "client_pubkey_or_fingerprint": "x"},
        ).json()
        device = _start_device_pairing(client, "device")
        client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": device["pair_code"]},
            headers=ALICE_OPENID_HEADERS,
        )
        device_complete = client.post(
            "/v1/local-mindroom/pair/device/poll",
            json={"device_secret": device["device_secret"]},
        ).json()
        assert device_complete["status"] == "connected"

        within_retention = started_at + provisioning.timedelta(seconds=599)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: within_retention)
        _start_device_pairing(client, "renewed")
        replay = client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": device["device_secret"]})
        assert replay.status_code == 410
        assert replay.json()["detail"] == provisioning.PAIR_SESSION_ALREADY_CLAIMED_DETAIL

        past_retention = provisioning._now_utc() + provisioning.timedelta(seconds=2 * 600 + 1)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: past_retention)
        _start_device_pairing(client, "latest")
        replay = client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": device["device_secret"]})
        assert replay.status_code == 404
        listed = client.get("/v1/local-mindroom/connections", headers=ALICE_OPENID_HEADERS).json()

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert [session["client_name"] for session in persisted["pair_sessions"]] == ["latest"]
    connection_ids = {browser_complete["client_id"], device_complete["client_id"]}
    assert {connection["id"] for connection in persisted["connections"]} == connection_ids
    assert {connection["id"] for connection in listed["connections"]} == connection_ids


def test_approve_extends_claim_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Approval near expiry extends the window so CLI can still claim credentials."""
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        near_expiry = provisioning._now_utc() + provisioning.timedelta(seconds=595)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: near_expiry)

        client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": started["pair_code"]},
            headers=ALICE_OPENID_HEADERS,
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
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        near_expiry = provisioning._now_utc() + provisioning.timedelta(seconds=595)
        monkeypatch.setattr(provisioning, "_now_utc", lambda: near_expiry)

        client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": started["pair_code"]},
            headers=ALICE_OPENID_HEADERS,
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
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        client.post(
            "/v1/local-mindroom/pair/device/approve",
            json={"pair_code": started["pair_code"]},
            headers=ALICE_OPENID_HEADERS,
        )
        client.post("/v1/local-mindroom/pair/device/poll", json={"device_secret": started["device_secret"]})

        for endpoint in ("approve", "inspect"):
            response = client.post(
                f"/v1/local-mindroom/pair/device/{endpoint}",
                json={"pair_code": started["pair_code"]},
                headers=ALICE_OPENID_HEADERS,
            )
            assert response.status_code == 409
            assert response.json()["detail"] == "Pair code already used"


def test_legacy_state_loads_browser_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """State files written before device pairing load sessions as browser-initiated."""
    _patch_openid_auth(monkeypatch)
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


def _install_homeserver(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_async_client = httpx.AsyncClient

    def _client(*, timeout: float, verify: bool) -> httpx.AsyncClient:
        assert verify is True
        return real_async_client(timeout=timeout, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(provisioning.httpx, "AsyncClient", _client)


@pytest.mark.asyncio
async def test_openid_userinfo_returns_local_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The service asks its own homeserver who owns the OpenID token and never sends an access token."""
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"sub": "@alice:mindroom.chat"})

    _install_homeserver(monkeypatch, _handler)

    user_id = await provisioning._matrix_openid_userinfo(_service_config(tmp_path / "state.json"), "openid-secret")

    assert user_id == "@alice:mindroom.chat"
    [request] = requests
    assert request.method == "GET"
    assert request.url.copy_with(query=None) == "https://mindroom.chat/_matrix/federation/v1/openid/userinfo"
    assert request.url.params["access_token"] == "openid-secret"  # noqa: S105
    assert "authorization" not in request.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sub",
    ["@alice:evil.example", "@alice:mindroom.chat.evil", "alice:mindroom.chat", "@:mindroom.chat", "@alice", None],
)
async def test_openid_userinfo_rejects_users_of_other_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sub: str | None,
) -> None:
    """Only a well-formed user ID on the service's own server name is accepted."""
    _install_homeserver(monkeypatch, lambda _request: httpx.Response(200, json={"sub": sub}))

    with pytest.raises(HTTPException) as exc_info:
        await provisioning._matrix_openid_userinfo(_service_config(tmp_path / "state.json"), "openid-secret")

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (401, {"errcode": "M_UNKNOWN_TOKEN", "error": "Access Token unknown or expired"}),
        (403, {"errcode": "M_FORBIDDEN", "error": "Forbidden"}),
        (400, {"errcode": "M_UNKNOWN_TOKEN", "error": "Invalid token"}),
    ],
)
async def test_openid_userinfo_rejects_invalid_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    body: dict[str, str],
) -> None:
    """Unknown or expired OpenID tokens are an authentication failure, not a server error."""
    _install_homeserver(monkeypatch, lambda _request: httpx.Response(status_code, json=body))

    with pytest.raises(HTTPException) as exc_info:
        await provisioning._matrix_openid_userinfo(_service_config(tmp_path / "state.json"), "openid-secret")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid Matrix OpenID token"


@pytest.mark.asyncio
async def test_openid_userinfo_reports_unreachable_homeserver_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport failures map to 502, and the token in the query string never reaches the error detail."""

    def _handler(request: httpx.Request) -> httpx.Response:
        msg = f"connection refused for {request.url}"
        raise httpx.ConnectError(msg, request=request)

    _install_homeserver(monkeypatch, _handler)

    with pytest.raises(HTTPException) as exc_info:
        await provisioning._matrix_openid_userinfo(_service_config(tmp_path / "state.json"), "openid-secret")

    assert exc_info.value.status_code == 502
    assert "openid-secret" not in str(exc_info.value.detail)
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [httpx.Response(500, json={"errcode": "M_UNKNOWN"}), httpx.Response(200, text="not json")],
)
async def test_openid_userinfo_reports_homeserver_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
) -> None:
    """Other homeserver failures are gateway errors."""
    _install_homeserver(monkeypatch, lambda _request: response)

    with pytest.raises(HTTPException) as exc_info:
        await provisioning._matrix_openid_userinfo(_service_config(tmp_path / "state.json"), "openid-secret")

    assert exc_info.value.status_code == 502


def test_device_endpoints_reject_access_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Device inspect and approve accept only OpenID tokens, never Matrix access tokens."""
    whoami_calls = _patch_legacy_access_token_auth(monkeypatch)
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        started = _start_device_pairing(client)
        for path in ("inspect", "approve"):
            for headers in ({"Authorization": "Bearer token-alice"}, {"X-Matrix-Access-Token": "token-alice"}):
                response = client.post(
                    f"/v1/local-mindroom/pair/device/{path}",
                    json={"pair_code": started["pair_code"]},
                    headers=headers,
                )
                assert response.status_code == 401
                assert response.json()["detail"] == "Missing Matrix OpenID token"

    assert whoami_calls == []


def test_browser_endpoints_accept_openid_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pair start, status, connection listing, and revocation authenticate with OpenID tokens."""
    whoami_calls = _patch_legacy_access_token_auth(monkeypatch)
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        start = client.post("/v1/local-mindroom/pair/start", headers=ALICE_OPENID_HEADERS)
        assert start.status_code == 200
        status = client.get(
            "/v1/local-mindroom/pair/status",
            headers={**ALICE_OPENID_HEADERS, provisioning.PAIR_STATUS_SESSION_HEADER: start.json()["pair_session_id"]},
        )
        assert status.json()["status"] == "pending"
        complete = client.post(
            "/v1/local-mindroom/pair/complete",
            json={"pair_code": start.json()["pair_code"], "client_name": "x", "client_pubkey_or_fingerprint": "x"},
        ).json()
        listed = client.get("/v1/local-mindroom/connections", headers=ALICE_OPENID_HEADERS)
        assert [item["id"] for item in listed.json()["connections"]] == [complete["client_id"]]
        revoked = client.delete(f"/v1/local-mindroom/connections/{complete['client_id']}", headers=ALICE_OPENID_HEADERS)
        assert revoked.json()["revoked"] is True

    assert whoami_calls == []


def test_browser_endpoints_still_accept_legacy_access_token_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployed chat client still sends access tokens to the browser-initiated endpoints."""
    _patch_legacy_access_token_auth(monkeypatch)
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        complete = _pair_local_client(client)
        for headers in ({"Authorization": "Bearer token-alice"}, {"X-Matrix-Access-Token": "token-alice"}):
            listed = client.get("/v1/local-mindroom/connections", headers=headers)
            assert [item["id"] for item in listed.json()["connections"]] == [complete["client_id"]]
        revoked = client.delete(
            f"/v1/local-mindroom/connections/{complete['client_id']}",
            headers={"X-Matrix-Access-Token": "token-alice"},
        )
        assert revoked.json()["revoked"] is True


def test_openid_token_wins_over_legacy_access_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When both are sent, only the OpenID token authenticates, even if it is invalid."""
    whoami_calls = _patch_legacy_access_token_auth(monkeypatch)
    _patch_openid_auth(monkeypatch)
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        _pair_local_client(client)
        whoami_calls.clear()
        as_bob = client.get(
            "/v1/local-mindroom/connections",
            headers={**BOB_OPENID_HEADERS, "Authorization": "Bearer token-alice"},
        )
        assert as_bob.json()["connections"] == []
        invalid = client.get(
            "/v1/local-mindroom/connections",
            headers={OPENID_TOKEN_HEADER: "openid-unknown", "X-Matrix-Access-Token": "token-alice"},
        )
        assert invalid.status_code == 401

    assert whoami_calls == []


def test_cors_allows_openid_token_header(tmp_path: Path) -> None:
    """Browsers may send the OpenID token header cross-origin from the chat client."""
    app = provisioning.create_app(_service_config(tmp_path / "state.json"))

    with TestClient(app) as client:
        response = client.options(
            "/v1/local-mindroom/pair/device/approve",
            headers={
                "Origin": "https://chat.mindroom.chat",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-matrix-openid-token",
            },
        )

    assert response.status_code == 200
    assert "x-matrix-openid-token" in response.headers["access-control-allow-headers"].lower()


def test_app_silences_httpx_request_url_logging(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The httpx logger stays above INFO because request URLs carry the OpenID token."""
    httpx_logger = logging.getLogger("httpx")
    original_level = httpx_logger.level
    httpx_logger.setLevel(logging.NOTSET)
    # An operator enabling INFO logging globally must still not see request URLs.
    caplog.set_level(logging.INFO)
    try:
        provisioning.create_app(_service_config(tmp_path / "state.json"))

        assert httpx_logger.level >= logging.WARNING
        assert not httpx_logger.isEnabledFor(logging.INFO)
    finally:
        httpx_logger.setLevel(original_level)
