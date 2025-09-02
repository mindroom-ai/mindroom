"""Tests for Matrix operations API endpoints."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_matrix_client() -> AsyncMock:
    """Create a mock Matrix client."""
    client = AsyncMock()
    client.close = AsyncMock()
    return client


@pytest.fixture
def mock_agent_user() -> MagicMock:
    """Create a mock agent user."""
    user = MagicMock()
    user.agent_name = "test_agent"
    user.user_id = "@mindroom_test_agent:localhost"
    user.display_name = "Test Agent"
    user.password = "test_password"  # noqa: S105
    user.access_token = "test_token"  # noqa: S105
    return user


class TestMatrixOperations:
    """Test Matrix operations API endpoints."""

    @pytest.mark.asyncio
    async def test_get_all_agents_rooms(
        self,
        test_client: TestClient,
        mock_agent_user: Any,  # noqa: ANN401
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test getting room information for all agents."""
        with (
            patch("mindroom.api.matrix_operations.create_agent_user", return_value=mock_agent_user),
            patch("mindroom.api.matrix_operations.login_agent_user", return_value=mock_matrix_client),
            patch(
                "mindroom.api.matrix_operations.get_joined_rooms",
                return_value=["test_room", "!extra_room:localhost", "!dm_room:localhost"],
            ),
        ):
            response = test_client.get("/api/matrix/agents/rooms")

            assert response.status_code == 200
            data = response.json()
            assert "agents" in data
            assert len(data["agents"]) == 1  # One test agent from fixture

            agent = data["agents"][0]
            assert agent["agent_id"] == "test_agent"
            assert agent["display_name"] == "Test Agent"
            assert "test_room" in agent["configured_rooms"]
            assert len(agent["unconfigured_rooms"]) == 2
            assert "!extra_room:localhost" in agent["unconfigured_rooms"]
            assert "!dm_room:localhost" in agent["unconfigured_rooms"]

    @pytest.mark.asyncio
    async def test_get_specific_agent_rooms(
        self,
        test_client: TestClient,
        mock_agent_user: Any,  # noqa: ANN401
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test getting room information for a specific agent."""
        with (
            patch("mindroom.api.matrix_operations.create_agent_user", return_value=mock_agent_user),
            patch("mindroom.api.matrix_operations.login_agent_user", return_value=mock_matrix_client),
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
    async def test_get_agent_rooms_not_found(self, test_client: TestClient) -> None:
        """Test getting rooms for non-existent agent."""
        response = test_client.get("/api/matrix/agents/nonexistent/rooms")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_leave_room(
        self,
        test_client: TestClient,
        mock_agent_user: Any,  # noqa: ANN401
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test leaving a room."""
        with (
            patch("mindroom.api.matrix_operations.create_agent_user", return_value=mock_agent_user),
            patch("mindroom.api.matrix_operations.login_agent_user", return_value=mock_matrix_client),
            patch("mindroom.api.matrix_operations.leave_room", return_value=True),
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
        mock_agent_user: Any,  # noqa: ANN401
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test failing to leave a room."""
        with (
            patch("mindroom.api.matrix_operations.create_agent_user", return_value=mock_agent_user),
            patch("mindroom.api.matrix_operations.login_agent_user", return_value=mock_matrix_client),
            patch("mindroom.api.matrix_operations.leave_room", return_value=False),
        ):
            response = test_client.post(
                "/api/matrix/rooms/leave",
                json={"agent_id": "test_agent", "room_id": "!room_to_leave:localhost"},
            )

            assert response.status_code == 500
            assert "Failed to leave room" in response.json()["detail"]

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
        mock_agent_user: Any,  # noqa: ANN401
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test bulk leaving rooms."""
        with (
            patch("mindroom.api.matrix_operations.create_agent_user", return_value=mock_agent_user),
            patch("mindroom.api.matrix_operations.login_agent_user", return_value=mock_matrix_client),
            patch("mindroom.api.matrix_operations.leave_room", return_value=True),
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
        mock_agent_user: Any,  # noqa: ANN401
        mock_matrix_client: Any,  # noqa: ANN401
    ) -> None:
        """Test bulk leaving rooms with partial failure."""
        # Mock different behaviors for different calls
        leave_room_results = [True, False]
        leave_room_mock = AsyncMock(side_effect=leave_room_results)

        with (
            patch("mindroom.api.matrix_operations.create_agent_user", return_value=mock_agent_user),
            patch("mindroom.api.matrix_operations.login_agent_user", return_value=mock_matrix_client),
            patch("mindroom.api.matrix_operations.leave_room", new=leave_room_mock),
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
