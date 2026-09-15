"""Structured operations through the existing command handler."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.commands.handler import handle_command
from mindroom.commands.parsing import command_parser
from mindroom.config.models import ModelConfig
from mindroom.message_target import MessageTarget
from mindroom.model_selection import command_result_content_to_dict
from mindroom.thread_models import resolve_thread_model_override, set_thread_model_override
from mindroom.turn_record import TurnRecord
from tests.authorization_helpers import make_test_command_handler_context
from tests.conftest import make_conversation_reader_mock
from tests.test_conversation_hydration import encrypted
from tests.test_model_selection_scope import ROOM, USER, joined_response, picker_setup, root_event
from tests.test_turn_controller_focused import _build_harness

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("operation", "model", "expected"),
    [
        ("reset", None, None),
        ("set", "reset", "reset"),
        ("set", "list", "list"),
        ("set", "default", "default"),
    ],
)
@pytest.mark.asyncio
async def test_explicit_model_operation(
    tmp_path: Path,
    operation: str,
    model: str | None,
    expected: str | None,
) -> None:
    """Explicit reset clears even with a reset key; explicit set never uses aliases."""
    client, config, paths, index, router, _ = picker_setup(tmp_path)
    config.models.update({name: ModelConfig(provider="openai", id="test-model") for name in ("reset", "list")})
    set_thread_model_override(paths, thread_id="$root", model_name="default", room_id=ROOM, set_by=USER)
    metadata = {"version": 1, "runtime_user_id": router, "runtime_device_id": "DEVICE", "operation": operation}
    if model is not None:
        metadata["model"] = model
    event = root_event(
        event_id="$command",
        content={"body": "!model reset", "msgtype": "m.text", "io.mindroom.model_selection": metadata},
    )
    command = command_parser.parse(event.body)
    results = []

    async def record(text: str, *, extra_content: dict | None = None) -> None:
        results.append((text, extra_content))

    context = make_test_command_handler_context(
        client=client,
        config=config,
        runtime_paths=paths,
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve(ROOM, "$root", "$command"),
        record_handled_turn=AsyncMock(),
        record_command_result=record,
        send_response=AsyncMock(return_value="$reply"),
        agent_reply_memberships=index,
    )
    await handle_command(context=context, room=client.rooms[ROOM], event=event, command=command, requester_user_id=USER)
    assert resolve_thread_model_override(paths, "$root", configured_models=config.models).active == expected
    result = results[0][1]["io.mindroom.model_selection_result"]
    assert result["operation"] == operation
    assert result["status"] == "applied"
    assert result["override"] == expected


@pytest.mark.parametrize(
    "failure",
    ["malformed", "missing_root", "foreign_root", "child", "requester_left", "unknown_model", "encrypted"],
)
@pytest.mark.asyncio
async def test_structured_rejection_never_falls_back_to_body(tmp_path: Path, failure: str) -> None:
    """Rejected metadata or scope must leave the override untouched despite valid body."""
    client, config, paths, index, router, agent = picker_setup(tmp_path)
    config.models["reset"] = ModelConfig(provider="openai", id="test-model")
    set_thread_model_override(paths, thread_id="$root", model_name="default", room_id=ROOM, set_by=USER)
    metadata = {"version": 1, "runtime_user_id": router, "runtime_device_id": "DEVICE", "operation": "reset"}
    if failure == "malformed":
        metadata["model"] = "reset"
    elif failure == "missing_root":
        client.room_get_event.return_value = nio.RoomGetEventError("Not found", "M_NOT_FOUND")
    elif failure == "foreign_root":
        client.room_get_event.return_value = nio.RoomGetEventResponse.from_dict(
            root_event(room_id="!foreign:localhost").source,
        )
    elif failure == "child":
        client.room_get_event.return_value = nio.RoomGetEventResponse.from_dict(
            root_event(
                content={
                    "body": "child",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$parent"},
                },
            ).source,
        )
    elif failure == "requester_left":
        client.joined_members.return_value = joined_response(router, agent)
    elif failure == "unknown_model":
        metadata.update(operation="set", model="deleted")
    elif failure == "encrypted":
        client.room_get_event.return_value = nio.RoomGetEventResponse.from_dict(encrypted("$root", sender=USER))
    event = root_event(
        event_id="$command",
        content={"body": "!model reset", "msgtype": "m.text", "io.mindroom.model_selection": metadata},
    )
    command = command_parser.parse(event.body)
    results = []

    async def record(text: str, *, extra_content: dict | None = None) -> None:
        results.append((text, extra_content))

    context = make_test_command_handler_context(
        client=client,
        config=config,
        runtime_paths=paths,
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve(ROOM, "$root", "$command"),
        record_handled_turn=AsyncMock(),
        record_command_result=record,
        send_response=AsyncMock(return_value="$reply"),
        agent_reply_memberships=index,
    )
    await handle_command(context=context, room=client.rooms[ROOM], event=event, command=command, requester_user_id=USER)
    assert resolve_thread_model_override(paths, "$root", configured_models=config.models).active == "default"
    assert "❌" in results[0][0]
    if failure == "malformed":
        assert results[0][1] is None
    else:
        result = results[0][1]["io.mindroom.model_selection_result"]
        assert result["status"] == "rejected"
        assert "override" not in result


@pytest.mark.parametrize("wrong_target", ["runtime_user_id", "runtime_device_id"])
@pytest.mark.asyncio
async def test_other_runtime_target_never_owns_checkpoint(tmp_path: Path, wrong_target: str) -> None:
    """A second runtime device must not admit or acknowledge the selected device's command."""
    client, config, paths, _, router, _ = picker_setup(tmp_path)
    harness = _build_harness(config, tmp_path / "turns", agent_name="router")
    executor = harness.controller.deps.command_executor
    executor.deps.runtime.client = client
    metadata = {
        "version": 1,
        "runtime_user_id": router,
        "runtime_device_id": "DEVICE",
        "operation": "set",
        "model": "default",
    }
    metadata[wrong_target] = "@other:localhost" if wrong_target == "runtime_user_id" else "SECOND_DEVICE"
    event = root_event(
        event_id="$command",
        content={"body": "!model default", "msgtype": "m.text", "io.mindroom.model_selection": metadata},
    )
    command = command_parser.parse(event.body)
    owned = await executor.execute_if_owned(
        client.rooms[ROOM],
        event,
        USER,
        command,
        target=MessageTarget.resolve(ROOM, "$root", "$command"),
        handled_turn=TurnRecord.create(["$command"]),
    )
    assert owned is False
    assert harness.turn_store.get_turn_record("$command") is None
    assert not harness.gateway.sent
    assert resolve_thread_model_override(paths, "$root", configured_models=config.models).active is None


