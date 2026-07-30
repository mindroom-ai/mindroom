"""Tests for undecryptable Megolm event handling."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import nio
import pytest
from nio.exceptions import LocalProtocolError

from mindroom.constants import resolve_runtime_paths
from mindroom.matrix import decrypt_failure
from mindroom.matrix.decrypt_failure import e2ee_stats, handle_decrypt_failure, raise_notice_floor

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


@pytest.fixture(autouse=True)
def _reset_notice_state() -> None:
    decrypt_failure._notice_ledgers.clear()
    decrypt_failure._notice_floors.clear()


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data")


def _megolm_event(
    session_id: str = "session123",
    *,
    event_id: str | None = None,
    server_timestamp: int = 1700000000000,
) -> nio.MegolmEvent:
    event = nio.MegolmEvent.from_dict(
        {
            "event_id": event_id or f"$undecryptable{uuid4().hex}:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": server_timestamp,
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


def _mock_client(
    outgoing_key_requests: dict | None = None,
    user_id: str = "@mindroom_assistant:localhost",
) -> AsyncMock:
    client = AsyncMock(spec=nio.AsyncClient)
    client.outgoing_key_requests = outgoing_key_requests or {}
    client.user_id = user_id
    return client


def _mock_room(users: list[str] | None = None) -> MagicMock:
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    room.users = dict.fromkeys(users or ["@user:localhost", "@mindroom_assistant:localhost"])
    return room


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
async def test_first_failing_bot_claims_notice_for_session(tmp_path: Path) -> None:
    """In multi-bot rooms only the first bot that fails on a session posts the notice."""
    runtime_paths = _runtime_paths(tmp_path)
    room = _mock_room(
        users=["@user:localhost", "@mindroom_assistant:localhost", "@mindroom_coder:localhost"],
    )
    notice = AsyncMock(return_value=True)

    with patch.object(decrypt_failure, "_send_decrypt_failure_notice", new=notice):
        await handle_decrypt_failure(
            _mock_client(user_id="@mindroom_assistant:localhost"),
            room,
            _megolm_event(),
            agent_name="assistant",
            runtime_paths=runtime_paths,
        )
        await handle_decrypt_failure(
            _mock_client(user_id="@mindroom_coder:localhost"),
            room,
            _megolm_event(),
            agent_name="coder",
            runtime_paths=runtime_paths,
        )

    assert notice.await_count == 1


@pytest.mark.asyncio
async def test_failed_send_releases_claim_for_retry(tmp_path: Path) -> None:
    """A cleanly failed notice send must not permanently suppress the session's notice."""
    runtime_paths = _runtime_paths(tmp_path)
    client = _mock_client()
    notice = AsyncMock(side_effect=[False, True])

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
            _megolm_event(),
            agent_name="assistant",
            runtime_paths=runtime_paths,
        )

    assert notice.await_count == 2  # retried once after the failure, then deduped


@pytest.mark.asyncio
async def test_notice_suppressed_for_events_below_room_floor(tmp_path: Path) -> None:
    """Events older than the room's notice floor (e.g. pre-join history) stay silent."""
    client = _mock_client()
    raise_notice_floor(client.user_id, "!room:localhost")
    notice = AsyncMock(return_value=True)

    with patch.object(decrypt_failure, "_send_decrypt_failure_notice", new=notice):
        await handle_decrypt_failure(
            client,
            _mock_room(),
            _megolm_event(server_timestamp=1700000000000),
            agent_name="assistant",
            runtime_paths=_runtime_paths(tmp_path),
        )

    notice.assert_not_awaited()
    client.request_room_key.assert_awaited_once()  # key request still goes out


@pytest.mark.asyncio
async def test_global_floor_suppresses_notices_in_every_room(tmp_path: Path) -> None:
    """A tokenless start suppresses notices for replayed history in all rooms."""
    client = _mock_client()
    raise_notice_floor(client.user_id)
    notice = AsyncMock(return_value=True)

    with patch.object(decrypt_failure, "_send_decrypt_failure_notice", new=notice):
        await handle_decrypt_failure(
            client,
            _mock_room(),
            _megolm_event(server_timestamp=1700000000000),
            agent_name="assistant",
            runtime_paths=_runtime_paths(tmp_path),
        )

    notice.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_newer_than_floor_still_notifies(tmp_path: Path) -> None:
    """Fresh messages arriving after a floor was raised must still notify."""
    client = _mock_client()
    raise_notice_floor(client.user_id, "!room:localhost")
    notice = AsyncMock(return_value=True)
    future_ts = int(time.time() * 1000) + 60_000

    with patch.object(decrypt_failure, "_send_decrypt_failure_notice", new=notice):
        await handle_decrypt_failure(
            client,
            _mock_room(),
            _megolm_event(server_timestamp=future_ts),
            agent_name="assistant",
            runtime_paths=_runtime_paths(tmp_path),
        )

    notice.assert_awaited_once()


@pytest.mark.asyncio
async def test_notice_payload_is_a_matrix_notice(tmp_path: Path) -> None:
    """The real delivery path sends an m.notice with the decrypt-failure body."""
    client = _mock_client()
    send = AsyncMock(return_value=MagicMock())

    with patch("mindroom.matrix.client_delivery.send_message_result", new=send):
        await handle_decrypt_failure(
            client,
            _mock_room(),
            _megolm_event(),
            agent_name="assistant",
            runtime_paths=_runtime_paths(tmp_path),
        )

    send.assert_awaited_once()
    args, kwargs = send.await_args
    assert args[0] is client
    assert args[1] == "!room:localhost"
    assert args[2]["msgtype"] == "m.notice"
    assert "couldn't decrypt" in args[2]["body"]
    assert kwargs["operation"] == "decrypt_failure_notice"


@pytest.mark.asyncio
async def test_stats_count_each_event_once_across_bots(tmp_path: Path) -> None:
    """Two bots failing on the same event must count it once, not twice."""
    runtime_paths = _runtime_paths(tmp_path)
    room = _mock_room()
    event = _megolm_event(event_id=f"$shared{uuid4().hex}:localhost")
    before = e2ee_stats().decrypt_failures
    before_room = e2ee_stats().decrypt_failures_by_room.get("!room:localhost", 0)

    with patch.object(decrypt_failure, "_send_decrypt_failure_notice", new=AsyncMock(return_value=True)):
        await handle_decrypt_failure(
            _mock_client(user_id="@mindroom_assistant:localhost"),
            room,
            event,
            agent_name="assistant",
            runtime_paths=runtime_paths,
        )
        await handle_decrypt_failure(
            _mock_client(user_id="@mindroom_coder:localhost"),
            room,
            event,
            agent_name="coder",
            runtime_paths=runtime_paths,
        )

    assert e2ee_stats().decrypt_failures == before + 1
    assert e2ee_stats().decrypt_failures_by_room["!room:localhost"] == before_room + 1
