"""Authenticated discovery requests use live scope, private replies, and bounds."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest
from nio.crypto import OlmDevice
from nio.crypto.device import TrustState
from PIL import Image

from mindroom.config.models import ModelConfig
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from mindroom.model_catalog_receiver import register_model_catalog_receiver
from mindroom.room_model_overrides import set_room_model_override
from mindroom.thread_models import set_thread_model_override
from tests.conftest import runtime_paths_for
from tests.test_model_selection_scope import ROOM, USER, joined_response, picker_setup

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


def request(**changes: object) -> AuthenticatedToDeviceEvent:
    """Build the authenticated transport event with no claimed device fields."""
    content = {"version": 1, "request_id": "request-1", "room_id": ROOM, "thread_id": "$root"}
    content.update(changes)
    return AuthenticatedToDeviceEvent(
        source={"content": content},
        sender=USER,
        type="io.mindroom.models.request",
        authenticated_device_id="REQUESTER",
    )


def receiver_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Retain real scope validation and catalog, replacing only outbound transport."""
    client, config, paths, index, router, agent = picker_setup(tmp_path)
    device = OlmDevice(USER, "REQUESTER", {"ed25519": "fingerprint", "curve25519": "curve"})
    client.olm = SimpleNamespace(device_store={USER: {"REQUESTER": device}})
    sent = AsyncMock()
    monkeypatch.setattr("mindroom.model_catalog_receiver.send_encrypted_to_device", sent)
    current = [config]
    register_model_catalog_receiver(
        client=client,
        agent_name="router",
        runtime_paths=paths,
        config_getter=lambda: current[0],
        membership_index=index,
        callback_wrapper=lambda callback: callback,
    )
    callback = client.add_to_device_callback.call_args.args[0]
    return callback, client, current, sent, router, agent


@pytest.mark.asyncio
async def test_authenticated_catalog_reply_and_live_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery returns exact allowlisted scope and current inherited model."""
    callback, client, current, sent, _, agent = receiver_setup(tmp_path, monkeypatch)
    await callback(request())
    reply = sent.call_args.kwargs
    assert reply["event_type"] == "io.mindroom.models.response"
    assert reply["verify_device"] is False
    assert sent.call_args.args[1].device_id == "REQUESTER"
    content = reply["content"]
    assert content["agent_user_ids"] == [agent]
    assert content["capabilities"] == ["model_selection"]
    assert content["selection"] == {"override": None, "inherited": [{"entity": "helper", "model": "default"}]}
    assert content["request_id"] == "request-1"
    old_revision = content["catalog_revision"]
    current[0] = current[0].model_copy(deep=True)
    current[0].models["default"].display_name = "Updated label"
    await callback(request(request_id="request-2"))
    assert sent.call_args.kwargs["content"]["models"][0]["display_name"] == "Updated label"
    assert sent.call_args.kwargs["content"]["catalog_revision"] != old_revision
    client.verify_device.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"version": True},
        {"thread_id": None},
        {"request_id": "x" * 129},
        {"room_id": ""},
        {"sender": USER},
        {"device_id": "REQUESTER"},
    ],
)
@pytest.mark.asyncio
async def test_malformed_requests_receive_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict,
) -> None:
    """Unknown fields, bounds and optional-thread typing fail closed."""
    callback, client, _, sent, _, _ = receiver_setup(tmp_path, monkeypatch)
    await callback(request(**changes))
    sent.assert_not_awaited()
    client.joined_members.assert_not_awaited()


@pytest.mark.parametrize(
    "denial",
    ["plaintext", "blocked", "unknown_device", "requester_left", "agent_left", "wrong_thread", "wrong_room"],
)
@pytest.mark.asyncio
async def test_unauthorized_requests_receive_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    denial: str,
) -> None:
    """Authenticated device and current real room/root access are both required."""
    callback, client, _, sent, router, agent = receiver_setup(tmp_path, monkeypatch)
    event = request()
    if denial == "plaintext":
        event = nio.UnknownToDeviceEvent(source=event.source, sender=USER, type=event.type)
    elif denial == "blocked":
        client.olm.device_store[USER]["REQUESTER"].trust_state = TrustState.blacklisted
    elif denial == "unknown_device":
        event.authenticated_device_id = "OTHER"
    elif denial == "requester_left":
        client.joined_members.return_value = joined_response(router, agent)
    elif denial == "agent_left":
        client.joined_members.return_value = joined_response(USER, router)
    elif denial == "wrong_thread":
        event = request(thread_id="$other")
    else:
        event = request(room_id="!other:localhost")
    await callback(event)
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_departure_during_icon_upload_prevents_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A previously authorized requester cannot receive after awaited catalog work."""
    callback, client, current, sent, router, agent = receiver_setup(tmp_path, monkeypatch)
    Image.new("RGB", (1, 1)).save(tmp_path / "icon.png")
    current[0].models["default"].icon = "icon.png"

    async def upload(*_args: object, **_kwargs: object) -> tuple:
        client.joined_members.return_value = joined_response(router, agent)
        return nio.UploadResponse("mxc://localhost/image"), None

    client.upload.side_effect = upload
    await callback(request())
    sent.assert_not_awaited()


