"""Tests for Matrix operations API endpoints."""

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import nio
import pytest
import yaml
from aiohttp import ClientError
from aioresponses import aioresponses
from fastapi.testclient import TestClient
from nio.http import TransportResponse

from mindroom import constants
from mindroom.api import config_lifecycle, main
from mindroom.config.agent import AgentConfig, RoomConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.state import MatrixState
from tests.api.conftest import trusted_upstream_headers, use_trusted_upstream_runtime


def _add_test_team_to_runtime_config() -> None:
    """Add a configured team to the API runtime config for one test."""
    api_state = config_lifecycle.require_api_state(main.app)
    with api_state.config_lock:
        context = api_state.snapshot
        if context.runtime_config is None:
            msg = "runtime config should be loaded"
            raise AssertionError(msg)
        context.config_data["teams"] = {
            "test_team": {
                "display_name": "Test Team",
                "role": "A test team",
                "agents": ["test_agent"],
                "rooms": ["team_room"],
                "mode": "coordinate",
            },
        }
        context.runtime_config.teams["test_team"] = TeamConfig(
            display_name="Test Team",
            role="A test team",
            agents=["test_agent"],
            rooms=["team_room"],
            mode="coordinate",
        )


def _add_room_metadata_to_runtime_config(room_key: str) -> None:
    """Add room-only metadata to the API runtime config for one test."""
    api_state = config_lifecycle.require_api_state(main.app)
    with api_state.config_lock:
        context = api_state.snapshot
        if context.runtime_config is None:
            msg = "runtime config should be loaded"
            raise AssertionError(msg)
        context.config_data.setdefault("rooms", {})[room_key] = {"description": "Room-only metadata"}
        context.runtime_config.rooms[room_key] = RoomConfig(description="Room-only metadata")


def _save_matrix_identity(
    runtime_paths: constants.RuntimePaths,
    identity: str,
    *,
    username: str,
    access_token: str,
) -> None:
    state = MatrixState.load(runtime_paths)
    state.add_account(
        f"agent_{identity}",
        username,
        None,
        domain="example.org",
        access_token=access_token,
    )
    state.save(runtime_paths)


def _save_matrix_room(runtime_paths: constants.RuntimePaths, room_key: str, room_id: str) -> None:
    state = MatrixState.load(runtime_paths)
    state.add_room(room_key, room_id, f"#{room_key}:example.org", room_key)
    state.save(runtime_paths)


@pytest.fixture
def mock_matrix_client() -> AsyncMock:
    """Create a mock Matrix client."""
    client = AsyncMock()
    client.close = AsyncMock()
    return client


