"""Tests for the approvals REST and WebSocket API."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette import status
from starlette.websockets import WebSocketDisconnect

from mindroom.tool_approval import get_approval_store

if TYPE_CHECKING:
    from pathlib import Path


def _create_pending_request() -> str:
    store = get_approval_store()
    assert store is not None
    request = asyncio.run(
        store.create_request(
            tool_name="run_shell_command",
            arguments={"command": "echo hi"},
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            session_id="session-1",
            channel="matrix",
            tenant_id="tenant-1",
            account_id="account-1",
            matched_rule="run_shell_command",
            script_path=None,
            timeout_seconds=60,
        ),
    )
    return request.id


def test_list_approvals_returns_pending_requests_sorted(test_client: TestClient) -> None:
    """GET /api/approvals should return only pending approvals sorted by created_at."""
    first_request_id = _create_pending_request()
    time.sleep(0.01)
    second_request_id = _create_pending_request()
    store = get_approval_store()
    assert store is not None
    asyncio.run(store.approve(first_request_id, resolved_by="standalone"))

    response = test_client.get("/api/approvals")

    assert response.status_code == 200
    assert [approval["id"] for approval in response.json()] == [second_request_id]


def test_approve_approval_returns_record_and_resolved_by(test_client: TestClient) -> None:
    """Approving one request should resolve it and capture the authenticated user."""
    request_id = _create_pending_request()

    response = test_client.post(f"/api/approvals/{request_id}/approve")

    assert response.status_code == 200
    assert response.json()["id"] == request_id
    assert response.json()["status"] == "approved"
    assert response.json()["resolved_by"] == "standalone"


def test_deny_approval_returns_record_and_reason(test_client: TestClient) -> None:
    """Deny should return the updated record with the user-supplied reason."""
    request_id = _create_pending_request()

    response = test_client.post(
        f"/api/approvals/{request_id}/deny",
        json={"reason": "Do not run shell commands from the dashboard."},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "denied"
    assert response.json()["resolution_reason"] == "Do not run shell commands from the dashboard."
    assert response.json()["resolved_by"] == "standalone"


def test_approval_routes_return_404_for_missing_ids(test_client: TestClient) -> None:
    """Missing approval IDs should return HTTP 404."""
    response = test_client.post("/api/approvals/missing-id/approve")

    assert response.status_code == 404
    assert "missing-id" in response.json()["detail"]


def test_approval_routes_return_409_for_already_resolved_requests(test_client: TestClient) -> None:
    """Existing but non-pending requests should return HTTP 409 with the current status."""
    request_id = _create_pending_request()
    first_response = test_client.post(f"/api/approvals/{request_id}/approve")
    assert first_response.status_code == 200

    response = test_client.post(f"/api/approvals/{request_id}/deny", json={"reason": "too late"})

    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "approved"


def test_approvals_websocket_sends_snapshot_and_updates(test_client: TestClient) -> None:
    """The approvals WebSocket should send one snapshot and then created/updated frames."""
    with test_client.websocket_connect("/api/approvals/ws") as websocket:
        snapshot = websocket.receive_json()
        assert snapshot == {"type": "snapshot", "approvals": []}

        request_id = _create_pending_request()
        created = websocket.receive_json()
        assert created["type"] == "created"
        assert created["approval"]["id"] == request_id
        assert created["approval"]["status"] == "pending"

        response = test_client.post(f"/api/approvals/{request_id}/approve")
        assert response.status_code == 200

        updated = websocket.receive_json()
        assert updated["type"] == "updated"
        assert updated["approval"]["id"] == request_id
        assert updated["approval"]["status"] == "approved"
        assert updated["approval"]["resolved_by"] == "standalone"


def test_approvals_websocket_rejects_missing_auth(
    temp_config_file: Path,
) -> None:
    """The approvals WebSocket should reject unauthenticated clients when an API key is required."""
    from mindroom import constants  # noqa: PLC0415
    from mindroom.api import main  # noqa: PLC0415

    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=temp_config_file,
        process_env={"MINDROOM_API_KEY": "secret-key"},
    )
    main.initialize_api_app(main.app, runtime_paths)
    main._load_config_from_file(main._app_runtime_paths(main.app), main.app)

    with (
        TestClient(main.app) as client,
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/api/approvals/ws"),
    ):
        pass

    assert exc_info.value.code == status.WS_1008_POLICY_VIOLATION


def test_approvals_websocket_rejects_unexpected_auth_error(test_client: TestClient) -> None:
    """Unexpected auth failures should still close the socket with a policy violation."""
    from mindroom.api import main  # noqa: PLC0415

    with (
        patch.object(main, "authenticate_websocket_user", side_effect=RuntimeError("boom")),
        pytest.raises(WebSocketDisconnect) as exc_info,
        test_client.websocket_connect("/api/approvals/ws"),
    ):
        pass

    assert exc_info.value.code == status.WS_1008_POLICY_VIOLATION
