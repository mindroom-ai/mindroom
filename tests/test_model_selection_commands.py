"""Structured operations through the existing command handler."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.commands.handler import handle_command
from mindroom.commands.model_commands import handle_model_command
from mindroom.commands.parsing import command_parser
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.entity_resolution import entity_identity_registry
from mindroom.message_target import MessageTarget
from mindroom.model_selection import command_result_content_to_dict
from mindroom.teams import TeamTurnModelSelection, resolve_team_turn_models
from mindroom.thread_models import resolve_thread_model_override, set_thread_model_override
from mindroom.turn_record import TurnRecord
from tests.authorization_helpers import make_test_command_handler_context
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, runtime_paths_for, test_runtime_paths
from tests.test_model_selection_scope import ROOM, USER, joined_response, picker_client, picker_setup, root_event
from tests.test_turn_controller_focused import _build_harness

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _helper_override(paths: RuntimePaths, config: Config) -> str | None:
    return resolve_thread_model_override(paths, "$root", config=config).active.get("helper")


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
    set_thread_model_override(
        paths,
        thread_id="$root",
        model_name="default",
        room_id=ROOM,
        set_by=USER,
        entity_names=("helper",),
        config=config,
    )
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
    assert _helper_override(paths, config) == expected
    result = results[0][1]["io.mindroom.model_selection_result"]
    assert result["operation"] == operation
    assert result["status"] == "applied"
    assert result["override"] == expected


@pytest.mark.parametrize("model_key", [" reset ", " clear ", " list ", " show ", " default ", "fast", " fast "])
@pytest.mark.asyncio
async def test_structured_set_keeps_exact_key_in_storage_reply_and_ack(tmp_path: Path, model_key: str) -> None:
    """Text trimming must never turn an explicit key into an alias or another model."""
    client, config, paths, _, router, _ = picker_setup(tmp_path)
    config.models.update(
        {
            " reset ": ModelConfig(provider="openai", id="padded-reset"),
            " clear ": ModelConfig(provider="openai", id="padded-clear"),
            " list ": ModelConfig(provider="openai", id="padded-list"),
            " show ": ModelConfig(provider="openai", id="padded-show"),
            " default ": ModelConfig(provider="openai", id="padded-default"),
            "fast": ModelConfig(provider="openai", id="plain-fast"),
            " fast ": ModelConfig(provider="openai", id="padded-fast"),
        },
    )
    set_thread_model_override(
        paths,
        thread_id="$root",
        model_name="default",
        room_id=ROOM,
        set_by=USER,
        entity_names=("helper",),
        config=config,
    )
    harness = _build_harness(config, tmp_path / "turns", agent_name="router")
    executor = harness.controller.deps.command_executor
    executor.deps.runtime.client = client
    event = root_event(
        event_id="$command",
        content={
            "body": f"!model {model_key}",
            "msgtype": "m.text",
            "io.mindroom.model_selection": {
                "version": 1,
                "runtime_user_id": router,
                "runtime_device_id": "DEVICE",
                "operation": "set",
                "model": model_key,
            },
        },
    )
    command = command_parser.parse(event.body)
    await executor.execute(
        client.rooms[ROOM],
        event,
        USER,
        command,
        target=MessageTarget.resolve(ROOM, "$root", "$command"),
        handled_turn=TurnRecord.create(["$command"]),
    )
    assert _helper_override(paths, config) == model_key
    reply = harness.gateway.sent[0]
    assert f"now uses `{model_key}`" in reply.response_text
    result = reply.extra_content["io.mindroom.model_selection_result"]
    assert result["status"] == "applied"
    assert result["model"] == result["override"] == model_key


@pytest.mark.parametrize("failure", ["malformed", "missing_root", "unknown_model"])
@pytest.mark.asyncio
async def test_structured_rejection_never_falls_back_to_body(tmp_path: Path, failure: str) -> None:
    """Rejected metadata or scope must leave the override untouched despite valid body."""
    client, config, paths, index, router, _ = picker_setup(tmp_path)
    config.models["reset"] = ModelConfig(provider="openai", id="test-model")
    set_thread_model_override(
        paths,
        thread_id="$root",
        model_name="default",
        room_id=ROOM,
        set_by=USER,
        entity_names=("helper",),
        config=config,
    )
    metadata = {"version": 1, "runtime_user_id": router, "runtime_device_id": "DEVICE", "operation": "reset"}
    if failure == "malformed":
        metadata["model"] = "reset"
    elif failure == "missing_root":
        client.room_get_event.return_value = nio.RoomGetEventError("Not found", "M_NOT_FOUND")
    elif failure == "unknown_model":
        metadata.update(operation="set", model="deleted")
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
    assert _helper_override(paths, config) == "default"
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
    assert _helper_override(paths, config) is None


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
    assert _helper_override(paths, config) == "default"
    saved = json.dumps(command_result_content_to_dict(pending.command_result_extra_content), sort_keys=True)
    assert sent[0].extra_content["io.mindroom.model_selection_result"]["command_event_id"] == "$command"
    # A different writer changes the live state after the failed send. Replay
    # must preserve the first outcome without reapplying its old mutation.
    config.models["later"] = ModelConfig(provider="openai", id="later-model")
    set_thread_model_override(
        paths,
        thread_id="$root",
        model_name="later",
        room_id=ROOM,
        set_by=USER,
        entity_names=("helper",),
        config=config,
    )
    await executor.execute(client.rooms[ROOM], event, USER, command, target=target, handled_turn=pending)
    assert _helper_override(paths, config) == "later"
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
        assert _helper_override(paths, config) is None
    finally:
        release_first.set()
        await asyncio.gather(first, second)
    assert _helper_override(paths, config) == "later"
    assert [request.delivery_turn_id for request in harness.gateway.sent] == ["$first", "$second"]


OTHER = "@other:localhost"


def restricted_config(tmp_path: Path) -> Config:
    """Return ``helper`` addressable by USER and OTHER, and ``restricted`` addressable only by OTHER."""
    return bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(display_name="Helper", access=ResponderAccessConfig(users=[USER, OTHER])),
                "restricted": AgentConfig(display_name="Restricted", access=ResponderAccessConfig(users=[OTHER])),
            },
            models={
                "default": ModelConfig(provider="openai", id="test-model"),
                "cheap": ModelConfig(provider="openai", id="cheap-model"),
                "expensive": ModelConfig(provider="openai", id="expensive-model"),
            },
        ),
        test_runtime_paths(tmp_path),
    )


def resolved_thread_models(config: Config, paths: RuntimePaths) -> dict[str, str]:
    """Return the model each agent of ``restricted_config`` runs with in the ``$root`` thread."""
    return {
        name: config.resolve_runtime_model(
            entity_name=name,
            room_id=ROOM,
            thread_id="$root",
            runtime_paths=paths,
        ).model_name
        for name in ("helper", "restricted")
    }


def _run_model_command(config: Config, args_text: str, requester_user_id: str) -> str:
    return handle_model_command(
        args_text,
        config=config,
        runtime_paths=runtime_paths_for(config),
        membership_index=AgentReplyMembershipIndex(),
        room_id=ROOM,
        thread_id="$root",
        requester_user_id=requester_user_id,
    )


def test_thread_overrides_keep_the_selection_of_entities_the_next_setter_cannot_address(tmp_path: Path) -> None:
    """Each setter changes or resets only the entities it may address, so a restricted entity keeps its selection."""
    config = restricted_config(tmp_path)
    paths = runtime_paths_for(config)

    for args_text, requester_user_id, expected in (
        ("expensive", OTHER, {"helper": "expensive", "restricted": "expensive"}),
        ("cheap", USER, {"helper": "cheap", "restricted": "expensive"}),
        ("reset", USER, {"helper": "default", "restricted": "expensive"}),
        ("reset", OTHER, {"helper": "default", "restricted": "default"}),
    ):
        _run_model_command(config, args_text, requester_user_id)
        assert resolved_thread_models(config, paths) == expected, (args_text, requester_user_id)


def test_team_thread_override_governs_members_a_team_only_requester_reaches(tmp_path: Path) -> None:
    """A requester whom only the team admits switches the model of the team's members during its turns too."""
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", access=ResponderAccessConfig(users=[OTHER]))},
            teams={
                "squad": TeamConfig(
                    display_name="Squad",
                    role="Ship code",
                    agents=["code"],
                    access=ResponderAccessConfig(users=[USER]),
                ),
            },
            models={
                "default": ModelConfig(provider="openai", id="test-model"),
                "expensive": ModelConfig(provider="openai", id="expensive-model"),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    _run_model_command(config, "expensive", USER)

    assert resolve_team_turn_models(
        "squad",
        ["code"],
        ROOM,
        config,
        runtime_paths_for(config),
        thread_id="$root",
    ) == TeamTurnModelSelection(team_model_name="expensive", member_model_names={"code": "expensive"})


@pytest.mark.asyncio
async def test_structured_picker_keeps_the_selection_of_entities_the_requester_cannot_address(tmp_path: Path) -> None:
    """A picker set or reset changes only the entities its requester may address."""
    config = restricted_config(tmp_path)
    paths = runtime_paths_for(config)
    registry = entity_identity_registry(config, paths)
    agents = (registry.current_id(name).full_id for name in ("helper", "restricted"))
    client = picker_client(config, paths, USER, OTHER, *agents)
    context = make_test_command_handler_context(
        client=client,
        config=config,
        runtime_paths=paths,
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve(ROOM, "$root", "$command"),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )

    for number, (requester_user_id, operation, model, expected) in enumerate(
        (
            (OTHER, "set", "expensive", {"helper": "expensive", "restricted": "expensive"}),
            (USER, "set", "cheap", {"helper": "cheap", "restricted": "expensive"}),
            (USER, "reset", None, {"helper": "default", "restricted": "expensive"}),
        ),
    ):
        metadata = {
            "version": 1,
            "runtime_user_id": client.user_id,
            "runtime_device_id": "DEVICE",
            "operation": operation,
        }
        if model is not None:
            metadata["model"] = model
        event = root_event(
            event_id=f"$command{number}",
            sender=requester_user_id,
            content={"body": "!model", "msgtype": "m.text", "io.mindroom.model_selection": metadata},
        )
        await handle_command(
            context=context,
            room=client.rooms[ROOM],
            event=event,
            command=command_parser.parse(event.body),
            requester_user_id=requester_user_id,
        )
        assert resolved_thread_models(config, paths) == expected, (requester_user_id, operation)


def test_model_show_names_the_entities_each_thread_override_governs(tmp_path: Path) -> None:
    """Showing the thread's overrides must not claim one for an entity it does not apply to."""
    config = restricted_config(tmp_path)
    _run_model_command(config, "expensive", OTHER)
    _run_model_command(config, "cheap", USER)

    shown = _run_model_command(config, "", USER)

    assert "- `cheap` (openai cheap-model) for `helper`\n" in shown
    assert "- `expensive` (openai expensive-model) for `restricted`\n" in shown


@pytest.mark.asyncio
async def test_thread_override_leaves_entities_the_requester_cannot_address(tmp_path: Path) -> None:
    """A member excluded by an agent's access cannot switch the model that agent uses in the thread."""
    config = restricted_config(tmp_path)
    paths = runtime_paths_for(config)
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = entity_identity_registry(config, paths).current_id("router").full_id
    room = nio.MatrixRoom(ROOM, client.user_id)
    event = root_event(event_id="$command", content={"body": "!model expensive", "msgtype": "m.text"})
    context = make_test_command_handler_context(
        client=client,
        config=config,
        runtime_paths=paths,
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve(ROOM, "$root", "$command"),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )

    await handle_command(
        context=context,
        room=room,
        event=event,
        command=command_parser.parse(event.body),
        requester_user_id=USER,
    )

    assert resolved_thread_models(config, paths) == {"helper": "expensive", "restricted": "default"}
