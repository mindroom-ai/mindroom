"""Tests for undecryptable Megolm event handling."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from nio.exceptions import LocalProtocolError

from mindroom.constants import resolve_runtime_paths
from mindroom.matrix import decrypt_failure
from mindroom.matrix.decrypt_failure import handle_decrypt_failure
from mindroom.matrix.state import MatrixAccount, MatrixState

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


@pytest.fixture(autouse=True)
def _reset_notice_ledger_cache() -> None:
    decrypt_failure._notice_ledgers.clear()


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data")


def _megolm_event(session_id: str = "session123") -> nio.MegolmEvent:
    event = nio.MegolmEvent.from_dict(
        {
            "event_id": "$undecryptable:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1700000000000,
            "type": "m.room.encrypted",
            "room_id": "!room:localhost",
            "content": {
                "algorithm": "m.megolm.v1.aes-sha2",
                "ciphertext": "cipher",
                "sender_key": "sender_key",
                "session_id": session_id,
                "device_id": "DEVICE1",
            },
        },
    )
    assert isinstance(event, nio.MegolmEvent)
    return event


def _mock_client(outgoing_key_requests: dict | None = None) -> AsyncMock:
    client = AsyncMock(spec=nio.AsyncClient)
    client.outgoing_key_requests = outgoing_key_requests or {}
    client.user_id = "@mindroom_assistant:localhost"
    return client


def _mock_room(users: list[str] | None = None) -> MagicMock:
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    room.users = dict.fromkeys(users or ["@user:localhost", "@mindroom_assistant:localhost"])
    return room


def _persist_accounts(runtime_paths: RuntimePaths, accounts: dict[str, MatrixAccount]) -> None:
    MatrixState(accounts=accounts).save(runtime_paths=runtime_paths)


@pytest.mark.asyncio
async def test_handle_decrypt_failure_requests_room_key(tmp_path: Path) -> None:
    """An undecryptable event should trigger one room-key request."""
    client = _mock_client()
    event = _megolm_event()

    with patch.object(decrypt_failure, "_send_decrypt_failure_notice", new=AsyncMock(return_value=True)):
        await handle_decrypt_failure(
            client,
            _mock_room(),
            event,
            agent_name="assistant",
            runtime_paths=_runtime_paths(tmp_path),
        )

    client.request_room_key.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_handle_decrypt_failure_skips_already_requested_session(tmp_path: Path) -> None:
    """A session with an outgoing key request should not be requested again."""
    client = _mock_client(outgoing_key_requests={"session123": MagicMock()})
    event = _megolm_event()

    with patch.object(decrypt_failure, "_send_decrypt_failure_notice", new=AsyncMock(return_value=True)):
        await handle_decrypt_failure(
            client,
            _mock_room(),
            event,
            agent_name="assistant",
            runtime_paths=_runtime_paths(tmp_path),
        )

    client.request_room_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_decrypt_failure_tolerates_concurrent_key_request(tmp_path: Path) -> None:
    """A racing duplicate key request should not raise out of the handler."""
    client = _mock_client()
    client.request_room_key.side_effect = LocalProtocolError("already requested")

    with patch.object(decrypt_failure, "_send_decrypt_failure_notice", new=AsyncMock(return_value=True)):
        await handle_decrypt_failure(
            client,
            _mock_room(),
            _megolm_event(),
            agent_name="assistant",
            runtime_paths=_runtime_paths(tmp_path),
        )

    client.request_room_key.assert_awaited_once()


@pytest.mark.asyncio
async def test_notice_sent_once_per_room_session(tmp_path: Path) -> None:
    """The visible notice must be posted at most once per (room, session)."""
    runtime_paths = _runtime_paths(tmp_path)
    client = _mock_client()
    notice = AsyncMock(return_value=True)

    with patch.object(decrypt_failure, "_send_decrypt_failure_notice", new=notice):
        await handle_decrypt_failure(
            client,
            _mock_room(),
            _megolm_event(),
            agent_name="assistant",
            runtime_paths=runtime_paths,
        )
        await handle_decrypt_failure(
            client,
            _mock_room(),
            _megolm_event(),
            agent_name="assistant",
            runtime_paths=runtime_paths,
        )
        await handle_decrypt_failure(
            client,
            _mock_room(),
            _megolm_event(session_id="other_session"),
            agent_name="assistant",
            runtime_paths=runtime_paths,
        )

    assert notice.await_count == 2  # one per distinct session


@pytest.mark.asyncio
async def test_notice_dedup_survives_ledger_cache_reset(tmp_path: Path) -> None:
    """The notice ledger persists on disk, so a fresh process does not re-notify."""
    runtime_paths = _runtime_paths(tmp_path)
    client = _mock_client()
    notice = AsyncMock(return_value=True)

    with patch.object(decrypt_failure, "_send_decrypt_failure_notice", new=notice):
        await handle_decrypt_failure(
            client,
            _mock_room(),
            _megolm_event(),
            agent_name="assistant",
            runtime_paths=runtime_paths,
        )
        decrypt_failure._notice_ledgers.clear()  # simulate restart
        await handle_decrypt_failure(
            client,
            _mock_room(),
            _megolm_event(),
            agent_name="assistant",
            runtime_paths=runtime_paths,
        )

    assert notice.await_count == 1


@pytest.mark.asyncio
async def test_non_router_stays_silent_when_router_is_in_room(tmp_path: Path) -> None:
    """When the router is present, only the router posts the notice."""
    runtime_paths = _runtime_paths(tmp_path)
    _persist_accounts(
        runtime_paths,
        {
            "agent_router": MatrixAccount(username="mindroom_router", password="x", domain="localhost"),  # noqa: S106
            "agent_assistant": MatrixAccount(username="mindroom_assistant", password="x", domain="localhost"),  # noqa: S106
        },
    )
    room = _mock_room(
        users=["@user:localhost", "@mindroom_router:localhost", "@mindroom_assistant:localhost"],
    )
    notice = AsyncMock(return_value=True)

    with patch.object(decrypt_failure, "_send_decrypt_failure_notice", new=notice):
        await handle_decrypt_failure(
            _mock_client(),
            room,
            _megolm_event(),
            agent_name="assistant",
            runtime_paths=runtime_paths,
        )

    notice.assert_not_awaited()

    with patch.object(decrypt_failure, "_send_decrypt_failure_notice", new=notice):
        router_client = _mock_client()
        router_client.user_id = "@mindroom_router:localhost"
        await handle_decrypt_failure(
            router_client,
            room,
            _megolm_event(session_id="router_session"),
            agent_name="router",
            runtime_paths=runtime_paths,
        )

    notice.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_notice_in_multi_bot_room_without_router(tmp_path: Path) -> None:
    """Multiple non-router bots in a room must not race to post notices."""
    runtime_paths = _runtime_paths(tmp_path)
    _persist_accounts(
        runtime_paths,
        {
            "agent_assistant": MatrixAccount(username="mindroom_assistant", password="x", domain="localhost"),  # noqa: S106
            "agent_coder": MatrixAccount(username="mindroom_coder", password="x", domain="localhost"),  # noqa: S106
        },
    )
    room = _mock_room(
        users=["@user:localhost", "@mindroom_assistant:localhost", "@mindroom_coder:localhost"],
    )
    notice = AsyncMock(return_value=True)

    with patch.object(decrypt_failure, "_send_decrypt_failure_notice", new=notice):
        await handle_decrypt_failure(
            _mock_client(),
            room,
            _megolm_event(),
            agent_name="assistant",
            runtime_paths=runtime_paths,
        )

    notice.assert_not_awaited()
