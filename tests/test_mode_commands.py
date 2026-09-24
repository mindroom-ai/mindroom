"""Mode commands authorize the named responder and preserve choices on refusal."""

from __future__ import annotations

# ruff: noqa: D103
from dataclasses import replace
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from agno.tools.toolkit import Toolkit

from mindroom import agents
from mindroom.agent_modes import clear_agent_mode, resolve_agent_mode, set_agent_mode
from mindroom.commands import mode_commands
from mindroom.commands.handler import handle_command
from mindroom.commands.parsing import CommandType, _CommandParser
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.credentials import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.message_target import MessageTarget
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target
from tests.authorization_helpers import make_test_command_handler_context
from tests.conftest import make_conversation_reader_mock, unwrap_extracted_collaborator
from tests.identity_helpers import persist_entity_accounts
from tests.response_runner_helpers import _bot, _plain_request
from tests.test_agent_cli_authority import _runtime_context as _authority_runtime_context

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


_CLI_DEPLOYMENT_ENV = {
    "MINDROOM_API_KEY": "fake-admin-key",
    "MINDROOM_AGENT_CLI_GATEWAY_URL": "http://gateway.test",
    "MINDROOM_AGENT_CLI_PRIMARY_URL": "http://primary.test",
    "MINDROOM_WORKER_BACKEND": "docker",
    "MINDROOM_DOCKER_WORKER_IMAGE": "mindroom-worker:test",
    "MINDROOM_DOCKER_WORKER_USER": "1000:1000",
}


