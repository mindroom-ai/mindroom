"""Sending one live frame, and asking the homeserver whether it relays them."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest
import pytest_asyncio
from aiohttp import web

import mindroom.live_frames.transport as transport_module
from mindroom.live_frames.transport import LiveSendOutcome, homeserver_relays_live_frames, send_live_frame
from tests.conftest import make_matrix_client_mock

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

_ROOM_ID = "!room:localhost"
type _VersionsServer = Callable[[dict[str, object]], Awaitable[str]]
_FRAME = {"v": 1, "reply": "$reply", "sid": "1761858000123-0a1b2c3d", "seq": 0, "snapshot": {"md": "hi"}}
_MEGOLM = {
    "algorithm": "m.megolm.v1.aes-sha2",
    "ciphertext": "x",
    "session_id": "s",
    "sender_key": "k",
    "device_id": "D",
}


def _client(*, encrypted: bool, response: object) -> AsyncMock:
    client = make_matrix_client_mock(user_id="@mindroom_code:localhost")
    client.rooms = {_ROOM_ID: SimpleNamespace(encrypted=encrypted)}
    client.room_encrypt = AsyncMock(return_value=_MEGOLM)
    client.room_send_ephemeral = AsyncMock(return_value=response)
    return client


def _error(errcode: str) -> nio.RoomSendEphemeralError:
    return nio.RoomSendEphemeralError(message="refused", status_code=errcode, room_id=_ROOM_ID)


@pytest.mark.asyncio
async def test_a_plaintext_room_gets_the_frame_as_it_is() -> None:
    """The frame goes out unchanged, keyed by the transaction id that names its publisher and sequence number."""
    client = _client(encrypted=False, response=nio.RoomSendEphemeralResponse(_ROOM_ID))

    assert await send_live_frame(client, _ROOM_ID, _FRAME, transaction_id="sid.0") is LiveSendOutcome.SENT
    client.room_encrypt.assert_not_awaited()
    client.room_send_ephemeral.assert_awaited_once_with(_ROOM_ID, "io.mindroom.live", _FRAME, tx_id="sid.0")


@pytest.mark.asyncio
async def test_an_encrypted_room_gets_the_frame_encrypted_without_counting_toward_rotation() -> None:
    """Frames use the room's session, shared to unverified devices as replies are, and leave its message count alone."""
    client = _client(encrypted=True, response=nio.RoomSendEphemeralResponse(_ROOM_ID))

    assert await send_live_frame(client, _ROOM_ID, _FRAME, transaction_id="sid.0") is LiveSendOutcome.SENT
    client.room_encrypt.assert_awaited_once_with(
        _ROOM_ID,
        "io.mindroom.live",
        _FRAME,
        ignore_unverified_devices=True,
        count_toward_rotation=False,
    )
    client.room_send_ephemeral.assert_awaited_once_with(
        _ROOM_ID,
        "io.mindroom.live",
        {"v": 1, "encrypted": _MEGOLM},
        tx_id="sid.0",
    )


@pytest.mark.asyncio
async def test_a_frame_over_the_size_limit_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frame too large for one ephemeral event stops frames instead of being refused by the homeserver."""
    monkeypatch.setattr(transport_module, "_MAX_FRAME_BYTES", 40)
    client = _client(encrypted=False, response=nio.RoomSendEphemeralResponse(_ROOM_ID))

    assert await send_live_frame(client, _ROOM_ID, _FRAME, transaction_id="sid.0") is LiveSendOutcome.TOO_LARGE
    client.room_send_ephemeral.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("errcode", "outcome"),
    [
        ("M_UNRECOGNIZED", LiveSendOutcome.STOP),
        ("M_FORBIDDEN", LiveSendOutcome.STOP),
        ("M_TOO_LARGE", LiveSendOutcome.TOO_LARGE),
        ("M_UNKNOWN_TOKEN", LiveSendOutcome.RETRY),
    ],
)
async def test_refusals_stop_frames_and_other_errors_retry(errcode: str, outcome: LiveSendOutcome) -> None:
    """A homeserver without the route, a room that refuses the sender, or an oversized body ends frames for the reply."""
    client = _client(encrypted=False, response=_error(errcode))

    assert await send_live_frame(client, _ROOM_ID, _FRAME, transaction_id="sid.0") is outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "outcome"), [(403, LiveSendOutcome.STOP), (502, LiveSendOutcome.RETRY)])
async def test_a_refusal_without_a_matrix_error_goes_by_its_http_status(status: int, outcome: LiveSendOutcome) -> None:
    """A proxy's bare 4xx refuses later frames too, so frames stop; a bare 5xx is retried."""
    error = nio.RoomSendEphemeralError(message=f"HTTP {status}", room_id=_ROOM_ID)
    error.transport_response = SimpleNamespace(status=status)
    client = _client(encrypted=False, response=error)

    assert await send_live_frame(client, _ROOM_ID, _FRAME, transaction_id="sid.0") is outcome


@pytest.mark.asyncio
async def test_a_room_the_client_does_not_know_gets_no_frames() -> None:
    """Without the room in the client's cache, its encryption is unknown, so no frame is sent."""
    client = _client(encrypted=False, response=nio.RoomSendEphemeralResponse(_ROOM_ID))
    client.rooms = {}

    assert await send_live_frame(client, _ROOM_ID, _FRAME, transaction_id="sid.0") is LiveSendOutcome.STOP
    client.room_send_ephemeral.assert_not_awaited()


@pytest_asyncio.fixture
async def versions_server(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_VersionsServer]:
    """Serve `/_matrix/client/versions` with the given unstable features."""
    monkeypatch.setattr(transport_module, "_feature_checks", {})
    runners: list[web.AppRunner] = []

    async def start(features: dict[str, object]) -> str:
        async def versions(_request: web.Request) -> web.Response:
            return web.json_response({"versions": ["v1.11"], "unstable_features": features})

        app = web.Application()
        app.router.add_get("/_matrix/client/versions", versions)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        runners.append(runner)
        return f"http://127.0.0.1:{runner.addresses[0][1]}"

    yield start
    for runner in runners:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_frames_need_the_fork_opt_in_flag(versions_server: _VersionsServer) -> None:
    """MSC2477 alone does not promise opt-in delivery, so only the fork's flag turns frames on."""
    assert await homeserver_relays_live_frames(await versions_server({"org.matrix.msc2477": True})) is False
    relaying = await versions_server({"org.matrix.msc2477": True, "io.mindroom.ephemeral_opt_in": True})
    assert await homeserver_relays_live_frames(relaying) is True


@pytest.mark.asyncio
async def test_a_failed_check_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A check that fails, as through a proxy while the homeserver restarts, is not cached as "no frames"."""
    monkeypatch.setattr(transport_module, "_feature_checks", {})
    answers = [
        web.Response(status=502),
        web.json_response({"versions": [], "unstable_features": {"io.mindroom.ephemeral_opt_in": True}}),
    ]

    async def versions(_request: web.Request) -> web.Response:
        return answers.pop(0)

    app = web.Application()
    app.router.add_get("/_matrix/client/versions", versions)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        assert await homeserver_relays_live_frames(url) is False
        assert await homeserver_relays_live_frames(url) is True
    finally:
        await runner.cleanup()