@pytest.mark.asyncio
async def test_replay_preserves_result_after_single_real_mutation(tmp_path: Path) -> None:
    """A failed Matrix send must replay the frozen result even after current state changes."""
    client, config, paths, _, router, _ = picker_setup(tmp_path)
    harness = _build_harness(config, tmp_path / "turns", agent_name="router")
    executor = harness.controller.deps.command_executor
    executor.deps.runtime.client = client
    metadata = {
        "version": 1,
        "runtime_user_id": router,
        "runtime_device_id": "DEVICE",
        "operation": "set",
        "model": "default",
    }
    event = root_event(
        event_id="$command",
        content={"body": "!model default", "msgtype": "m.text", "io.mindroom.model_selection": metadata},
    )
    command = command_parser.parse(event.body)
    target = MessageTarget.resolve(ROOM, "$root", "$command")
    sent = []

    async def fail_send(request: object) -> None:
        sent.append(request)

    with patch.object(harness.gateway, "send_text", new=fail_send), pytest.raises(RuntimeError, match="did not return"):
        await executor.execute(
            client.rooms[ROOM],
            event,
            USER,
            command,
            target=target,
            handled_turn=TurnRecord.create(["$command"]),
        )
    pending = harness.turn_store.get_turn_record("$command")
    assert pending is not None
    assert resolve_thread_model_override(paths, "$root", configured_models=config.models).active == "default"
    saved = json.dumps(command_result_content_to_dict(pending.command_result_extra_content), sort_keys=True)
    assert sent[0].extra_content["io.mindroom.model_selection_result"]["command_event_id"] == "$command"
    # A different writer changes the live state after the failed send. Replay
    # must preserve the first outcome without reapplying its old mutation.
    config.models["later"] = ModelConfig(provider="openai", id="later-model")
    set_thread_model_override(paths, thread_id="$root", model_name="later", room_id=ROOM, set_by=USER)
    await executor.execute(client.rooms[ROOM], event, USER, command, target=target, handled_turn=pending)
    assert resolve_thread_model_override(paths, "$root", configured_models=config.models).active == "later"
    assert json.dumps(harness.gateway.sent[0].extra_content, sort_keys=True) == saved
    assert harness.gateway.sent[0].response_text == sent[0].response_text
    assert harness.gateway.sent[0].delivery_turn_id == sent[0].delivery_turn_id


