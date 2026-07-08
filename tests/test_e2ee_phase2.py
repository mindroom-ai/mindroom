"""Tests for managed-room encryption enablement, encryption commands, and store recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from nio.store import SqliteStore

from mindroom.commands.encryption_commands import handle_e2ee_command, handle_encrypt_command
from mindroom.commands.parsing import CommandType, command_parser
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.matrix import rooms as matrix_rooms
from mindroom.matrix import state as matrix_state
from mindroom.matrix.client_room_admin import create_room, ensure_room_encryption_enabled
from mindroom.matrix.client_session import olm_store_dir, olm_store_exists
from mindroom.matrix.rooms import _managed_room_should_be_encrypted
from mindroom.matrix.users import AgentMatrixUser, login_agent_user
from tests.conftest import bind_runtime_paths, runtime_paths_for

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
        client.rooms = {}
        client.room_get_state_event.return_value = _state_error("M_NOT_FOUND")
        client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$state", room_id="!room:localhost")

        assert await ensure_room_encryption_enabled(client, "!room:localhost") is True
        client.room_put_state.assert_awaited_once_with(
            "!room:localhost",
            "m.room.encryption",
            {"algorithm": "m.megolm.v1.aes-sha2"},
        )

    @pytest.mark.asyncio
    async def test_enabling_flips_cached_room_encrypted_flag(self) -> None:
        """Sends inside the sync window must already see the room as encrypted."""
        client = AsyncMock(spec=nio.AsyncClient)
        cached_room = MagicMock(spec=nio.MatrixRoom)
        cached_room.encrypted = False
        client.rooms = {"!room:localhost": cached_room}
        client.room_get_state_event.return_value = _state_error("M_NOT_FOUND")
        client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$state", room_id="!room:localhost")

        assert await ensure_room_encryption_enabled(client, "!room:localhost") is True
        assert cached_room.encrypted is True

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


def _bound_config(tmp_path: Path, **config_data: object) -> Config:
    runtime_paths = _runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config.model_validate(config_data, context={"runtime_paths": runtime_paths}),
        runtime_paths,
    )


class TestManagedRoomEncryptionReconcile:
    """The reconcile wiring in _ensure_room_exists must honor the encryption config."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("encrypt_managed_rooms", "expected_calls"), [(True, 1), (False, 0)])
    async def test_existing_room_reconciled_to_encrypted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        encrypt_managed_rooms: bool,
        expected_calls: int,
    ) -> None:
        """Existing managed rooms flip to encrypted on startup and hot reload when configured."""
        config = _bound_config(tmp_path, matrix_room_access={"encrypt_managed_rooms": encrypt_managed_rooms})
        mock_client = AsyncMock()
        mock_client.homeserver = "https://example.com"
        mock_client.rooms = {"!lobby:example.com": object()}
        mock_client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(
            room_alias="#lobby:example.com",
            room_id="!lobby:example.com",
            servers=["example.com"],
        )

        monkeypatch.setattr(matrix_state, "load_rooms", dict)
        monkeypatch.setattr(matrix_rooms, "_add_room", MagicMock())
        monkeypatch.setattr(matrix_rooms, "ensure_room_name", AsyncMock(return_value=True))
        monkeypatch.setattr(matrix_rooms, "ensure_room_has_topic", AsyncMock())
        monkeypatch.setattr(matrix_rooms, "ensure_thread_tags_power_level", AsyncMock(return_value=True))
        enable_encryption = AsyncMock(return_value=True)
        monkeypatch.setattr(matrix_rooms, "ensure_room_encryption_enabled", enable_encryption)

        room_id = await matrix_rooms._ensure_room_exists(
            client=mock_client,
            room_key="lobby",
            config=config,
            runtime_paths=runtime_paths_for(config),
            room_name="Lobby",
            power_users=[],
        )

        assert room_id == "!lobby:example.com"
        assert enable_encryption.await_count == expected_calls
        if expected_calls:
            enable_encryption.assert_awaited_once_with(mock_client, "!lobby:example.com")

    @pytest.mark.asyncio
    async def test_new_room_created_encrypted_when_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Managed room creation passes the configured encryption flag through."""
        config = _bound_config(tmp_path, matrix_room_access={"encrypt_managed_rooms": True})
        mock_client = AsyncMock()
        mock_client.homeserver = "https://example.com"
        mock_client.rooms = {}
        mock_client.room_resolve_alias.return_value = nio.RoomResolveAliasError(
            "not found",
            status_code="M_NOT_FOUND",
        )

        monkeypatch.setattr(matrix_state, "load_rooms", dict)
        monkeypatch.setattr(matrix_rooms, "_add_room", MagicMock())
        monkeypatch.setattr(matrix_rooms, "generate_room_topic_ai", AsyncMock(return_value="topic"))
        monkeypatch.setattr(matrix_rooms, "_set_room_avatar_if_available", AsyncMock())
        created = AsyncMock(return_value="!new:example.com")
        monkeypatch.setattr(matrix_rooms, "create_room", created)

        room_id = await matrix_rooms._ensure_room_exists(
            client=mock_client,
            room_key="vault",
            config=config,
            runtime_paths=runtime_paths_for(config),
            room_name="Vault",
            power_users=[],
        )

        assert room_id == "!new:example.com"
        assert created.await_args.kwargs["encrypted"] is True


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
        admin_gate = AsyncMock(return_value=None)

        with patch("mindroom.commands.encryption_commands.room_admin_power_user", new=admin_gate):
            response = await handle_encrypt_command(
                "confirm",
                client=client,
                room_id="!room:localhost",
                requester_user_id="@requester:localhost",
                sender_user_id="@sender:localhost",
            )

        assert "Room admin only" in response
        client.room_put_state.assert_not_awaited()
        # The gate must evaluate exactly the requester and sender, never the bot itself
        # (managed rooms grant the bot PL 100, which would make everyone an admin).
        admin_gate.assert_awaited_once_with(
            client,
            "!room:localhost",
            ("@requester:localhost", "@sender:localhost"),
        )

    @pytest.mark.asyncio
    async def test_confirm_enables_encryption_for_admin(self) -> None:
        """Admins can enable encryption with confirm."""
        client = AsyncMock(spec=nio.AsyncClient)
        client.rooms = {}
        client.room_get_state_event.return_value = _state_error("M_NOT_FOUND")
        client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$state", room_id="!room:localhost")
        admin_gate = AsyncMock(return_value="@requester:localhost")

        with patch("mindroom.commands.encryption_commands.room_admin_power_user", new=admin_gate):
            response = await handle_encrypt_command(
                "confirm",
                client=client,
                room_id="!room:localhost",
                requester_user_id="@requester:localhost",
                sender_user_id="@sender:localhost",
            )

        assert "now enabled" in response
        client.room_put_state.assert_awaited_once()
        admin_gate.assert_awaited_once_with(
            client,
            "!room:localhost",
            ("@requester:localhost", "@sender:localhost"),
        )


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
        client.cross_signing_identity = None

        response = await handle_e2ee_command(client=client, room_id="!room:localhost")

        assert "Room: encrypted" in response
        assert "@mindroom_assistant:localhost" in response
        assert "DEVICEID" in response
        assert "Cross-signing: not bootstrapped" in response


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
        fresh_client.olm = None

        with (
            patch("mindroom.matrix.users.restore_login", new=AsyncMock()) as mock_restore,
            patch("mindroom.matrix.users.login", new=AsyncMock(return_value=fresh_client)) as mock_login,
            patch("mindroom.matrix.users.ensure_agent_cross_signing", new=AsyncMock()) as mock_cross_signing,
        ):
            client = await login_agent_user("http://localhost:8008", agent_user, runtime_paths=runtime_paths)

        mock_restore.assert_not_awaited()
        mock_login.assert_awaited_once()
        assert client is fresh_client
        assert agent_user.device_id == "NEWDEVICE"
        # The fresh device must be re-signed with the persisted cross-signing keys.
        mock_cross_signing.assert_awaited_once_with(fresh_client, agent_user)

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
        restored_client.olm = None

        with (
            patch("mindroom.matrix.users.restore_login", new=AsyncMock(return_value=restored_client)) as mock_restore,
            patch("mindroom.matrix.users.login", new=AsyncMock()) as mock_login,
            patch("mindroom.matrix.users.ensure_agent_cross_signing", new=AsyncMock()) as mock_cross_signing,
        ):
            client = await login_agent_user("http://localhost:8008", agent_user, runtime_paths=runtime_paths)

        mock_restore.assert_awaited_once()
        mock_login.assert_not_awaited()
        assert client is restored_client
        # Every login path must bootstrap cross-signing for the session's device.
        mock_cross_signing.assert_awaited_once_with(restored_client, agent_user)

    def test_olm_store_exists_matches_real_nio_store_naming(self, tmp_path: Path) -> None:
        """The presence check must agree with the file nio actually creates."""
        runtime_paths = _runtime_paths(tmp_path)
        user_id = "@mindroom_assistant:localhost"
        store_dir = olm_store_dir(user_id, runtime_paths)
        store_dir.mkdir(parents=True)

        assert not olm_store_exists(user_id, "REALDEVICE", runtime_paths)
        SqliteStore(user_id, "REALDEVICE", str(store_dir))
        assert olm_store_exists(user_id, "REALDEVICE", runtime_paths)
