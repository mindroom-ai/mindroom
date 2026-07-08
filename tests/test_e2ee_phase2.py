"""Tests for managed-room encryption enablement, encryption commands, and store recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.commands.encryption_commands import handle_e2ee_command, handle_encrypt_command
from mindroom.commands.parsing import CommandType, command_parser
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.matrix.client_room_admin import create_room, ensure_room_encryption_enabled
from mindroom.matrix.client_session import olm_store_dir, olm_store_exists
from mindroom.matrix.rooms import _managed_room_should_be_encrypted
from mindroom.matrix.users import AgentMatrixUser, login_agent_user

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_ENCRYPTION_STATE = {"type": "m.room.encryption", "state_key": "", "content": {"algorithm": "m.megolm.v1.aes-sha2"}}


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data")


def _state_error(status_code: str) -> nio.RoomGetStateEventError:
    return nio.RoomGetStateEventError.from_dict({"errcode": status_code, "error": status_code}, "!room:localhost")


def _state_present() -> nio.RoomGetStateEventResponse:
    return nio.RoomGetStateEventResponse(
        content={"algorithm": "m.megolm.v1.aes-sha2"},
        event_type="m.room.encryption",
        state_key="",
        room_id="!room:localhost",
    )


class TestRoomCreationEncryption:
    """create_room should include the encryption state event only when requested."""

    @pytest.mark.asyncio
    async def test_encrypted_room_creation_includes_encryption_state(self) -> None:
        """Encrypted creation must include the m.room.encryption state event."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = "@bot:localhost"
        client.room_create.return_value = nio.RoomCreateResponse(room_id="!new:localhost")

        await create_room(client, "Secure", encrypted=True)

        initial_state = client.room_create.await_args.kwargs["initial_state"]
        assert _ENCRYPTION_STATE in initial_state

    @pytest.mark.asyncio
    async def test_unencrypted_room_creation_omits_encryption_state(self) -> None:
        """Default creation must stay unencrypted."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = "@bot:localhost"
        client.room_create.return_value = nio.RoomCreateResponse(room_id="!new:localhost")

        await create_room(client, "Plain")

        initial_state = client.room_create.await_args.kwargs["initial_state"]
        assert all(event["type"] != "m.room.encryption" for event in initial_state)


class TestEnsureRoomEncryptionEnabled:
    """Enable-only reconciliation of room encryption state."""

    @pytest.mark.asyncio
    async def test_noop_when_already_encrypted(self) -> None:
        """An encrypted room needs no state change."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_get_state_event.return_value = _state_present()

        assert await ensure_room_encryption_enabled(client, "!room:localhost") is True
        client.room_put_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enables_when_missing(self) -> None:
        """A missing encryption state event is added once."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_get_state_event.return_value = _state_error("M_NOT_FOUND")
        client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$state", room_id="!room:localhost")

        assert await ensure_room_encryption_enabled(client, "!room:localhost") is True
        client.room_put_state.assert_awaited_once_with(
            "!room:localhost",
            "m.room.encryption",
            {"algorithm": "m.megolm.v1.aes-sha2"},
        )

    @pytest.mark.asyncio
    async def test_reports_failure_when_put_state_rejected(self) -> None:
        """A rejected state change reports failure."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_get_state_event.return_value = _state_error("M_NOT_FOUND")
        client.room_put_state.return_value = nio.RoomPutStateError.from_dict(
            {"errcode": "M_FORBIDDEN", "error": "no permission"},
            "!room:localhost",
        )

        assert await ensure_room_encryption_enabled(client, "!room:localhost") is False


class TestManagedRoomEncryptionConfig:
    """Per-room and global managed-room encryption configuration."""

    def test_defaults_to_unencrypted(self) -> None:
        """Managed rooms stay unencrypted without configuration."""
        config = Config()
        assert _managed_room_should_be_encrypted("lobby", config) is False

    def test_global_default_applies(self) -> None:
        """The global default encrypts all managed rooms."""
        config = Config(matrix_room_access={"encrypt_managed_rooms": True})
        assert _managed_room_should_be_encrypted("lobby", config) is True

    def test_per_room_override_wins(self) -> None:
        """Per-room settings override the global default."""
        config = Config(
            matrix_room_access={"encrypt_managed_rooms": True},
            rooms={"plain": {"encrypted": False}, "secure": {"encrypted": True}},
        )
        assert _managed_room_should_be_encrypted("plain", config) is False
        assert _managed_room_should_be_encrypted("secure", config) is True
        assert _managed_room_should_be_encrypted("other", config) is True