@pytest.mark.asyncio
async def test_crash_before_result_returns_uncertainty_without_success(tmp_path: Path) -> None:
    """An execution-start checkpoint cannot manufacture applied metadata after restart."""
    client, config, _, _, router, _ = picker_setup(tmp_path)
    harness = _build_harness(config, tmp_path / "turns", agent_name="router")
    executor = harness.controller.deps.command_executor
    executor.deps.runtime.client = client
    event = root_event(
        event_id="$command",
        content={
            "body": "!model default",
            "msgtype": "m.text",
            "io.mindroom.model_selection": {
                "version": 1,
                "runtime_user_id": router,
                "runtime_device_id": "DEVICE",
                "operation": "set",
                "model": "default",
            },
        },
    )
    command = command_parser.parse(event.body)
    pending = await harness.turn_store.record_pending_turn(
        TurnRecord.create(["$command"], completed=False, command_execution_started=True),
    )
    assert pending is not None
    await executor.execute(
        client.rooms[ROOM],
        event,
        USER,
        command,
        target=MessageTarget.resolve(ROOM, "$root", "$command"),
        handled_turn=pending,
    )
    assert "uncertain" in harness.gateway.sent[0].response_text
    assert harness.gateway.sent[0].extra_content is None


@pytest.mark.parametrize("second_structured", [False, True])
@pytest.mark.asyncio
async def test_same_thread_senders_cannot_overtake_waiting_model_command(
    tmp_path: Path,
    *,
    second_structured: bool,
) -> None:
    """A second sender's text or picker command must wait behind pending scope proof."""
    client, config, paths, _, router, agent = picker_setup(tmp_path)
    config.models["later"] = ModelConfig(provider="openai", id="later-model")
    second_user = "@second:localhost"
    config.agents["helper"].access.users.append(second_user)
    client.joined_members.return_value = joined_response(USER, second_user, router, agent)
    harness = _build_harness(config, tmp_path / "turns", agent_name="router")
    executor = harness.controller.deps.command_executor
    executor.deps.runtime.client = client
    first_read = asyncio.Event()
    release_first = asyncio.Event()
    reads = 0

    async def read_root(*_args: object) -> nio.RoomGetEventResponse:
        nonlocal reads
        reads += 1
        if reads == 1:
            first_read.set()
            await release_first.wait()
        return nio.RoomGetEventResponse.from_dict(root_event().source)

    client.room_get_event.side_effect = read_root

    def event_for(event_id: str, sender: str, model: str, *, structured: bool) -> nio.Event:
        content = {"body": f"!model {model}", "msgtype": "m.text"}
        if structured:
            content["io.mindroom.model_selection"] = {
                "version": 1,
                "runtime_user_id": router,
                "runtime_device_id": "DEVICE",
                "operation": "set",
                "model": model,
            }
        return root_event(event_id=event_id, sender=sender, content=content)

    async def run(event: nio.RoomMessageText) -> None:
        command = command_parser.parse(event.body)
        await executor.execute(
            client.rooms[ROOM],
            event,
            event.sender,
            command,
            target=MessageTarget.resolve(ROOM, "$root", event.event_id),
            handled_turn=TurnRecord.create([event.event_id]),
        )

    first = asyncio.create_task(run(event_for("$first", USER, "default", structured=True)))
    await first_read.wait()
    second = asyncio.create_task(run(event_for("$second", second_user, "later", structured=second_structured)))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), timeout=0.2)
        assert resolve_thread_model_override(paths, "$root", configured_models=config.models).active is None
    finally:
        release_first.set()
        await asyncio.gather(first, second)
    assert resolve_thread_model_override(paths, "$root", configured_models=config.models).active == "later"
    assert [request.delivery_turn_id for request in harness.gateway.sent] == ["$first", "$second"]