@pytest.mark.parametrize("large", ["count", "payload"])
@pytest.mark.asyncio
async def test_limits_do_not_return_partial_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    large: str,
) -> None:
    """Over-limit catalogs become unavailable, never silently truncated."""
    callback, _, current, sent, _, _ = receiver_setup(tmp_path, monkeypatch)
    current[0].models = {
        str(i): ModelConfig(provider="openai", id="x" * (300 if large == "payload" else 1))
        for i in range(256 if large == "payload" else 257)
    }
    await callback(request())
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicates_and_capacity_are_bounded_before_task_queue(
    tmp_path: Path,
) -> None:
    """Flooded callbacks cannot enqueue unbounded background scope/icon work."""
    client, config, paths, index, _, _ = picker_setup(tmp_path)
    device = OlmDevice(USER, "REQUESTER", {"ed25519": "fingerprint", "curve25519": "curve"})
    client.olm = SimpleNamespace(device_store={USER: {"REQUESTER": device}})
    queued = []

    def wrapper(callback: object) -> object:
        async def enqueue(event: object) -> None:
            queued.append((callback, event))

        return enqueue

    register_model_catalog_receiver(
        client=client,
        agent_name="router",
        runtime_paths=paths,
        config_getter=lambda: config,
        membership_index=index,
        callback_wrapper=wrapper,
    )
    callback = client.add_to_device_callback.call_args.args[0]
    await asyncio.gather(*(callback(request()) for _ in range(50)))
    assert len(queued) == 1
    await asyncio.gather(*(callback(request(request_id=str(i))) for i in range(50)))
    assert len(queued) <= 8


@pytest.mark.asyncio
async def test_non_router_does_not_register(tmp_path: Path) -> None:
    """Only the runtime router advertises discovery."""
    client, config, paths, index, _, _ = picker_setup(tmp_path)
    register_model_catalog_receiver(
        client=client,
        agent_name="helper",
        runtime_paths=paths,
        config_getter=lambda: config,
        membership_index=index,
        callback_wrapper=lambda callback: callback,
    )
    client.add_to_device_callback.assert_not_called()


