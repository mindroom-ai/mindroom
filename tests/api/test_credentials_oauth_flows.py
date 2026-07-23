"""Tests for the pending dashboard OAuth state flow helpers."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from mindroom.api.credentials_oauth_flows import consume_pending_oauth_request, issue_pending_oauth_state
from mindroom.api.main import initialize_api_app
from mindroom.constants import resolve_runtime_paths


def _oauth_state_test_client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    initialize_api_app(app, resolve_runtime_paths(storage_path=tmp_path / "mindroom_data"))

    @app.post("/issue/{service}")
    async def issue(service: str, request: Request, user_id: str, agent_name: str | None = None) -> dict[str, str]:
        request.scope["auth_user"] = {"user_id": user_id}
        return {"state": issue_pending_oauth_state(request, service, agent_name)}

    @app.post("/consume/{service}")
    async def consume(service: str, request: Request, state: str, user_id: str) -> dict[str, str | None]:
        request.scope["auth_user"] = {"user_id": user_id}
        return {"agent_name": consume_pending_oauth_request(request, service, state).agent_name}

    return TestClient(app)


def test_pending_oauth_state_round_trip(tmp_path: Path) -> None:
    """Issued state should consume exactly once for the issuing user and service."""
    client = _oauth_state_test_client(tmp_path)

    state = client.post("/issue/google?user_id=alice&agent_name=general").json()["state"]
    response = client.post(f"/consume/google?user_id=alice&state={state}")

    assert response.status_code == 200
    assert response.json() == {"agent_name": "general"}


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