def _runtime_context(tmp_path: Path) -> ToolRuntimeContext:
    context = _authority_runtime_context(tmp_path)
    return replace(
        context,
        target=MessageTarget.resolve(
            context.target.room_id,
            context.target.source_thread_id,
            context.target.reply_to_event_id,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("thread_mode", ["room", "thread"])
@pytest.mark.parametrize("action", ["standard", "reset", "show"])
async def test_mode_command_matches_response_runner_session(
    tmp_path: Path,
    private: bool,
    thread_mode: Literal["room", "thread"],
    action: str,
) -> None:
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    config = runner.deps.runtime.config
    paths = runner.deps.runtime_paths
    config.agents["general"].thread_mode = thread_mode
    config.agents["general"].private = AgentPrivateConfig(per="user") if private else None
    config.administrators = ["@user:localhost"]
    target = MessageTarget.resolve("!room:localhost", "$thread", "$next", room_mode=thread_mode == "room")
    request = _plain_request(target)
    runtime = await runner.prepare_response_runtime(request)
    root = resolve_agent_storage("general", config, paths, runtime.tool_dispatch.execution_identity).state_root
    set_agent_mode(root, "general", target.session_id, "minimal", request.user_id)
    result = mode_commands.handle_mode_command(
        f"general {action}",
        config=config,
        runtime_paths=paths,
        target=MessageTarget.resolve("!room:localhost", "$thread", "$command"),
        requester_id=request.user_id,
        membership_index=runner.deps.runtime.agent_reply_memberships,
    )
    turn = runner._agent_turn_context(
        request,
        runtime=runtime,
        run_id="run",
        active_event_ids={"$next"},
        transient_enrichment_items=(),
        system_enrichment_items=(),
    )
    expected = "minimal" if action == "show" else "standard"
    assert turn.agent_mode == expected
    assert f"uses `{expected}`" in result
    # Choosing the default keeps no record in the bounded mode store.
    assert clear_agent_mode(root, "general", target.session_id) is (action == "show")


@pytest.mark.asyncio
async def test_private_agent_mode_is_isolated_per_requester(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    config = runner.deps.runtime.config
    paths = runner.deps.runtime_paths
    config.agents["general"].private = AgentPrivateConfig(per="user")
    config.administrators = ["@user:localhost", "@other:localhost"]
    target = MessageTarget.resolve("!room:localhost", "$thread", "$next")
    request = _plain_request(target)
    runtime = await runner.prepare_response_runtime(request)
    root = resolve_agent_storage("general", config, paths, runtime.tool_dispatch.execution_identity).state_root
    set_agent_mode(root, "general", target.session_id, "minimal", request.user_id)

    def show(requester_id: str) -> str:
        return mode_commands.handle_mode_command(
            "general show",
            config=config,
            runtime_paths=paths,
            target=MessageTarget.resolve("!room:localhost", "$thread", "$command"),
            requester_id=requester_id,
            membership_index=runner.deps.runtime.agent_reply_memberships,
        )

    assert request.user_id == "@user:localhost"
    assert "uses `minimal`" in show("@user:localhost")
    assert "uses `standard`" in show("@other:localhost")


def test_thread_mode_command_rejects_main_timeline_without_saving(tmp_path: Path) -> None:
    context = _runtime_context(tmp_path)
    context.config.administrators = [context.requester_id]
    result = mode_commands.handle_mode_command(
        "helper standard",
        config=context.config,
        runtime_paths=context.runtime_paths,
        target=MessageTarget.resolve(context.room_id, None, "$command", thread_start_root_event_id="$command"),
        requester_id=context.requester_id,
        membership_index=context.agent_reply_memberships,
    )
    assert "inside an existing thread" in result
    assert not list(tmp_path.rglob("agent_modes.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("plain_reply", [False, True])
async def test_mode_handler_preserves_canonical_or_explicit_source_thread(tmp_path: Path, plain_reply: bool) -> None:
    runtime = _runtime_context(tmp_path)
    runtime.config.administrators = [runtime.requester_id]
    root = resolve_agent_storage(
        "helper",
        runtime.config,
        runtime.runtime_paths,
        build_execution_identity_from_runtime_context(runtime),
    ).state_root
    set_agent_mode(root, "helper", runtime.session_id, "minimal", runtime.requester_id)
    room = nio.MatrixRoom(runtime.room_id, "@router:example.test")
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$command",
            "sender": runtime.requester_id,
            "origin_server_ts": 1,
            "type": "m.room.message",
            "content": {
                "body": "!mode helper standard",
                "msgtype": "m.text",
                "m.relates_to": (
                    {"m.in_reply_to": {"event_id": "$thread-message"}}
                    if plain_reply
                    else {"rel_type": "m.thread", "event_id": runtime.thread_id}
                ),
            },
        },
    )
    context = make_test_command_handler_context(
        client=runtime.client,
        config=runtime.config,
        runtime_paths=runtime.runtime_paths,
        logger=MagicMock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve(
            runtime.room_id,
            runtime.thread_id if plain_reply else None,
            "$command",
            room_mode=not plain_reply,
        ),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
        agent_reply_memberships=runtime.agent_reply_memberships,
    )
    await handle_command(
        context=context,
        room=room,
        event=event,
        command=_CommandParser().parse(event.body),
        requester_user_id=runtime.requester_id,
    )
    assert resolve_agent_mode(root, "helper", runtime.session_id) == "standard"


def test_mode_parser_preserves_target_and_action() -> None:
    command = _CommandParser().parse("!mode helper minimal")
    assert command is not None
    assert command.type == CommandType.MODE
    assert command.args == {"args_text": "helper minimal"}


_NON_ROOT_USER_REQUIRED = "CLI workers require an explicit non-root Docker user."


@pytest.mark.parametrize(
    ("invalid_env", "reason"),
    [
        ({"MINDROOM_DOCKER_WORKER_USER": "root"}, _NON_ROOT_USER_REQUIRED),
        ({"MINDROOM_DOCKER_WORKER_USER": "0:1000"}, _NON_ROOT_USER_REQUIRED),
        ({"MINDROOM_DOCKER_WORKER_USER": ""}, _NON_ROOT_USER_REQUIRED),
        (
            {"MINDROOM_DOCKER_WORKER_IMAGE": ""},
            "MINDROOM_DOCKER_WORKER_IMAGE must be set when MINDROOM_WORKER_BACKEND=docker.",
        ),
        (
            {"MINDROOM_DOCKER_WORKER_ENV_JSON": '{"CUSTOM_SECRET": "fake-secret-value"}'},
            "CLI workers do not support Docker extra env.",
        ),
    ],
)
def test_selection_refuses_invalid_docker_profile_before_saving(
    tmp_path: Path,
    invalid_env: dict[str, str],
    reason: str,
) -> None:
    context = _runtime_context(tmp_path)
    context.config.administrators = [context.requester_id]
    context.config.agents["helper"] = AgentConfig(display_name="Helper", tools=["shell"], memory_backend="file")
    paths = replace(context.runtime_paths, process_env=_CLI_DEPLOYMENT_ENV | invalid_env)
    storage = resolve_agent_storage(
        "helper",
        context.config,
        paths,
        build_execution_identity_from_runtime_context(context),
    )
    set_agent_mode(storage.state_root, "helper", context.session_id, "standard", context.requester_id)
    saved = (storage.state_root / "agent_modes.json").read_bytes()
    result = mode_commands.handle_mode_command(
        "helper minimal",
        config=context.config,
        runtime_paths=paths,
        target=context.target,
        requester_id=context.requester_id,
        membership_index=context.agent_reply_memberships,
    )
    assert result == f"Minimal mode is unavailable: {reason}"
    assert "fake-secret-value" not in result
    assert (storage.state_root / "agent_modes.json").read_bytes() == saved


def test_mode_command_refusal_and_recovery_controls(tmp_path: Path) -> None:
    context = _runtime_context(tmp_path)
    context.config.administrators = [context.requester_id]
    kwargs = {
        "config": context.config,
        "runtime_paths": context.runtime_paths,
        "target": context.target,
        "requester_id": context.requester_id,
        "membership_index": context.agent_reply_memberships,
    }
    result = mode_commands.handle_mode_command("helper minimal", **kwargs)
    assert "shell" in result
    result = mode_commands.handle_mode_command("missing minimal", **kwargs)
    assert "Unknown agent" in result

    root = resolve_agent_storage(
        "helper",
        context.config,
        context.runtime_paths,
        build_execution_identity_from_runtime_context(context),
    ).state_root
    set_agent_mode(root, "helper", context.session_id, "minimal", context.requester_id)
    assert "minimal" in mode_commands.handle_mode_command("helper show", **kwargs)
    assert "shell" in mode_commands.handle_mode_command("helper minimal", **kwargs)
    assert resolve_agent_mode(root, "helper", context.session_id) == "minimal"
    assert "standard" in mode_commands.handle_mode_command("helper standard", **kwargs)
    assert resolve_agent_mode(root, "helper", context.session_id) == "standard"
    assert "standard" in mode_commands.handle_mode_command("helper reset", **kwargs)
    assert "denied" in mode_commands.handle_mode_command(
        "helper minimal",
        **(kwargs | {"requester_id": "@mallory:other"}),
    )


@pytest.mark.parametrize(
    "settings_scope",
    ["global", "scoped", "inherited_allowed", "inherited_ungranted", "scoped_override"],
)
@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize(
    "restriction",
    [
        {"enable_run_shell_command": False},
        {"include_tools": ["run_shell_command"]},
        {"exclude_tools": ["kill_shell_command"]},
    ],
)
def test_selection_checks_effective_shell_permissions(
    tmp_path: Path,
    settings_scope: str,
    restore: bool,
    restriction: dict[str, object],
) -> None:

    context = _runtime_context(tmp_path)
    context.config.administrators = [context.requester_id]
    restored = {
        "enable_run_shell_command": True,
        "include_tools": ["run_shell_command", "check_shell_command", "kill_shell_command"],
        "exclude_tools": [],
    }
    context.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        memory_backend="file",
        tools=[{"shell": restored}] if restore else ["shell"],
        worker_scope=None if settings_scope == "global" else "user",
    )
    context.config.defaults.worker_grantable_credentials = (
        ["shell"] if settings_scope in {"inherited_allowed", "scoped_override"} else []
    )
    paths = replace(
        context.runtime_paths,
        process_env=_CLI_DEPLOYMENT_ENV,
    )
    identity = build_execution_identity_from_runtime_context(context)
    storage = resolve_agent_storage("helper", context.config, paths, identity)
    target = build_agent_toolkit_worker_target(
        storage.execution.execution_scope,
        "helper",
        is_private=storage.execution.is_private,
        execution_identity=identity,
        runtime_paths=paths,
    )
    save_scoped_credentials(
        "shell",
        restriction,
        credentials_manager=get_runtime_credentials_manager(paths),
        worker_target=target if settings_scope == "scoped" else None,
    )
    if settings_scope == "scoped_override":
        save_scoped_credentials(
            "shell",
            restored,
            credentials_manager=get_runtime_credentials_manager(paths),
            worker_target=target,
        )
    set_agent_mode(storage.state_root, "helper", context.session_id, "standard", context.requester_id)
    prior_choice = (storage.state_root / "agent_modes.json").read_bytes()
    result = mode_commands.handle_mode_command(
        "helper minimal",
        config=context.config,
        runtime_paths=paths,
        target=context.target,
        requester_id=context.requester_id,
        membership_index=context.agent_reply_memberships,
    )
    permitted = restore or settings_scope in {"inherited_ungranted", "scoped_override"}
    persist_entity_accounts(context.config, paths)
    canonical = agents.create_agent(
        "helper",
        context.config,
        paths,
        identity,
        session_id=context.session_id,
        persist_runtime_state=False,
    )
    assert isinstance(canonical.tools, list)
    shell_functions = {
        name
        for tool in canonical.tools
        if isinstance(tool, Toolkit) and tool.name == "shell_tools"
        for name in tool.get_async_functions()
    }
    required = {"run_shell_command", "check_shell_command", "kill_shell_command"}
    assert required.issubset(shell_functions) is permitted
    assert ("uses `minimal`" if permitted else "run, check, and kill") in result
    assert resolve_agent_mode(storage.state_root, "helper", context.session_id) == (
        "minimal" if permitted else "standard"
    )
    if not permitted:
        assert (storage.state_root / "agent_modes.json").read_bytes() == prior_choice


@pytest.mark.parametrize("failure", ["registry_import", "tool_import", "tool_config"])
def test_selection_preserves_choice_on_shell_preparation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Expected dependency/config failures refuse selection without exposing their values."""
    context = _runtime_context(tmp_path)
    context.config.administrators = [context.requester_id]
    context.config.agents["helper"] = AgentConfig(display_name="Helper", tools=["shell"], memory_backend="file")
    storage = resolve_agent_storage(
        "helper",
        context.config,
        context.runtime_paths,
        build_execution_identity_from_runtime_context(context),
    )
    set_agent_mode(storage.state_root, "helper", context.session_id, "standard", context.requester_id)
    saved = (storage.state_root / "agent_modes.json").read_bytes()

    def unavailable(*_args: object, **_kwargs: object) -> None:
        error = ValueError if failure == "tool_config" else ImportError
        message = "fake-secret-value-in-private-config"
        raise error(message)

    monkeypatch.setattr(
        mode_commands,
        "ensure_tool_registry_loaded" if failure == "registry_import" else "get_tool_by_name",
        unavailable,
    )
    result = mode_commands.handle_mode_command(
        "helper minimal",
        config=context.config,
        runtime_paths=context.runtime_paths,
        target=context.target,
        requester_id=context.requester_id,
        membership_index=context.agent_reply_memberships,
    )
    assert result == "Minimal mode requires an available, valid shell configuration."
    assert "fake-secret" not in result
    assert (storage.state_root / "agent_modes.json").read_bytes() == saved
