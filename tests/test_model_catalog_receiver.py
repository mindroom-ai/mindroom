"""Authenticated discovery requests use live scope, private replies, and bounds."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import MethodType, SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from nio.crypto import OlmDevice
from nio.crypto.device import TrustState
from PIL import Image
from structlog.testing import capture_logs

from mindroom.config.models import ModelConfig, ReasoningConfig
from mindroom.model_catalog_receiver import _MAX_USER_REQUESTS, _Receiver, register_model_catalog_receiver
from mindroom.room_model_overrides import set_room_model_override
from mindroom.thread_models import set_thread_model_override
from tests.conftest import runtime_paths_for
from tests.test_model_selection_scope import ROOM, USER, joined_response, picker_setup
from tests.test_olm_to_device import olm_transport

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


def request(**changes: object) -> nio.AuthenticatedToDeviceEvent:
    """Build the authenticated transport event with no claimed device fields."""
    content = {"version": 1, "request_id": "request-1", "room_id": ROOM, "thread_id": "$root"}
    content.update(changes)
    return nio.AuthenticatedToDeviceEvent(
        source={"content": content},
        sender=USER,
        type="io.mindroom.models.request",
        authenticated_sender=nio.AuthenticatedDevice(USER, "REQUESTER", "curve", "fingerprint"),
    )


def device_request(user_id: str, device_id: str, request_id: str) -> nio.AuthenticatedToDeviceEvent:
    """Build one request from a device whose keys are its device ID."""
    event = request(request_id=request_id)
    event.sender = user_id
    event.authenticated_sender = nio.AuthenticatedDevice(user_id, device_id, device_id, device_id)
    return event


def dropped(logs: list[dict]) -> list[str]:
    """Return the reason of each logged drop, in order."""
    return [entry["reason"] for entry in logs if entry["event"] == "model_catalog_dropped"]


def known_devices(devices: dict[str, list[str]]) -> SimpleNamespace:
    """Register each user's devices with keys equal to their device IDs."""
    return SimpleNamespace(
        device_store={
            user_id: {name: OlmDevice(user_id, name, {"ed25519": name, "curve25519": name}) for name in names}
            for user_id, names in devices.items()
        },
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
    with capture_logs() as logs:
        await callback(request())
    [answered] = logs
    assert answered["event"] == "model_catalog_answered"
    assert answered["log_level"] == "info"
    assert answered["model_count"] == 1
    assert answered["elapsed_ms"] >= 0
    assert {key: answered[key] for key in ("sender", "sender_device_id", "request_id", "room_id", "thread_id")} == {
        "sender": USER,
        "sender_device_id": "REQUESTER",
        "request_id": "request-1",
        "room_id": ROOM,
        "thread_id": "$root",
    }
    reply = sent.call_args.kwargs
    assert reply["event_type"] == "io.mindroom.models.response"
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
    with capture_logs() as logs:
        await callback(request(**changes))
    sent.assert_not_awaited()
    client.joined_members.assert_not_awaited()
    assert dropped(logs) == ["unparseable"]
    assert "request_id" not in logs[0]


@pytest.mark.asyncio
async def test_other_to_device_types_are_not_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only model requests count as dropped; the router receives other to-device traffic too."""
    callback, _, _, sent, _, _ = receiver_setup(tmp_path, monkeypatch)
    event = request()
    event.type = "m.room_key_request"
    with capture_logs() as logs:
        await callback(event)
    sent.assert_not_awaited()
    assert logs == []


@pytest.mark.parametrize(
    ("denial", "reason"),
    [
        ("plaintext", None),
        ("blocked", "unknown_sender_device"),
        ("deleted", "unknown_sender_device"),
        ("historical_fingerprint", "unknown_sender_device"),
        ("historical_curve", "unknown_sender_device"),
        ("unknown_device", "unknown_sender_device"),
        ("requester_left", "not_joined"),
        ("agent_left", "no_addressable_agent"),
        ("wrong_thread", "thread_root_unreadable"),
        ("wrong_room", "room_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_unauthorized_requests_receive_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    denial: str,
    reason: str | None,
) -> None:
    """Authenticated device and current real room/root access are both required."""
    callback, client, _, sent, router, agent = receiver_setup(tmp_path, monkeypatch)
    event = request()
    if denial == "plaintext":
        event = nio.UnknownToDeviceEvent(source=event.source, sender=USER, type=event.type)
    elif denial == "blocked":
        client.olm.device_store[USER]["REQUESTER"].trust_state = TrustState.blacklisted
    elif denial == "deleted":
        client.olm.device_store[USER]["REQUESTER"].deleted = True
    elif denial == "historical_fingerprint":
        event.authenticated_sender = replace(event.authenticated_sender, ed25519="previous")
    elif denial == "historical_curve":
        event.authenticated_sender = replace(event.authenticated_sender, curve25519="previous")
    elif denial == "unknown_device":
        event.authenticated_sender = replace(event.authenticated_sender, device_id="OTHER")
    elif denial == "requester_left":
        client.joined_members.return_value = joined_response(router, agent)
    elif denial == "agent_left":
        client.joined_members.return_value = joined_response(USER, router)
    elif denial == "wrong_thread":
        event = request(thread_id="$other")
    else:
        event = request(room_id="!other:localhost")
    with capture_logs() as logs:
        await callback(event)
    sent.assert_not_awaited()
    assert dropped(logs) == ([] if reason is None else [reason])


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
    with capture_logs() as logs:
        await callback(request())
    sent.assert_not_awaited()
    assert dropped(logs) == ["not_joined"]


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
    with capture_logs() as logs:
        await callback(request())
    sent.assert_not_awaited()
    assert dropped(logs) == ["response_too_large" if large == "payload" else "too_many_models"]


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
    with capture_logs() as logs:
        await asyncio.gather(*(callback(request()) for _ in range(50)))
    assert len(queued) == 1
    assert dropped(logs) == ["duplicate_in_flight"] * 49
    with capture_logs() as logs:
        await asyncio.gather(*(callback(request(request_id=str(i))) for i in range(50)))
    assert len(queued) <= 8
    assert dropped(logs) == ["rate_limited_user_inflight"] * (50 - len(queued) + 1)


@pytest.mark.asyncio
async def test_shared_in_flight_pool_drop_is_logged(tmp_path: Path) -> None:
    """A request beyond the shared in-flight pool logs the global limit as its reason."""
    client, config, paths, index, _, _ = picker_setup(tmp_path)
    users = [f"@user{i}:localhost" for i in range(9)]
    client.olm = known_devices({user: ["DEVICE"] for user in users})

    def wrapper(_callback: object) -> object:
        async def enqueue(_event: object) -> None:
            return None

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
    with capture_logs() as logs:
        for user in users:
            await callback(device_request(user, "DEVICE", "same-id"))
    assert dropped(logs) == ["rate_limited_global"]
    assert logs[0]["sender"] == users[-1]


@pytest.mark.asyncio
async def test_one_account_with_many_devices_leaves_capacity_for_other_users(tmp_path: Path) -> None:
    """Logging one account into more devices does not let it hold every shared slot."""
    client, config, paths, index, _, _ = picker_setup(tmp_path)
    other = "@other:localhost"
    devices = [f"DEVICE{i}" for i in range(64)]
    client.olm = known_devices({USER: devices, other: ["OTHER"]})
    queued = []

    def wrapper(_callback: object) -> object:
        async def enqueue(event: object) -> None:
            queued.append(event)

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

    for name in devices:
        await callback(device_request(USER, name, name))
    await callback(device_request(other, "OTHER", "other"))

    assert [event.sender for event in queued].count(USER) < 8
    assert [event.sender for event in queued][-1] == other


@pytest.mark.asyncio
async def test_completed_requests_from_many_devices_share_one_rate_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finishing each request on a fresh device cannot bypass the per-user window or take more rate-table entries."""
    client, config, paths, index, _, _ = picker_setup(tmp_path)
    other = "@other:localhost"
    devices = [f"DEVICE{i}" for i in range(20)]
    client.olm = known_devices({USER: devices, other: ["OTHER"]})
    monkeypatch.setattr("mindroom.model_catalog_receiver.send_encrypted_to_device", AsyncMock())
    receivers: list[_Receiver] = []
    admitted: list[str] = []

    def wrapper(callback: object) -> object:
        assert isinstance(callback, MethodType)
        assert isinstance(callback.__self__, _Receiver)
        receivers.append(callback.__self__)

        async def run(event: nio.AuthenticatedToDeviceEvent) -> None:
            admitted.append(event.sender)
            await callback(event)

        return run

    register_model_catalog_receiver(
        client=client,
        agent_name="router",
        runtime_paths=paths,
        config_getter=lambda: config,
        membership_index=index,
        callback_wrapper=wrapper,
    )
    callback = client.add_to_device_callback.call_args.args[0]

    for name in devices:
        await callback(device_request(USER, name, name))

    assert admitted == [USER] * _MAX_USER_REQUESTS
    assert not receivers[0].active
    assert list(receivers[0].rates) == [USER]
    await callback(device_request(other, "OTHER", "other"))
    assert admitted[-1] == other


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
    set_room_model_override(paths, room_id=ROOM, model_name="room")
    set_thread_model_override(
        paths,
        thread_id="$root",
        model_name="thread",
        entity_names=("helper",),
        config=config,
    )
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
async def test_selection_reports_model_keys_and_their_reasoning_levels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An override or inherited model at a level reports its catalog key and the level beside it."""
    callback, _, current, sent, _, _ = receiver_setup(tmp_path, monkeypatch)
    config = current[0]
    paths = runtime_paths_for(config)
    reasoning = ReasoningConfig(levels=["low", "high"], default="low")
    config.models["room"] = ModelConfig(provider="openai", id="room-model", reasoning=reasoning)
    config.models["thread"] = ModelConfig(provider="openai", id="thread-model", reasoning=reasoning)
    set_room_model_override(paths, room_id=ROOM, model_name="room@high")
    set_thread_model_override(
        paths,
        thread_id="$root",
        model_name="thread@high",
        entity_names=("helper",),
        config=config,
    )

    await callback(request())

    assert sent.call_args.kwargs["content"]["selection"] == {
        "override": "thread",
        "reasoning": "high",
        "inherited": [{"entity": "helper", "model": "room", "reasoning": "high"}],
    }


@pytest.mark.asyncio
async def test_selection_reports_only_an_override_governing_every_picker_entity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An override that another setter applied outside the picker's entities is not this picker's selection."""
    callback, _, current, sent, _, _ = receiver_setup(tmp_path, monkeypatch)
    config = current[0]
    paths = runtime_paths_for(config)
    config.models["thread"] = ModelConfig(provider="openai", id="thread-model")
    set_thread_model_override(
        paths,
        thread_id="$root",
        model_name="thread",
        entity_names=("router", "restricted"),
        config=config,
    )

    await callback(request())

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
    with capture_logs() as logs:
        await callback(request())
    assert cancelled.is_set()
    sent.assert_not_awaited()
    assert dropped(logs) == ["timeout"]
    assert logs[0]["elapsed_ms"] >= 0
    client.joined_members.side_effect = None
    client.joined_members.return_value = original
    await callback(request())
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_request_logs_once_and_releases_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task cancelled mid-request, as on shutdown, logs one drop, re-raises, and frees its slot."""
    callback, client, _, sent, _, _ = receiver_setup(tmp_path, monkeypatch)
    started = asyncio.Event()

    async def stalled(*_args: object) -> None:
        started.set()
        await asyncio.Event().wait()

    original = client.joined_members.return_value
    client.joined_members.side_effect = stalled
    with capture_logs() as logs:
        task = asyncio.create_task(callback(request()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    sent.assert_not_awaited()
    assert dropped(logs) == ["cancelled"]
    assert len(logs) == 1
    assert logs[0]["request_id"] == "request-1"
    assert logs[0]["elapsed_ms"] >= 0
    client.joined_members.side_effect = None
    client.joined_members.return_value = original
    await callback(request())
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_rate_limit_survives_completed_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finishing each request cannot bypass the per-user request window."""
    callback, _, _, sent, _, _ = receiver_setup(tmp_path, monkeypatch)
    with capture_logs() as logs:
        for i in range(20):
            await callback(request(request_id=str(i)))
    assert sent.await_count == 8
    assert dropped(logs) == ["rate_limited_user_window"] * 12
    assert [entry["event"] for entry in logs].count("model_catalog_answered") == 8


@pytest.mark.asyncio
async def test_queued_request_cannot_repin_replaced_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Device replacement after admission cannot redirect the authenticated reply."""
    client, config, paths, index, _, _ = picker_setup(tmp_path)
    device = OlmDevice(USER, "REQUESTER", {"ed25519": "fingerprint", "curve25519": "curve"})
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
    with capture_logs() as logs:
        await callback(event)
    sent.assert_not_awaited()
    assert dropped(logs) == ["sender_device_changed"]


@pytest.mark.parametrize("change", ["blocked", "left", "config"])
@pytest.mark.asyncio
async def test_final_authorization_rechecks_after_selection_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Awaited selection preparation cannot authorize stale access at NIO handoff."""
    callback, client, current, sent, router, agent = receiver_setup(tmp_path, monkeypatch)
    to_thread = asyncio.to_thread
    prepared = False

    async def selection_work(*args: object, **kwargs: object) -> object:
        nonlocal prepared
        result = await to_thread(*args, **kwargs)
        prepared = True
        if change == "blocked":
            client.olm.device_store[USER]["REQUESTER"].trust_state = TrustState.blacklisted
        elif change == "left":
            client.joined_members.return_value = joined_response(router, agent)
        else:
            current[0] = current[0].model_copy(deep=True)
        return result

    monkeypatch.setattr(asyncio, "to_thread", selection_work)
    with capture_logs() as logs:
        await callback(request())
    assert prepared
    sent.assert_not_awaited()
    assert dropped(logs) == [
        {"blocked": "sender_device_changed", "left": "not_joined", "config": "config_changed"}[change],
    ]


@pytest.mark.asyncio
async def test_config_reload_during_final_scope_await_prevents_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final application scope validation must reject config replaced while awaiting membership."""
    callback, client, current, sent, _, _ = receiver_setup(tmp_path, monkeypatch)
    original_config = current[0]
    membership = client.joined_members.return_value
    membership_reads = 0

    async def joined_members(*_args: object) -> nio.JoinedMembersResponse:
        nonlocal membership_reads
        membership_reads += 1
        # Each scope proof reads membership before and after the root event.
        if membership_reads == 5:
            await asyncio.sleep(0)
            current[0] = original_config.model_copy(deep=True)
        return membership

    client.joined_members.side_effect = joined_members
    with capture_logs() as logs:
        await callback(request())
    assert membership_reads == 6
    assert current[0] is not original_config
    sent.assert_not_awaited()
    assert dropped(logs) == ["config_changed"]


@pytest.mark.asyncio
async def test_catalog_receiver_delivers_through_public_nio_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported send arguments must not silently drop an authorized catalog."""
    scope_client, config, paths, index, router, agent = picker_setup(tmp_path)
    async with olm_transport(sender=router, recipient=USER) as (client, peer, requests, _):
        assert peer.olm is not None
        peer_keys = peer.olm.share_keys()["device_keys"]
        await client.receive_response(nio.KeysQueryResponse({USER: {"DESKTOP": peer_keys}}, {}))
        client.rooms.update(scope_client.rooms)
        monkeypatch.setattr(client, "joined_members", scope_client.joined_members)
        monkeypatch.setattr(client, "room_get_event", scope_client.room_get_event)
        registered = MagicMock(wraps=client.add_to_device_callback)
        monkeypatch.setattr(client, "add_to_device_callback", registered)
        register_model_catalog_receiver(
            client=client,
            agent_name="router",
            runtime_paths=paths,
            config_getter=lambda: config,
            membership_index=index,
            callback_wrapper=lambda callback: callback,
        )
        event = request()
        event.authenticated_sender = nio.AuthenticatedDevice(
            USER,
            "DESKTOP",
            peer_keys["keys"]["curve25519:DESKTOP"],
            peer_keys["keys"]["ed25519:DESKTOP"],
        )
        await registered.call_args.args[0](event)
        assert scope_client.joined_members.await_count == 6
        deliveries = [item for item in requests if "/sendToDevice/" in item["path"]]
        assert len(deliveries) == 1
        messages = deliveries[0]["body"]["messages"]
        assert set(messages) == {USER}
        assert set(messages[USER]) == {"DESKTOP"}
        assert agent not in str(messages)
        assert messages[USER]["DESKTOP"]["algorithm"] == "m.olm.v1.curve25519-aes-sha2"
