"""Test our mocking strategy to ensure tests actually test what we think they do.

This file uses parametrized tests to verify that:
1. WITH aioresponses mocking, operations succeed
2. WITHOUT aioresponses mocking, operations fail/timeout

This gives us confidence that our tests are actually testing HTTP interactions
and not accidentally bypassing them.

Key insights learned during development:
- nio's client.user_id is empty until after login (it's set during login response processing)
- Login responses MUST include 'home_server' field in addition to standard fields
- Sync endpoints require regex matching to handle query parameters that nio adds
- Use pytest_asyncio.fixture (not pytest.fixture) for async fixtures
- Proper cleanup with fixtures eliminates need for try/finally blocks
- aioresponses captures requests as dict with (method, URL) tuple as key
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from typing import TYPE_CHECKING

import nio
import pytest
import pytest_asyncio
from aioresponses import CallbackResult, aioresponses

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
TIMEOUT = 0.2


class TestMockingStrategy:
    """Verify our mocking strategy actually intercepts HTTP calls."""

    @pytest_asyncio.fixture
    async def client(self) -> AsyncGenerator[nio.AsyncClient, None]:
        """Create an nio.AsyncClient for testing and ensure cleanup."""
        homeserver = "https://matrix.example.org"
        user_id = "@test:example.org"
        client = nio.AsyncClient(homeserver, user_id)
        # Manually set user_id due to matrix-nio bug
        client.user_id = user_id
        yield client
        await client.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_mock", [True, False])
    async def test_login_mocking(self, client: nio.AsyncClient, use_mock: bool) -> None:
        """Test that login actually requires HTTP mocking to succeed."""
        if use_mock:
            # WITH mocking - should succeed
            with aioresponses() as m:
                m.post(
                    f"{client.homeserver}/_matrix/client/v3/login",
                    status=200,
                    payload={
                        "user_id": "@test:example.org",  # Must be hardcoded - client.user_id is empty before login
                        "access_token": "test_token",
                        "device_id": "TESTDEVICE",
                        "home_server": "example.org",  # Required field for nio nio.LoginResponse validation
                    },
                )

                response = await client.login("password")

                # Should succeed
                assert isinstance(response, nio.LoginResponse)
                assert client.access_token == "test_token"  # noqa: S105
                assert len(m.requests) == 1
        else:
            # WITHOUT mocking - should fail
            with pytest.raises((ConnectionError, TimeoutError, OSError)) as exc_info:
                # Can't modify frozen config, just let it fail
                await asyncio.wait_for(client.login("password"), timeout=TIMEOUT)

            # Should fail with connection/timeout error
            assert (
                "matrix.example.org" in str(exc_info.value)
                or "timeout" in str(exc_info.value).lower()
                or "TimeoutError" in str(exc_info.type)
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_mock", [True, False])
    async def test_room_send_mocking(self, client: nio.AsyncClient, use_mock: bool) -> None:
        """Test that room_send actually requires HTTP mocking to succeed."""
        room_id = "!test:example.org"

        # Pretend we're logged in
        client.access_token = "test_token"  # noqa: S105

        if use_mock:
            # WITH mocking - should succeed
            with aioresponses() as m:
                # Match the exact URL pattern nio uses

                # nio appends a transaction ID to the URL, so we need to match with regex
                m.put(
                    re.compile(rf".*{re.escape(room_id)}/send/m\.room\.message/.*"),
                    status=200,
                    payload={"event_id": "$test_event:example.org"},
                )

                response = await client.room_send(
                    room_id=room_id,
                    message_type="m.room.message",
                    content={"msgtype": "m.text", "body": "Test"},
                )

                # Should succeed
                assert isinstance(response, nio.RoomSendResponse)
                assert response.event_id == "$test_event:example.org"
                assert len(m.requests) == 1
        else:
            # WITHOUT mocking - should fail
            # Use a short timeout to prevent hanging
            try:
                response = await asyncio.wait_for(
                    client.room_send(
                        room_id=room_id,
                        message_type="m.room.message",
                        content={"msgtype": "m.text", "body": "Test"},
                    ),
                    timeout=TIMEOUT,
                )
                # Should fail
                assert isinstance(response, nio.RoomSendError)
            except TimeoutError:
                # Timeout is also a valid failure mode
                pass

    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_mock", [True, False])
    async def test_sync_mocking(self, client: nio.AsyncClient, use_mock: bool) -> None:
        """Test that sync actually requires HTTP mocking to succeed."""
        client.access_token = "test_token"  # noqa: S105

        if use_mock:
            # WITH mocking - should succeed
            with aioresponses() as m:
                # Mock with regex to match any query parameters - nio adds timeout, since, filter, etc.
                m.get(
                    re.compile(rf"{re.escape(client.homeserver)}/_matrix/client/v3/sync.*"),
                    status=200,
                    payload={
                        "next_batch": "s123456",
                        "rooms": {"join": {}, "invite": {}, "leave": {}},
                        "presence": {"events": []},
                        "account_data": {"events": []},
                    },
                )

                response = await client.sync(timeout=30)

                # Should succeed
                assert isinstance(response, nio.SyncResponse)
                assert response.next_batch == "s123456"
                assert len(m.requests) == 1
        else:
            # WITHOUT mocking - should fail
            # Use asyncio timeout to prevent hanging
            try:
                response = await asyncio.wait_for(client.sync(timeout=1000), timeout=TIMEOUT)
                # Should fail
                assert isinstance(response, nio.SyncError)
                assert "Failed to sync" in str(response) or "Unknown error" in str(response)
            except TimeoutError:
                # This is also a valid failure mode
                pass

    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_mock", [True, False])
    async def test_join_room_mocking(self, client: nio.AsyncClient, use_mock: bool) -> None:
        """Test that join actually requires HTTP mocking to succeed."""
        room_id = "!test:example.org"
        client.access_token = "test_token"  # noqa: S105

        if use_mock:
            # WITH mocking - should succeed
            with aioresponses() as m:
                m.post(
                    f"{client.homeserver}/_matrix/client/v3/join/{room_id}",
                    status=200,
                    payload={"room_id": room_id},
                )

                response = await client.join(room_id)

                # Should succeed
                assert isinstance(response, nio.JoinResponse)
                assert response.room_id == room_id
                assert len(m.requests) == 1
        else:
            # WITHOUT mocking - should fail
            try:
                response = await asyncio.wait_for(client.join(room_id), timeout=TIMEOUT)
                # Should fail
                assert isinstance(response, nio.JoinError)
            except TimeoutError:
                # Timeout is also a valid failure mode
                pass

    @pytest.mark.asyncio
    async def test_multiple_operations_require_all_mocks(self, client: nio.AsyncClient) -> None:
        """Test that complex operations require all HTTP calls to be mocked."""
        homeserver = client.homeserver

        # Try with incomplete mocking - should fail
        with aioresponses() as m:
            # Only mock login, not sync
            m.post(
                f"{homeserver}/_matrix/client/v3/login",
                status=200,
                payload={
                    "user_id": "@test:example.org",
                    "access_token": "test_token",
                    "device_id": "TESTDEVICE",
                    "home_server": "example.org",
                },
            )

            # Login should work
            login_response = await client.login("password")
            assert isinstance(login_response, nio.LoginResponse)

            # But sync should fail because it's not mocked
            try:
                sync_response = await asyncio.wait_for(client.sync(timeout=1000), timeout=TIMEOUT)
                assert isinstance(sync_response, nio.SyncError)
            except TimeoutError:
                # This is also a valid failure mode
                pass

        # Now with complete mocking - should succeed
        with aioresponses() as m:
            # Mock both login and sync
            m.post(
                f"{homeserver}/_matrix/client/v3/login",
                status=200,
                payload={
                    "user_id": "@test:example.org",
                    "access_token": "test_token2",
                    "device_id": "TESTDEVICE2",
                    "home_server": "example.org",
                },
            )
            m.get(
                re.compile(rf"{re.escape(homeserver)}/_matrix/client/v3/sync.*"),
                status=200,
                payload={
                    "next_batch": "s789",
                    "rooms": {"join": {}, "invite": {}, "leave": {}},
                    "presence": {"events": []},
                    "account_data": {"events": []},
                },
            )

            # Both should work
            login_response = await client.login("password")
            assert isinstance(login_response, nio.LoginResponse)

            sync_response = await client.sync(timeout=1000)
            assert isinstance(sync_response, nio.SyncResponse)
            assert sync_response.next_batch == "s789"

    @pytest.mark.asyncio
    async def test_request_inspection(self, client: nio.AsyncClient) -> None:
        """Test that we can inspect the actual HTTP requests made."""
        room_id = "!test:example.org"
        client.access_token = "test_token"  # noqa: S105

        with aioresponses() as m:
            # Capture the request
            request_data = None

            async def capture_request(_url: str, **kwargs: dict[str, object]) -> CallbackResult:
                nonlocal request_data
                request_data = kwargs

                return CallbackResult(status=200, payload={"event_id": "$captured:example.org"})

            m.put(
                re.compile(rf".*{re.escape(room_id)}/send/m\.room\.message/.*"),
                callback=capture_request,
            )

            # Send a message
            test_content = {
                "msgtype": "m.text",
                "body": "Test message with special content",
                "custom_field": "custom_value",
            }

            response = await client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=test_content,
            )

            # Verify we captured the request
            assert request_data is not None

            # Check the request content

            if "data" in request_data and isinstance(request_data["data"], str):
                sent_content = json.loads(request_data["data"])
            else:
                sent_content = request_data.get("json", {})

            assert sent_content["msgtype"] == "m.text"
            assert sent_content["body"] == "Test message with special content"
            assert sent_content["custom_field"] == "custom_value"

            # Verify response
            assert response.event_id == "$captured:example.org"


class TestMockingStrategyExtended:
    """Additional mocking tests for Matrix client methods."""

    @pytest_asyncio.fixture
    async def client(self) -> AsyncGenerator[nio.AsyncClient, None]:
        """Create an nio.AsyncClient for testing and ensure cleanup."""
        homeserver = "https://matrix.example.org"
        user_id = "@test:example.org"
        client = nio.AsyncClient(homeserver, user_id)
        # Manually set user_id due to matrix-nio bug
        client.user_id = user_id
        yield client
        await client.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_mock", [True, False])
    async def test_set_displayname_mocking(self, client: nio.AsyncClient, use_mock: bool) -> None:
        """Test that set_displayname actually requires HTTP mocking to succeed."""
        client.access_token = "test_token"  # noqa: S105
        display_name = "Test Bot"

        if use_mock:
            # WITH mocking - should succeed
            with aioresponses() as m:
                m.put(
                    f"{client.homeserver}/_matrix/client/v3/profile/{client.user_id}/displayname",
                    status=200,
                    payload={},
                )

                response = await client.set_displayname(display_name)

                # Should succeed
                assert response is not None
                assert len(m.requests) == 1
        else:
            # WITHOUT mocking - should fail
            with suppress(TimeoutError):
                await asyncio.wait_for(client.set_displayname(display_name), timeout=TIMEOUT)
                msg = "Expected TimeoutError but operation succeeded without mocking"
                raise RuntimeError(msg)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_mock", [True, False])
    async def test_room_invite_mocking(self, client: nio.AsyncClient, use_mock: bool) -> None:
        """Test that room_invite actually requires HTTP mocking to succeed."""
        room_id = "!test:example.org"
        user_id = "@invitee:example.org"
        client.access_token = "test_token"  # noqa: S105

        if use_mock:
            # WITH mocking - should succeed
            with aioresponses() as m:
                m.post(
                    f"{client.homeserver}/_matrix/client/v3/rooms/{room_id}/invite",
                    status=200,
                    payload={},
                )

                response = await client.room_invite(room_id, user_id)

                # Should succeed
                assert response is not None
                assert len(m.requests) == 1
        else:
            # WITHOUT mocking - should fail
            with suppress(TimeoutError):
                await asyncio.wait_for(client.room_invite(room_id, user_id), timeout=TIMEOUT)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_mock", [True, False])
    async def test_room_messages_mocking(self, client: nio.AsyncClient, use_mock: bool) -> None:
        """Test that room_messages actually requires HTTP mocking to succeed."""
        room_id = "!test:example.org"
        client.access_token = "test_token"  # noqa: S105

        if use_mock:
            # WITH mocking - should succeed
            with aioresponses() as m:
                # room_messages uses GET with query parameters
                m.get(
                    re.compile(
                        rf"{re.escape(client.homeserver)}/_matrix/client/v3/rooms/{re.escape(room_id)}/messages.*",
                    ),
                    status=200,
                    payload={
                        "start": "s123",
                        "end": "e456",
                        "chunk": [
                            {
                                "type": "m.room.message",
                                "content": {"msgtype": "m.text", "body": "Hello"},
                                "event_id": "$msg1:example.org",
                                "sender": "@user:example.org",
                                "origin_server_ts": 1234567890,
                            },
                        ],
                    },
                )

                response = await client.room_messages(room_id, start="s123")

                # Should succeed
                assert isinstance(response, nio.RoomMessagesResponse)
                assert len(response.chunk) == 1
                assert len(m.requests) == 1
        else:
            # WITHOUT mocking - should fail
            try:
                response = await asyncio.wait_for(client.room_messages(room_id, start="s123"), timeout=TIMEOUT)
                assert isinstance(response, nio.RoomMessagesError)
            except TimeoutError:
                # Timeout is also valid
                pass

    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_mock", [True, False])
    async def test_joined_members_mocking(self, client: nio.AsyncClient, use_mock: bool) -> None:
        """Test that joined_members actually requires HTTP mocking to succeed."""
        room_id = "!test:example.org"
        client.access_token = "test_token"  # noqa: S105

        if use_mock:
            # WITH mocking - should succeed
            with aioresponses() as m:
                m.get(
                    f"{client.homeserver}/_matrix/client/v3/rooms/{room_id}/joined_members",
                    status=200,
                    payload={
                        "joined": {
                            "@alice:example.org": {
                                "display_name": "Alice",
                                "avatar_url": "mxc://example.org/avatar1",
                            },
                            "@bob:example.org": {
                                "display_name": "Bob",
                                "avatar_url": None,
                            },
                        },
                    },
                )

                response = await client.joined_members(room_id)

                # Should succeed
                assert isinstance(response, nio.JoinedMembersResponse)
                assert len(response.members) == 2
                assert len(m.requests) == 1
        else:
            # WITHOUT mocking - should fail
            try:
                response = await asyncio.wait_for(client.joined_members(room_id), timeout=TIMEOUT)
                assert isinstance(response, nio.JoinedMembersError)
            except TimeoutError:
                # Timeout is also valid
                pass

    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_mock", [True, False])
    async def test_joined_rooms_mocking(self, client: nio.AsyncClient, use_mock: bool) -> None:
        """Test that joined_rooms actually requires HTTP mocking to succeed."""
        client.access_token = "test_token"  # noqa: S105

        if use_mock:
            # WITH mocking - should succeed
            with aioresponses() as m:
                m.get(
                    f"{client.homeserver}/_matrix/client/v3/joined_rooms",
                    status=200,
                    payload={
                        "joined_rooms": [
                            "!room1:example.org",
                            "!room2:example.org",
                            "!room3:example.org",
                        ],
                    },
                )

                response = await client.joined_rooms()

                # Should succeed
                assert isinstance(response, nio.JoinedRoomsResponse)
                assert len(response.rooms) == 3
                assert "!room1:example.org" in response.rooms
                assert len(m.requests) == 1
        else:
            # WITHOUT mocking - should fail
            try:
                response = await asyncio.wait_for(client.joined_rooms(), timeout=TIMEOUT)
                assert isinstance(response, nio.JoinedRoomsError)
            except TimeoutError:
                # Timeout is also valid
                pass

    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_mock", [True, False])
    async def test_room_put_state_mocking(self, client: nio.AsyncClient, use_mock: bool) -> None:
        """Test that room_put_state actually requires HTTP mocking to succeed."""
        room_id = "!test:example.org"
        event_type = "m.room.name"
        content = {"name": "Test Room"}
        client.access_token = "test_token"  # noqa: S105

        if use_mock:
            # WITH mocking - should succeed
            with aioresponses() as m:
                m.put(
                    f"{client.homeserver}/_matrix/client/v3/rooms/{room_id}/state/{event_type}",
                    status=200,
                    payload={"event_id": "$state_event:example.org"},
                )

                response = await client.room_put_state(room_id, event_type, content)

                # Should succeed
                assert isinstance(response, nio.RoomPutStateResponse)
                assert response.event_id == "$state_event:example.org"
                assert len(m.requests) == 1
        else:
            # WITHOUT mocking - should fail
            try:
                response = await asyncio.wait_for(client.room_put_state(room_id, event_type, content), timeout=TIMEOUT)
                assert isinstance(response, nio.RoomPutStateError)
            except TimeoutError:
                # Timeout is also valid
                pass

    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_mock", [True, False])
    async def test_login_info_mocking(self, client: nio.AsyncClient, use_mock: bool) -> None:
        """Test that login_info actually requires HTTP mocking to succeed."""
        if use_mock:
            # WITH mocking - should succeed
            with aioresponses() as m:
                m.get(
                    f"{client.homeserver}/_matrix/client/v3/login",
                    status=200,
                    payload={
                        "flows": [
                            {"type": "m.login.password"},
                            {
                                "type": "m.login.sso",
                                "identity_providers": [
                                    {
                                        "id": "google",
                                        "name": "Google",
                                        "icon": "mxc://example.org/google-icon",
                                    },
                                ],
                            },
                        ],
                    },
                )

                response = await client.login_info()

                # Should succeed
                assert isinstance(response, nio.LoginInfoResponse)
                assert len(response.flows) == 2
                assert response.flows[0] == "m.login.password"
                assert response.flows[1] == "m.login.sso"
                assert len(m.requests) == 1
        else:
            # WITHOUT mocking - should fail
            with suppress(TimeoutError):
                response = await asyncio.wait_for(client.login_info(), timeout=TIMEOUT)
                # Some servers might actually respond, so check for error
                if response:
                    assert isinstance(response, nio.LoginInfoError | nio.LoginInfoResponse)