@pytest.mark.asyncio
async def test_room_inheritance_excludes_thread_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reset explanation retains room model while actual override stays separate."""
    callback, _, current, sent, _, _ = receiver_setup(tmp_path, monkeypatch)
    config = current[0]
    paths = runtime_paths_for(config)
    config.models["room"] = ModelConfig(provider="openai", id="room-model")
    config.models["thread"] = ModelConfig(provider="openai", id="thread-model")
    set_room_model_override(paths, room_id=ROOM, model_name="room", set_by=USER)
    set_thread_model_override(paths, room_id=ROOM, thread_id="$root", model_name="thread", set_by=USER)
    await callback(request())
    assert sent.call_args.kwargs["content"]["selection"] == {
        "override": "thread",
        "inherited": [{"entity": "helper", "model": "room"}],
    }
    del config.models["thread"]
    await callback(request(request_id="next"))
    assert sent.call_args.kwargs["content"]["selection"]["override"] is None
    event = request(request_id="room-only")
    del event.source["content"]["thread_id"]
    await callback(event)
    assert "thread_id" not in sent.call_args.kwargs["content"]
    assert sent.call_args.kwargs["content"]["selection"]["override"] is None


@pytest.mark.asyncio
async def test_deadline_cancels_work_and_releases_capacity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Timed-out requests release reservations and never send late responses."""
    monkeypatch.setattr("mindroom.model_catalog_receiver._DEADLINE_SECONDS", 0.02)
    callback, client, _, sent, _, _ = receiver_setup(tmp_path, monkeypatch)
    cancelled = asyncio.Event()

    async def stalled(*_args: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    original = client.joined_members.return_value
    client.joined_members.side_effect = stalled
    await callback(request())
    assert cancelled.is_set()
    sent.assert_not_awaited()
    client.joined_members.side_effect = None
    client.joined_members.return_value = original
    await callback(request())
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_rate_limit_survives_completed_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finishing each request cannot bypass the per-device request window."""
    callback, _, _, sent, _, _ = receiver_setup(tmp_path, monkeypatch)
    for i in range(20):
        await callback(request(request_id=str(i)))
    assert sent.await_count == 8


@pytest.mark.asyncio
async def test_queued_request_cannot_repin_replaced_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Device replacement after admission cannot redirect the authenticated reply."""
    client, config, paths, index, _, _ = picker_setup(tmp_path)
    device = OlmDevice(USER, "REQUESTER", {"ed25519": "original", "curve25519": "curve"})
    client.olm = SimpleNamespace(device_store={USER: {"REQUESTER": device}})
    sent = AsyncMock()
    monkeypatch.setattr("mindroom.model_catalog_receiver.send_encrypted_to_device", sent)
    pending = []

    def wrapper(callback: object) -> object:
        async def enqueue(event: object) -> None:
            pending.append((callback, event))

        return enqueue

    register_model_catalog_receiver(
        client=client,
        agent_name="router",
        runtime_paths=paths,
        config_getter=lambda: config,
        membership_index=index,
        callback_wrapper=wrapper,
    )
    await client.add_to_device_callback.call_args.args[0](request())
    client.olm.device_store[USER]["REQUESTER"] = OlmDevice(
        USER,
        "REQUESTER",
        {"ed25519": "replacement", "curve25519": "new-curve"},
    )
    callback, event = pending[0]
    await callback(event)
    sent.assert_not_awaited()


@pytest.mark.parametrize("change", ["blocked", "left", "config"])
@pytest.mark.asyncio
async def test_final_transport_guard_rechecks_after_session_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Session establishment cannot leave stale access or configuration authorized."""
    callback, client, current, sent, router, agent = receiver_setup(tmp_path, monkeypatch)
    guards = []

    async def session_work(*_args: object, **kwargs: object) -> None:
        if change == "blocked":
            client.olm.device_store[USER]["REQUESTER"].trust_state = TrustState.blacklisted
        elif change == "left":
            client.joined_members.return_value = joined_response(router, agent)
        else:
            current[0] = current[0].model_copy(deep=True)
        guards.append(await kwargs["before_send"]())

    sent.side_effect = session_work
    await callback(request())
    assert guards == [False]


@pytest.mark.asyncio
async def test_config_reload_during_final_scope_await_prevents_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last transport guard must reject config replaced inside its awaited scope."""
    callback, client, current, sent, _, _ = receiver_setup(tmp_path, monkeypatch)
    original_config = current[0]
    membership = client.joined_members.return_value
    in_transport = False
    delivered = []

    async def joined_members(*_args: object) -> nio.JoinedMembersResponse:
        if in_transport and current[0] is original_config:
            await asyncio.sleep(0)
            current[0] = original_config.model_copy(deep=True)
        return membership

    async def transport(*_args: object, **kwargs: object) -> None:
        nonlocal in_transport
        in_transport = True
        if await kwargs["before_send"]():
            delivered.append(kwargs["content"])

    client.joined_members.side_effect = joined_members
    sent.side_effect = transport
    await callback(request())
    assert in_transport
    assert current[0] is not original_config
    assert delivered == []
