"""Sending one live frame, and asking the homeserver whether it relays them."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest
import pytest_asyncio
from aiohttp import web
from nio.api import Api
from nio.crypto.sessions import OutboundGroupSession

import mindroom.live_frames.transport as transport_module
from mindroom.live_frames.transport import LiveSendOutcome, frame_fits, homeserver_relays_live_frames, send_live_frame
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
async def test_send_live_frame_encrypts_with_the_given_type() -> None:
    """A frame of another type is encrypted and sent as that type, so the event a client decrypts names it."""
    client = _client(encrypted=True, response=nio.RoomSendEphemeralResponse(_ROOM_ID))

    outcome = await send_live_frame(
        client,
        _ROOM_ID,
        _FRAME,
        transaction_id="subagents.sid.0",
        event_type="io.mindroom.subagents",
    )

    assert outcome is LiveSendOutcome.SENT
    client.room_encrypt.assert_awaited_once_with(
        _ROOM_ID,
        "io.mindroom.subagents",
        _FRAME,
        ignore_unverified_devices=True,
        count_toward_rotation=False,
    )
    client.room_send_ephemeral.assert_awaited_once_with(
        _ROOM_ID,
        "io.mindroom.subagents",
        {"v": 1, "encrypted": _MEGOLM},
        tx_id="subagents.sid.0",
    )


def _megolm_content(room_id: str, event_type: str, frame: dict[str, object]) -> dict[str, object]:
    """Return the content nio sends for the frame in an encrypted room, encrypted with a real Megolm session."""
    session = OutboundGroupSession()
    session.mark_as_shared()
    ciphertext = session.encrypt(Api.to_json({"content": frame, "type": event_type, "room_id": room_id}), count=False)
    encrypted = {**_MEGOLM, "ciphertext": ciphertext, "session_id": session.id, "sender_key": "k" * 43}
    return {"v": 1, "encrypted": encrypted}


@pytest.mark.parametrize(
    "text",
    ["a" * 10_000, "ж" * 5_000, "日本" * 2_500, "😀" * 2_000, ""],
    ids=["ascii", "cyrillic", "cjk", "emoji", "empty"],
)
def test_the_encrypted_size_bounds_what_megolm_sends(text: str) -> None:
    """The size estimate is never below the real encrypted content and stays within a few hundred bytes of it."""
    frame = {**_FRAME, "snapshot": {"md": text}}
    sent = len(json.dumps(_megolm_content(_ROOM_ID, "io.mindroom.subagents", frame), separators=(",", ":")))
    estimate = transport_module._encrypted_size(_ROOM_ID, frame, event_type="io.mindroom.subagents")
    assert sent <= estimate <= sent + 300


def test_a_frame_fits_by_the_size_it_goes_out_at() -> None:
    """Characters outside ASCII cost far more once encrypted, so a frame can fit a plaintext room but not an encrypted one."""
    frame = {**_FRAME, "snapshot": {"md": "ж" * 8_000}}
    plaintext = _client(encrypted=False, response=None)
    encrypted = _client(encrypted=True, response=None)
    assert frame_fits(plaintext, _ROOM_ID, frame, event_type="io.mindroom.live")
    assert not frame_fits(encrypted, _ROOM_ID, frame, event_type="io.mindroom.live")


@pytest.mark.asyncio
async def test_a_plaintext_frame_is_measured_as_nio_sends_it() -> None:
    """A frame goes out as JSON with ASCII escapes, so one of 22,000 UTF-8 bytes is 66,000 on the wire and not sent."""
    frame = {**_FRAME, "snapshot": {"md": "ж" * 11_000}}
    client = _client(encrypted=False, response=nio.RoomSendEphemeralResponse(_ROOM_ID))

    assert not frame_fits(client, _ROOM_ID, frame, event_type="io.mindroom.live")
    assert await send_live_frame(client, _ROOM_ID, frame, transaction_id="sid.0") is LiveSendOutcome.STOP
    client.room_send_ephemeral.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_send_that_hangs_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A send still waiting at the timeout is given up, and the next frame is a snapshot."""
    monkeypatch.setattr(transport_module, "_SEND_TIMEOUT_SECONDS", 0.01)
    client = _client(encrypted=False, response=None)

    async def hang(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(1)

    client.room_send_ephemeral = AsyncMock(side_effect=hang)

    assert await send_live_frame(client, _ROOM_ID, _FRAME, transaction_id="sid.0") is LiveSendOutcome.RETRY


@pytest.mark.asyncio
async def test_a_send_that_raises_stops_frames() -> None:
    """A send that raises, as when the room's group session cannot be shared, ends frames instead of failing the caller."""
    client = _client(encrypted=True, response=None)
    client.room_encrypt = AsyncMock(side_effect=nio.exceptions.GroupEncryptionError("not shared"))

    assert await send_live_frame(client, _ROOM_ID, _FRAME, transaction_id="sid.0") is LiveSendOutcome.STOP


@pytest.mark.asyncio
async def test_a_frame_over_the_size_limit_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frame too large for one ephemeral event stops frames instead of being refused by the homeserver."""
    monkeypatch.setattr(transport_module, "_MAX_FRAME_BYTES", 40)
    client = _client(encrypted=False, response=nio.RoomSendEphemeralResponse(_ROOM_ID))

    assert await send_live_frame(client, _ROOM_ID, _FRAME, transaction_id="sid.0") is LiveSendOutcome.STOP
    client.room_send_ephemeral.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("errcode", "outcome"),
    [
        ("M_UNRECOGNIZED", LiveSendOutcome.STOP),
        ("M_FORBIDDEN", LiveSendOutcome.STOP),
        ("M_TOO_LARGE", LiveSendOutcome.STOP),
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