class TestEncryptCommand:
    """`!encrypt` review/confirm flow."""

    @pytest.mark.asyncio
    async def test_already_encrypted_room_reports_status(self) -> None:
        """An encrypted room reports its status."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_get_state_event.return_value = _state_present()

        response = await handle_encrypt_command(
            "",
            client=client,
            room_id="!room:localhost",
            requester_user_id="@user:localhost",
            sender_user_id="@user:localhost",
        )

        assert "already end-to-end encrypted" in response

    @pytest.mark.asyncio
    async def test_review_warns_about_irreversibility(self) -> None:
        """The review warns before any change."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_get_state_event.return_value = _state_error("M_NOT_FOUND")

        response = await handle_encrypt_command(
            "",
            client=client,
            room_id="!room:localhost",
            requester_user_id="@user:localhost",
            sender_user_id="@user:localhost",
        )

        assert "irreversible" in response
        assert "!encrypt confirm" in response
        client.room_put_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confirm_requires_room_admin(self) -> None:
        """Non-admins cannot enable encryption."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_get_state_event.return_value = _state_error("M_NOT_FOUND")

        with patch(
            "mindroom.commands.encryption_commands.room_admin_power_user",
            new=AsyncMock(return_value=None),
        ):
            response = await handle_encrypt_command(
                "confirm",
                client=client,
                room_id="!room:localhost",
                requester_user_id="@user:localhost",
                sender_user_id="@user:localhost",
            )

        assert "Room admin only" in response
        client.room_put_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confirm_enables_encryption_for_admin(self) -> None:
        """Admins can enable encryption with confirm."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_get_state_event.return_value = _state_error("M_NOT_FOUND")
        client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$state", room_id="!room:localhost")

        with patch(
            "mindroom.commands.encryption_commands.room_admin_power_user",
            new=AsyncMock(return_value="@user:localhost"),
        ):
            response = await handle_encrypt_command(
                "confirm",
                client=client,
                room_id="!room:localhost",
                requester_user_id="@user:localhost",
                sender_user_id="@user:localhost",
            )

        assert "now enabled" in response
        client.room_put_state.assert_awaited_once()


class TestE2EECommand:
    """`!e2ee` diagnostics output."""

    @pytest.mark.asyncio
    async def test_reports_room_state_and_device(self) -> None:
        """Diagnostics include room state and bot device."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.room_get_state_event.return_value = _state_present()
        client.user_id = "@mindroom_assistant:localhost"
        client.device_id = "DEVICEID"
        client.olm = MagicMock()

        response = await handle_e2ee_command(client=client, room_id="!room:localhost")

        assert "Room: encrypted" in response
        assert "@mindroom_assistant:localhost" in response
        assert "DEVICEID" in response
        assert "Cross-signing: not yet supported" in response


class TestCommandParsing:
    """Parser coverage for the new commands."""

    def test_encrypt_parses(self) -> None:
        """The bare command parses."""
        command = command_parser.parse("!encrypt")
        assert command is not None
        assert command.type == CommandType.ENCRYPT
        assert command.args == {"args_text": ""}

    def test_encrypt_confirm_parses(self) -> None:
        """The confirm argument parses."""
        command = command_parser.parse("!encrypt confirm")
        assert command is not None
        assert command.type == CommandType.ENCRYPT
        assert command.args == {"args_text": "confirm"}

    def test_e2ee_parses(self) -> None:
        """The diagnostics command parses."""
        command = command_parser.parse("!e2ee")
        assert command is not None
        assert command.type == CommandType.E2EE


class TestStoreLossFallback:
    """Missing olm stores must trigger a fresh-device login instead of a wedged restore."""

    @pytest.mark.asyncio
    async def test_missing_store_skips_restore_and_logs_in_fresh(self, tmp_path: Path) -> None:
        """A lost store must produce a fresh device."""
        runtime_paths = _runtime_paths(tmp_path)
        agent_user = AgentMatrixUser(
            agent_name="assistant",
            user_id="@mindroom_assistant:localhost",
            display_name="Assistant",
            password="pw",  # noqa: S106
            device_id="LOSTDEVICE",
            access_token="token",  # noqa: S106
        )
        fresh_client = AsyncMock(spec=nio.AsyncClient)
        fresh_client.user_id = "@mindroom_assistant:localhost"
        fresh_client.device_id = "NEWDEVICE"
        fresh_client.access_token = "new-token"  # noqa: S105

        with (
            patch("mindroom.matrix.users.restore_login", new=AsyncMock()) as mock_restore,
            patch("mindroom.matrix.users.login", new=AsyncMock(return_value=fresh_client)) as mock_login,
        ):
            client = await login_agent_user("http://localhost:8008", agent_user, runtime_paths=runtime_paths)

        mock_restore.assert_not_awaited()
        mock_login.assert_awaited_once()
        assert client is fresh_client
        assert agent_user.device_id == "NEWDEVICE"

    @pytest.mark.asyncio
    async def test_present_store_restores_session(self, tmp_path: Path) -> None:
        """An intact store restores the persisted session."""
        runtime_paths = _runtime_paths(tmp_path)
        user_id = "@mindroom_assistant:localhost"
        store_dir = olm_store_dir(user_id, runtime_paths)
        store_dir.mkdir(parents=True)
        (store_dir / f"{user_id}_GOODDEVICE.db").write_bytes(b"")
        assert olm_store_exists(user_id, "GOODDEVICE", runtime_paths)

        agent_user = AgentMatrixUser(
            agent_name="assistant",
            user_id=user_id,
            display_name="Assistant",
            password="pw",  # noqa: S106
            device_id="GOODDEVICE",
            access_token="token",  # noqa: S106
        )
        restored_client = AsyncMock(spec=nio.AsyncClient)
        restored_client.user_id = user_id
        restored_client.device_id = "GOODDEVICE"
        restored_client.access_token = "token"  # noqa: S105

        with (
            patch("mindroom.matrix.users.restore_login", new=AsyncMock(return_value=restored_client)) as mock_restore,
            patch("mindroom.matrix.users.login", new=AsyncMock()) as mock_login,
        ):
            client = await login_agent_user("http://localhost:8008", agent_user, runtime_paths=runtime_paths)

        mock_restore.assert_awaited_once()
        mock_login.assert_not_awaited()
        assert client is restored_client
