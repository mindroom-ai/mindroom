"""Tests for the pending dashboard OAuth state flow helpers."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from mindroom.api.auth_user import AuthSource, AuthUser, bind_request_auth_user
from mindroom.api.credentials_oauth_flows import consume_pending_oauth_request, issue_pending_oauth_state
from mindroom.api.main import initialize_api_app
from mindroom.constants import resolve_runtime_paths


def _oauth_state_test_client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    initialize_api_app(app, resolve_runtime_paths(storage_path=tmp_path / "mindroom_data"))

    @app.post("/issue/{service}")
    async def issue(
        service: str,
        request: Request,
        user_id: str,
        agent_name: str | None = None,
        payload: dict[str, str] | None = None,
    ) -> dict[str, str]:
        bind_request_auth_user(request, AuthUser(user_id=user_id, email=None, source=AuthSource.PLATFORM))
        return {"state": issue_pending_oauth_state(request, service, agent_name, payload=payload)}

    @app.post("/consume/{service}")
    async def consume(service: str, request: Request, state: str, user_id: str) -> dict[str, object]:
        bind_request_auth_user(request, AuthUser(user_id=user_id, email=None, source=AuthSource.PLATFORM))
        pending = consume_pending_oauth_request(request, service, state)
        return {"agent_name": pending.agent_name, "payload": pending.payload}

    return TestClient(app)


def test_pending_oauth_state_round_trip(tmp_path: Path) -> None:
    """Issued state should consume exactly once for the issuing user and service."""
    client = _oauth_state_test_client(tmp_path)

    issue_response = client.post("/issue/google?user_id=alice&agent_name=general")
    assert issue_response.status_code == 200
    response = client.post(f"/consume/google?user_id=alice&state={issue_response.json()['state']}")

    assert response.status_code == 200
    assert response.json() == {"agent_name": "general", "payload": {}}


def test_pending_oauth_state_rejects_different_user(tmp_path: Path) -> None:
    """Pending OAuth state should stay valid for the issuer after a different user is rejected."""
    client = _oauth_state_test_client(tmp_path)

    state = client.post("/issue/google?user_id=alice&agent_name=general").json()["state"]

    consume_response = client.post(f"/consume/google?user_id=bob&state={state}")
    assert consume_response.status_code == 403
    assert "current user" in consume_response.json()["detail"]

    issuer_response = client.post(f"/consume/google?user_id=alice&state={state}")
    assert issuer_response.status_code == 200
    assert issuer_response.json() == {"agent_name": "general", "payload": {}}


def test_pending_oauth_request_preserves_payload(tmp_path: Path) -> None:
    """Pending OAuth state should round-trip service-specific callback payload."""
    client = _oauth_state_test_client(tmp_path)
    payload = {"instance_url": "https://ha.example.com", "client_id": "client-id"}

    state = client.post("/issue/homeassistant?user_id=alice&agent_name=general", json=payload).json()["state"]
    response = client.post(f"/consume/homeassistant?user_id=alice&state={state}")

    assert response.status_code == 200
    assert response.json() == {"agent_name": "general", "payload": payload}


def test_pending_oauth_state_consume_twice_fails(tmp_path: Path) -> None:
    """A consumed state token must not be replayable."""
    client = _oauth_state_test_client(tmp_path)

    state = client.post("/issue/google?user_id=alice&agent_name=general").json()["state"]
    assert client.post(f"/consume/google?user_id=alice&state={state}").status_code == 200

    replay_response = client.post(f"/consume/google?user_id=alice&state={state}")
    assert replay_response.status_code == 400
    assert "invalid or expired" in replay_response.json()["detail"]


def test_pending_oauth_state_tampered_token_fails(tmp_path: Path) -> None:
    """A tampered state token must be rejected without consuming the real one."""
    client = _oauth_state_test_client(tmp_path)

    state = client.post("/issue/google?user_id=alice&agent_name=general").json()["state"]
    tampered = state[:-2] + ("AA" if not state.endswith("AA") else "BB")

    tampered_response = client.post(f"/consume/google?user_id=alice&state={tampered}")
    assert tampered_response.status_code == 400
    assert "invalid or expired" in tampered_response.json()["detail"]

    # The genuine token must still be consumable after the tampered attempt.
    assert client.post(f"/consume/google?user_id=alice&state={state}").status_code == 200


def test_pending_oauth_state_rejects_wrong_service(tmp_path: Path) -> None:
    """State issued for one integration must not complete another integration."""
    client = _oauth_state_test_client(tmp_path)

    state = client.post("/issue/google?user_id=alice&agent_name=general").json()["state"]
    response = client.post(f"/consume/spotify?user_id=alice&state={state}")

    assert response.status_code == 400
    assert "does not match this integration" in response.json()["detail"]