class TestMatrixOperations:
    """Test Matrix operations API endpoints."""

    @pytest.mark.parametrize("entity_id", ["test_agent", "test_team"])
    def test_entity_avatar_serves_current_matrix_thumbnail(
        self,
        test_client: TestClient,
        entity_id: str,
    ) -> None:
        """Configured agents and teams expose their current saved-identity thumbnail."""
        if entity_id == "test_team":
            _add_test_team_to_runtime_config()
        runtime_paths = main._app_runtime_paths(main.app)
        _save_matrix_identity(
            runtime_paths,
            entity_id,
            username=f"{entity_id}_bot",
            access_token=f"{entity_id}-token",
        )

        with aioresponses() as matrix:
            matrix.get(
                re.compile(r"http://localhost:8008/_matrix/client/v3/profile/.*"),
                payload={"avatar_url": "mxc://example.org/current-avatar"},
            )
            matrix.get(
                "http://localhost:8008/_matrix/client/v1/media/thumbnail/example.org/current-avatar"
                "?width=96&height=96&method=scale&allow_remote=true",
                body=b"thumbnail",
                content_type="image/png",
            )

            response = test_client.get(f"/api/matrix/agents/{entity_id}/avatar")

            assert response.status_code == 200, (response.text, list(matrix.requests))
            assert all(
                call.kwargs["headers"]["Authorization"] == f"Bearer {entity_id}-token"
                for calls in matrix.requests.values()
                for call in calls
            )
        assert response.content == b"thumbnail"
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "no-store" in response.headers["cache-control"]
        assert f"{entity_id}-token" not in str(response.headers)

    @pytest.mark.parametrize("entity_id", ["unknown", "test_agent"])
    def test_entity_avatar_requires_configured_entity_and_saved_identity(
        self,
        test_client: TestClient,
        entity_id: str,
    ) -> None:
        """Unknown entities and configured entities without credentials remain unavailable."""
        with aioresponses() as matrix:
            response = test_client.get(f"/api/matrix/agents/{entity_id}/avatar")
            assert not matrix.requests
        assert response.status_code == 404
        assert response.json()["detail"] == (
            f"Agent or team {entity_id} not found" if entity_id == "unknown" else "Avatar is not available"
        )

    @pytest.mark.parametrize(
        ("avatar_url", "content_type", "body"),
        [
            ("https://example.org/avatar.png", None, None),
            ("mxc://example.org/avatar", "image/svg+xml", b"<svg/>"),
            ("mxc://example.org/avatar", "image/png", b"x" * (1024 * 1024 + 1)),
        ],
        ids=["invalid-mxc", "non-raster", "oversized"],
    )
    def test_entity_avatar_rejects_unsafe_media(
        self,
        test_client: TestClient,
        avatar_url: str,
        content_type: str | None,
        body: bytes | None,
    ) -> None:
        """Dashboard avatar reads keep the Connections MXC, MIME, and size limits."""
        runtime_paths = main._app_runtime_paths(main.app)
        _save_matrix_identity(
            runtime_paths,
            "test_agent",
            username="test_agent_bot",
            access_token="avatar-token",  # noqa: S106
        )

        with aioresponses() as matrix:
            matrix.get(
                re.compile(r"http://localhost:8008/_matrix/client/v3/profile/.*"),
                payload={"avatar_url": avatar_url},
            )
            if content_type is not None and body is not None:
                matrix.get(
                    re.compile(r"http://localhost:8008/_matrix/client/v1/media/thumbnail/.*"),
                    body=body,
                    content_type=content_type,
                )
            response = test_client.get("/api/matrix/agents/test_agent/avatar")

        assert response.status_code == 404
        assert response.json()["detail"] == "Avatar is not available"
        assert len(matrix.requests) == (1 if content_type is None else 2)

    def test_entity_avatar_maps_transport_failures_and_closes_client(
        self,
        test_client: TestClient,
    ) -> None:
        """A failed profile fetch returns a bounded error without leaking the Matrix client."""
        matrix_client = AsyncMock()
        matrix_client.user_id = "@test_agent:example.org"
        matrix_client.get_profile.side_effect = ClientError("offline")
        matrix_client.close = AsyncMock()

        with patch(
            "mindroom.api.matrix_operations.create_agent_http_client",
            return_value=matrix_client,
        ):
            response = test_client.get("/api/matrix/agents/test_agent/avatar")

        assert response.status_code == 502
        matrix_client.close.assert_awaited_once_with()

    def test_entity_avatar_maps_matrix_profile_error_to_upstream_failure(self, test_client: TestClient) -> None:
        """A Matrix error response from profile lookup is not reported as a missing avatar."""
        matrix_client = AsyncMock()
        matrix_client.user_id = "@test_agent:example.org"
        matrix_client.get_profile.return_value = nio.ProfileGetError(
            "rate limited",
            status_code="M_LIMIT_EXCEEDED",
        )
        matrix_client.close = AsyncMock()

        with patch(
            "mindroom.api.matrix_operations.create_agent_http_client",
            return_value=matrix_client,
        ):
            response = test_client.get("/api/matrix/agents/test_agent/avatar")

        assert response.status_code == 502
        matrix_client.close.assert_awaited_once_with()

    @pytest.mark.parametrize(("http_status", "expected"), [(503, 502), (200, 404)])
    def test_entity_avatar_distinguishes_thumbnail_failure_from_rejected_content(
        self,
        test_client: TestClient,
        http_status: int,
        expected: int,
    ) -> None:
        """HTTP media failures are upstream errors while local content rejection stays unavailable."""
        thumbnail_error = nio.ThumbnailError("invalid thumbnail response")
        thumbnail_error.transport_response = TransportResponse()
        thumbnail_error.transport_response.status_code = http_status
        matrix_client = AsyncMock()
        matrix_client.user_id = "@test_agent:example.org"
        matrix_client.get_profile.return_value = nio.ProfileGetResponse(
            avatar_url="mxc://example.org/current-avatar",
        )
        matrix_client.thumbnail.return_value = thumbnail_error
        matrix_client.close = AsyncMock()

        with patch(
            "mindroom.api.matrix_operations.create_agent_http_client",
            return_value=matrix_client,
        ):
            response = test_client.get("/api/matrix/agents/test_agent/avatar")

        assert response.status_code == expected
        matrix_client.close.assert_awaited_once_with()

    def test_entity_avatar_maps_http_thumbnail_failure_without_matrix_errcode(self, test_client: TestClient) -> None:
        """A real aiohttp 5xx thumbnail response is upstream failure even without a Matrix errcode."""
        runtime_paths = main._app_runtime_paths(main.app)
        _save_matrix_identity(
            runtime_paths,
            "test_agent",
            username="test_agent_bot",
            access_token="avatar-token",  # noqa: S106
        )

        with aioresponses() as matrix:
            matrix.get(
                re.compile(r"http://localhost:8008/_matrix/client/v3/profile/.*"),
                payload={"avatar_url": "mxc://example.org/current-avatar"},
            )
            matrix.get(
                "http://localhost:8008/_matrix/client/v1/media/thumbnail/example.org/current-avatar"
                "?width=96&height=96&method=scale&allow_remote=true",
                status=503,
                payload={},
            )

            response = test_client.get("/api/matrix/agents/test_agent/avatar")

        assert response.status_code == 502

    def test_avatar_routes_require_dashboard_authentication(self, test_client: TestClient) -> None:
        """Matrix avatar routes retain the matrix router's dashboard authentication."""
        runtime_paths = use_trusted_upstream_runtime(test_client.app)
        assert config_lifecycle.load_config_into_app(runtime_paths, test_client.app) is True

        agent_response = test_client.get("/api/matrix/agents/test_agent/avatar")
        room_response = test_client.get("/api/matrix/rooms/avatar", params={"room_id": "test_room"})

        assert agent_response.status_code == 401
        assert room_response.status_code == 401

        authenticated = test_client.get(
            "/api/matrix/agents/test_agent/avatar",
            headers=trusted_upstream_headers(),
        )
        assert authenticated.status_code == 404

    @pytest.mark.parametrize(
        ("room_avatar_content", "expected"),
        [({"url": "mxc://example.org/room-avatar"}, 200), ({}, 404)],
        ids=["current-avatar", "no-avatar"],
    )
    def test_room_avatar_reads_current_state_with_configured_occupant(
        self,
        test_client: TestClient,
        room_avatar_content: dict[str, str],
        expected: int,
    ) -> None:
        """A configured room is read through a saved identity assigned to that room."""
        runtime_paths = main._app_runtime_paths(main.app)
        _save_matrix_room(runtime_paths, "test_room", "!test:example.org")
        _save_matrix_identity(
            runtime_paths,
            "test_agent",
            username="test_agent_bot",
            access_token="room-token",  # noqa: S106
        )

        with aioresponses() as matrix:
            matrix.get(
                re.compile(r"http://localhost:8008/_matrix/client/v3/rooms/.*/state/m\.room\.avatar"),
                payload=room_avatar_content,
            )
            if expected == 200:
                matrix.get(
                    "http://localhost:8008/_matrix/client/v1/media/thumbnail/example.org/room-avatar"
                    "?width=96&height=96&method=scale&allow_remote=true",
                    body=b"room-thumbnail",
                    content_type="image/webp",
                )

            response = test_client.get("/api/matrix/rooms/avatar", params={"room_id": "!test:example.org"})

            assert matrix.requests
            assert all(
                call.kwargs["headers"]["Authorization"] == "Bearer room-token"
                for calls in matrix.requests.values()
                for call in calls
            )
        assert response.status_code == expected, response.text
        if expected == 200:
            assert response.content == b"room-thumbnail"
            assert response.headers["content-type"] == "image/webp"

    def test_room_avatar_uses_router_for_room_only_metadata(self, test_client: TestClient) -> None:
        """Room metadata without an assigned entity remains readable through the configured router."""
        _add_room_metadata_to_runtime_config("announcements")
        runtime_paths = main._app_runtime_paths(main.app)
        _save_matrix_room(runtime_paths, "announcements", "!announcements:example.org")
        _save_matrix_identity(
            runtime_paths,
            ROUTER_AGENT_NAME,
            username="router_bot",
            access_token="router-token",  # noqa: S106
        )

        with aioresponses() as matrix:
            matrix.get(
                re.compile(r"http://localhost:8008/_matrix/client/v3/rooms/.*/state/m\.room\.avatar"),
                payload={"url": "mxc://example.org/announcements-avatar"},
            )
            matrix.get(
                re.compile(r"http://localhost:8008/_matrix/client/v1/media/thumbnail/.*"),
                body=b"room-thumbnail",
                content_type="image/jpeg",
            )

            response = test_client.get("/api/matrix/rooms/avatar", params={"room_id": "announcements"})

            assert response.status_code == 200, (response.text, list(matrix.requests))
            assert all(
                call.kwargs["headers"]["Authorization"] == "Bearer router-token"
                for calls in matrix.requests.values()
                for call in calls
            )

    def test_room_avatar_maps_malformed_upstream_and_closes_clients(self, test_client: TestClient) -> None:
        """Malformed room state becomes a bounded upstream error and closes every attempted identity."""
        runtime_paths = main._app_runtime_paths(main.app)
        _save_matrix_room(runtime_paths, "test_room", "!test:example.org")
        malformed_content: Any = []
        clients = [AsyncMock(), AsyncMock()]
        for matrix_client in clients:
            matrix_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
                malformed_content,
                "m.room.avatar",
                "",
                "!test:example.org",
            )
            matrix_client.close = AsyncMock()

        with patch(
            "mindroom.api.matrix_operations.create_agent_http_client",
            side_effect=clients,
        ):
            response = test_client.get("/api/matrix/rooms/avatar", params={"room_id": "test_room"})

        assert response.status_code == 502
        for matrix_client in clients:
            matrix_client.close.assert_awaited_once_with()

    def test_room_avatar_maps_matrix_thumbnail_error_to_upstream_failure(self, test_client: TestClient) -> None:
        """A Matrix error response from room thumbnail lookup is returned as an upstream failure."""
        runtime_paths = main._app_runtime_paths(main.app)
        _save_matrix_room(runtime_paths, "test_room", "!test:example.org")
        matrix_client = AsyncMock()
        matrix_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
            {"url": "mxc://example.org/room-avatar"},
            "m.room.avatar",
            "",
            "!test:example.org",
        )
        matrix_client.thumbnail.return_value = nio.ThumbnailError(
            "server error",
            status_code="M_UNKNOWN",
        )
        matrix_client.close = AsyncMock()

        with patch(
            "mindroom.api.matrix_operations.create_agent_http_client",
            return_value=matrix_client,
        ):
            response = test_client.get("/api/matrix/rooms/avatar", params={"room_id": "test_room"})

        assert response.status_code == 502
        matrix_client.close.assert_awaited_once_with()

    def test_room_avatar_rejects_unconfigured_room_without_matrix_access(self, test_client: TestClient) -> None:
        """An arbitrary Matrix room ID cannot turn the dashboard into a room-media oracle."""
        with aioresponses() as matrix:
            response = test_client.get(
                "/api/matrix/rooms/avatar",
                params={"room_id": "!private:example.org"},
            )
            assert not matrix.requests
        assert response.status_code == 404
        assert response.json()["detail"] == "Room avatar is not available"

    @pytest.mark.asyncio
    async def test_get_all_agents_rooms(
        self,
        test_client: TestClient,
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test getting room information for configured agents and teams."""
        _add_test_team_to_runtime_config()

        with (
            patch(
                "mindroom.api.matrix_operations.create_agent_http_client",
                return_value=mock_matrix_client,
            ),
            patch(
                "mindroom.api.matrix_operations.get_joined_rooms",
                return_value=["test_room", "team_room", "!extra_room:localhost", "!dm_room:localhost"],
            ),
            patch(
                "mindroom.matrix.rooms.is_dm_room",
                side_effect=lambda _client, room_id: room_id == "!dm_room:localhost",
            ),
        ):
            response = test_client.get("/api/matrix/agents/rooms")

            assert response.status_code == 200
            data = response.json()
            assert "agents" in data
            assert len(data["agents"]) == 2

            entities_by_id = {entity["agent_id"]: entity for entity in data["agents"]}

            assert set(entities_by_id) == {"test_agent", "test_team"}
            assert entities_by_id["test_agent"]["display_name"] == "Test Agent"
            assert "test_room" in entities_by_id["test_agent"]["configured_rooms"]
            assert "!extra_room:localhost" in entities_by_id["test_agent"]["unconfigured_rooms"]
            assert "!dm_room:localhost" not in entities_by_id["test_agent"]["unconfigured_rooms"]

            assert entities_by_id["test_team"]["display_name"] == "Test Team"
            assert "team_room" in entities_by_id["test_team"]["configured_rooms"]
            assert "!extra_room:localhost" in entities_by_id["test_team"]["unconfigured_rooms"]
            assert "!dm_room:localhost" not in entities_by_id["test_team"]["unconfigured_rooms"]

    @pytest.mark.asyncio
    async def test_get_specific_agent_rooms(
        self,
        test_client: TestClient,
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test getting room information for a specific agent."""
        with (
            patch(
                "mindroom.api.matrix_operations.create_agent_http_client",
                return_value=mock_matrix_client,
            ),
            patch(
                "mindroom.api.matrix_operations.get_joined_rooms",
                return_value=["test_room", "!extra_room:localhost"],
            ),
        ):
            response = test_client.get("/api/matrix/agents/test_agent/rooms")

            assert response.status_code == 200
            data = response.json()
            assert data["agent_id"] == "test_agent"
            assert data["display_name"] == "Test Agent"
            assert len(data["configured_rooms"]) == 1
            assert len(data["unconfigured_rooms"]) == 1
            assert "!extra_room:localhost" in data["unconfigured_rooms"]

    @pytest.mark.asyncio
    async def test_get_specific_team_rooms(
        self,
        test_client: TestClient,
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test getting rooms for a specific configured team."""
        _add_test_team_to_runtime_config()

        with (
            patch(
                "mindroom.api.matrix_operations.create_agent_http_client",
                return_value=mock_matrix_client,
            ),
            patch(
                "mindroom.api.matrix_operations.get_joined_rooms",
                return_value=["team_room", "!external_room:localhost"],
            ),
        ):
            response = test_client.get("/api/matrix/agents/test_team/rooms")

            assert response.status_code == 200
            data = response.json()
            assert data["agent_id"] == "test_team"
            assert data["display_name"] == "Test Team"
            assert data["configured_rooms"] == ["team_room"]
            assert data["unconfigured_rooms"] == ["!external_room:localhost"]

    @pytest.mark.asyncio
    async def test_get_agent_rooms_treats_trigger_only_room_as_unconfigured(
        self,
        tmp_path: Path,
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Tool-managed trigger rooms should not widen authored room membership."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "models": {"default": {"provider": "ollama", "id": "test-model"}},
                    "agents": {
                        "test_agent": {
                            "display_name": "Test Agent",
                            "role": "A test agent",
                            "rooms": ["test_room"],
                        },
                    },
                },
            ),
            encoding="utf-8",
        )
        runtime_paths = constants.resolve_primary_runtime_paths(config_path=config_path, process_env={})
        main.initialize_api_app(main.app, runtime_paths)
        assert config_lifecycle.load_config_into_app(runtime_paths, main.app) is True

        with (
            patch(
                "mindroom.api.matrix_operations.create_agent_http_client",
                return_value=mock_matrix_client,
            ),
            patch(
                "mindroom.api.matrix_operations.get_joined_rooms",
                return_value=["test_room", "!campground:localhost", "!extra_room:localhost"],
            ),
        ):
            response = TestClient(main.app, base_url="http://localhost").get("/api/matrix/agents/test_agent/rooms")

        assert response.status_code == 200
        data = response.json()
        assert data["configured_rooms"] == ["test_room"]
        assert data["unconfigured_rooms"] == ["!campground:localhost", "!extra_room:localhost"]

    @pytest.mark.asyncio
    async def test_get_agent_rooms_not_found(self, test_client: TestClient) -> None:
        """Test getting rooms for non-existent agent."""
        response = test_client.get("/api/matrix/agents/nonexistent/rooms")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_leave_room(
        self,
        test_client: TestClient,
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test leaving a room."""
        with (
            patch(
                "mindroom.api.matrix_operations.create_agent_http_client",
                return_value=mock_matrix_client,
            ),
            patch.object(config_lifecycle.app_state(main.app), "leave_matrix_room", AsyncMock(return_value=True)),
        ):
            response = test_client.post(
                "/api/matrix/rooms/leave",
                json={"agent_id": "test_agent", "room_id": "!room_to_leave:localhost"},
            )

            assert response.status_code == 200
            assert response.json()["success"] is True

    @pytest.mark.asyncio
    async def test_leave_room_failure(
        self,
        test_client: TestClient,
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test failing to leave a room."""
        with (
            patch(
                "mindroom.api.matrix_operations.create_agent_http_client",
                return_value=mock_matrix_client,
            ),
            patch.object(config_lifecycle.app_state(main.app), "leave_matrix_room", AsyncMock(return_value=False)),
        ):
            response = test_client.post(
                "/api/matrix/rooms/leave",
                json={"agent_id": "test_agent", "room_id": "!room_to_leave:localhost"},
            )

            assert response.status_code == 500
            assert "Failed to leave room" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_leave_room_for_team(
        self,
        test_client: TestClient,
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test leaving a room for a configured team."""
        _add_test_team_to_runtime_config()

        with (
            patch(
                "mindroom.api.matrix_operations.create_agent_http_client",
                return_value=mock_matrix_client,
            ),
            patch.object(config_lifecycle.app_state(main.app), "leave_matrix_room", AsyncMock(return_value=True)),
        ):
            response = test_client.post(
                "/api/matrix/rooms/leave",
                json={"agent_id": "test_team", "room_id": "!room_to_leave:localhost"},
            )

            assert response.status_code == 200
            assert response.json()["success"] is True

    @pytest.mark.asyncio
    async def test_leave_room_agent_not_found(self, test_client: TestClient) -> None:
        """Test leaving room with non-existent agent."""
        response = test_client.post(
            "/api/matrix/rooms/leave",
            json={"agent_id": "nonexistent", "room_id": "!room:localhost"},
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_leave_rooms_bulk(
        self,
        test_client: TestClient,
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test bulk leaving rooms."""
        with (
            patch(
                "mindroom.api.matrix_operations.create_agent_http_client",
                return_value=mock_matrix_client,
            ),
            patch.object(config_lifecycle.app_state(main.app), "leave_matrix_room", AsyncMock(return_value=True)),
        ):
            requests = [
                {"agent_id": "test_agent", "room_id": "!room1:localhost"},
                {"agent_id": "test_agent", "room_id": "!room2:localhost"},
            ]

            response = test_client.post("/api/matrix/rooms/leave-bulk", json=requests)

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert len(data["results"]) == 2
            assert all(r["success"] for r in data["results"])

    @pytest.mark.asyncio
    async def test_leave_rooms_bulk_partial_failure(
        self,
        test_client: TestClient,
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test bulk leaving rooms with partial failure."""
        # Mock different behaviors for different calls
        leave_room_results = [True, False]
        leave_room_mock = AsyncMock(side_effect=leave_room_results)

        with (
            patch(
                "mindroom.api.matrix_operations.create_agent_http_client",
                return_value=mock_matrix_client,
            ),
            patch.object(config_lifecycle.app_state(main.app), "leave_matrix_room", leave_room_mock),
        ):
            requests = [
                {"agent_id": "test_agent", "room_id": "!room1:localhost"},
                {"agent_id": "test_agent", "room_id": "!room2:localhost"},
            ]

            response = test_client.post("/api/matrix/rooms/leave-bulk", json=requests)

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is False  # Overall failure due to partial failure
            assert len(data["results"]) == 2
            assert data["results"][0]["success"] is True
            assert data["results"][1]["success"] is False

    @pytest.mark.asyncio
    async def test_get_agent_rooms_uses_one_runtime_snapshot(
        self,
        test_client: TestClient,
        tmp_path: Path,
        mock_matrix_client: Any,  # noqa: ANN401
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Agent-room reads should use the same runtime snapshot as the committed config read."""
        first_runtime = constants.resolve_primary_runtime_paths(config_path=tmp_path / "first.yaml", process_env={})
        second_runtime = constants.resolve_primary_runtime_paths(config_path=tmp_path / "second.yaml", process_env={})

        def _fake_runtime_snapshot_read(_request: Any) -> tuple[Config, constants.RuntimePaths]:  # noqa: ANN401
            main.initialize_api_app(main.app, second_runtime)
            return Config(
                agents={
                    "old_agent": AgentConfig(
                        display_name="Old",
                        role="Test",
                        rooms=[],
                    ),
                },
            ), first_runtime

        monkeypatch.setattr(
            "mindroom.api.matrix_operations.read_committed_runtime_config",
            _fake_runtime_snapshot_read,
        )

        with (
            patch(
                "mindroom.api.matrix_operations.create_agent_http_client",
                return_value=mock_matrix_client,
            ) as create_client,
            patch("mindroom.api.matrix_operations.get_joined_rooms", return_value=[]),
        ):
            response = test_client.get("/api/matrix/agents/old_agent/rooms")

        assert response.status_code == 200
        assert create_client.call_args.args[1] == first_runtime

    @pytest.mark.asyncio
    async def test_leave_room_refuses_replaced_installation(
        self,
        test_client: TestClient,
        tmp_path: Path,
        mock_matrix_client: Any,  # noqa: ANN401
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A runtime path swap clears the old membership owner before a leave can run."""
        first_runtime = constants.resolve_primary_runtime_paths(config_path=tmp_path / "first.yaml", process_env={})
        second_runtime = constants.resolve_primary_runtime_paths(config_path=tmp_path / "second.yaml", process_env={})

        def _fake_snapshot_read(
            _request: Any,  # noqa: ANN401
            reader: Any,  # noqa: ANN401
        ) -> tuple[dict[str, Any], constants.RuntimePaths]:
            main.initialize_api_app(main.app, second_runtime)
            return (
                reader(
                    {
                        "agents": {
                            "old_agent": {
                                "display_name": "Old",
                                "role": "Test",
                                "rooms": [],
                            },
                        },
                    },
                ),
                first_runtime,
            )

        monkeypatch.setattr(
            "mindroom.api.matrix_operations.read_committed_config_and_runtime",
            _fake_snapshot_read,
        )

        with (
            patch(
                "mindroom.api.matrix_operations.create_agent_http_client",
                return_value=mock_matrix_client,
            ),
            patch.object(config_lifecycle.app_state(main.app), "leave_matrix_room", AsyncMock(return_value=True)),
        ):
            response = test_client.post(
                "/api/matrix/rooms/leave",
                json={"agent_id": "old_agent", "room_id": "!room:localhost"},
            )

        assert response.status_code == 503


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/matrix/agents/rooms", None),
        ("get", "/api/matrix/agents/test_agent/rooms", None),
        ("post", "/api/matrix/rooms/leave", {"agent_id": "test_agent", "room_id": "!room:localhost"}),
        (
            "post",
            "/api/matrix/rooms/leave-bulk",
            [{"agent_id": "test_agent", "room_id": "!room:localhost"}],
        ),
    ],
)
def test_matrix_operations_refuse_stale_config_after_invalid_reload(
    test_client: TestClient,
    temp_config_file: Path,
    mock_matrix_client: Any,  # noqa: ANN401
    method: str,
    path: str,
    payload: dict[str, Any] | list[dict[str, Any]] | None,
) -> None:
    """Matrix operations should surface malformed current config instead of stale cached entities."""
    runtime_paths = main._app_runtime_paths(main.app)
    temp_config_file.write_text("agents:\n  broken: [\n", encoding="utf-8")
    assert config_lifecycle.load_config_into_app(runtime_paths, main.app) is False

    with (
        patch(
            "mindroom.api.matrix_operations.create_agent_http_client",
            return_value=mock_matrix_client,
        ),
        patch("mindroom.api.matrix_operations.get_joined_rooms", return_value=["test_room"]),
        patch.object(config_lifecycle.app_state(main.app), "leave_matrix_room", AsyncMock(return_value=True)),
    ):
        if payload is None:
            response = getattr(test_client, method)(path)
        else:
            response = getattr(test_client, method)(path, json=payload)

    assert response.status_code == 422
    assert "Could not parse configuration YAML" in response.json()["detail"][0]["msg"]
