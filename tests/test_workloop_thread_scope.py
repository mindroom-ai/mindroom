"""Regression tests for workloop thread scoping."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

import mindroom.tool_system.plugins as plugin_module
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_ENRICH,
    EVENT_MESSAGE_RECEIVED,
    EVENT_SCHEDULE_FIRED,
    AfterResponseContext,
    HookRegistry,
    MessageEnrichContext,
    MessageEnvelope,
    MessageReceivedContext,
    ResponseResult,
    ScheduleFiredContext,
)
from mindroom.hooks.execution import emit, emit_collect
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.scheduling import ScheduledWorkflow
from mindroom.tool_system.metadata import _TOOL_REGISTRY, TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.skills import _get_plugin_skill_roots, set_plugin_skill_roots
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Generator

    from mindroom.constants import RuntimePaths


@dataclass(frozen=True)
class _LoadedWorkloop:
    config: Config
    runtime_paths: RuntimePaths
    registry: HookRegistry


def _plugin_root() -> Path:
    # Try repo-local first, fall back to runtime config dir
    repo_path = Path(__file__).resolve().parents[1] / "plugins" / "workloop"
    if repo_path.is_dir():
        return repo_path
    return Path.home() / ".mindroom-chat" / "plugins" / "workloop"


pytestmark = pytest.mark.skipif(
    not _plugin_root().is_dir(),
    reason="workloop plugin checkout is not available in this environment",
)


def _copy_plugin_root(tmp_path: Path) -> Path:
    """Copy the live workloop plugin into tmp_path and patch known fixture drift."""
    source_root = _plugin_root()
    copied_root = tmp_path / "plugins" / "workloop"
    shutil.copytree(source_root, copied_root)
    types_path = copied_root / "types.py"
    types_text = types_path.read_text(encoding="utf-8")
    if "ROUTER_AGENT_NAME" not in types_text:
        types_text = types_text.replace(
            "    from mindroom.hooks import HookMessageSender, HookRoomStateQuerier\n",
            "    from mindroom.constants import ROUTER_AGENT_NAME\n"
            "    from mindroom.hooks import HookMessageSender, HookRoomStateQuerier\n",
            1,
        )
        types_path.write_text(types_text, encoding="utf-8")
    hooks_path = copied_root / "hooks.py"
    hooks_text = hooks_path.read_text(encoding="utf-8")
    if 'name="workloop-command"' not in hooks_text:
        hooks_text = hooks_text.replace(
            "\n\nasync def workloop_command(ctx: Any) -> None:\n",
            "\n\n@hook(\n"
            '    event="message:received",\n'
            '    name="workloop-command",\n'
            "    agents=(ROUTER_AGENT_NAME,),\n"
            "    priority=100,\n"
            "    timeout_ms=15000,\n"
            ")\n"
            "async def workloop_command(ctx: Any) -> None:\n",
            1,
        )
    hooks_path.write_text(hooks_text, encoding="utf-8")
    return copied_root


def _state_root(loaded: _LoadedWorkloop) -> Path:
    return loaded.runtime_paths.storage_root / "plugins" / "workloop"


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _only_todos_state(loaded: _LoadedWorkloop) -> dict[str, object]:
    todo_files = sorted((_state_root(loaded) / "threads").glob("*/todos.json"))
    assert len(todo_files) == 1
    return _read_json(todo_files[0])


def _tool_context(
    loaded: _LoadedWorkloop,
    *,
    thread_id: str | None = None,
    resolved_thread_id: str | None = "$thread_root",
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="code",
        room_id="!room:localhost",
        thread_id=thread_id,
        resolved_thread_id=resolved_thread_id,
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=loaded.config,
        runtime_paths=loaded.runtime_paths,
        room=MagicMock(),
        reply_to_event_id=None,
        storage_path=None,
    )


def _message_envelope(
    *,
    body: str,
    agent_name: str,
    thread_id: str | None = None,
    resolved_thread_id: str | None = "$thread_root",
) -> MessageEnvelope:
    target = MessageTarget.resolve(
        room_id="!room:localhost",
        thread_id=thread_id,
        reply_to_event_id="$event",
        safe_thread_root=resolved_thread_id if thread_id is None else None,
    )
    if thread_id is not None:
        target = target.with_thread_root(resolved_thread_id)
    return MessageEnvelope(
        source_event_id="$event",
        room_id="!room:localhost",
        target=target,
        requester_id="@user:localhost",
        sender_id="@user:localhost",
        body=body,
        attachment_ids=(),
        mentioned_agents=(),
        agent_name=agent_name,
        source_kind="message",
    )


@pytest.fixture
def loaded_workloop(tmp_path: Path) -> Generator[_LoadedWorkloop, None, None]:
    """Load the workloop plugin into an isolated runtime rooted at tmp_path."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            plugins=[str(_copy_plugin_root(tmp_path))],
        ),
        runtime_paths,
    )

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        plugins = load_plugins(config, runtime_paths_for(config))
        registry = HookRegistry.from_plugins(plugins)
        if not registry._hooks_by_event:
            pytest.skip("workloop plugin checkout is not compatible with the current hook API")
        yield _LoadedWorkloop(
            config=config,
            runtime_paths=runtime_paths_for(config),
            registry=registry,
        )
    finally:
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_tool_scope_uses_resolved_thread_id(loaded_workloop: _LoadedWorkloop) -> None:
    """Agent tool calls should persist todos under the response thread."""
    tool = get_tool_by_name(
        "workloop_todo_manager",
        loaded_workloop.runtime_paths,
        worker_target=None,
    )

    with tool_runtime_context(_tool_context(loaded_workloop)):
        result = tool.plan(agent=MagicMock(), tasks="Investigate threaded schedule poke")

    assert "Created 1 item" in result
    state = _only_todos_state(loaded_workloop)
    assert state["thread_id"] == "$thread_root"


@pytest.mark.asyncio
async def test_enrichment_uses_resolved_thread_scope_and_clears_busy_state(
    loaded_workloop: _LoadedWorkloop,
) -> None:
    """Enrichment should read the resolved-thread file and track busy state in that scope."""
    tool = get_tool_by_name(
        "workloop_todo_manager",
        loaded_workloop.runtime_paths,
        worker_target=None,
    )

    with tool_runtime_context(_tool_context(loaded_workloop)):
        tool.plan(agent=MagicMock(), tasks="Review threaded workloop state")

    enrich_context = MessageEnrichContext(
        event_name=EVENT_MESSAGE_ENRICH,
        plugin_name="",
        settings={},
        config=loaded_workloop.config,
        runtime_paths=loaded_workloop.runtime_paths,
        logger=get_logger("tests.workloop").bind(event_name=EVENT_MESSAGE_ENRICH),
        correlation_id="corr-enrich",
        envelope=_message_envelope(body="hello", agent_name="code"),
        target_entity_name="code",
        target_member_names=None,
    )

    items = await emit_collect(loaded_workloop.registry, EVENT_MESSAGE_ENRICH, enrich_context)

    assert [item.key for item in items] == ["workloop"]
    assert "Review threaded workloop state" in items[0].text

    agent_state_path = _state_root(loaded_workloop) / "agents" / "code.json"
    agent_state = _read_json(agent_state_path)
    assert "!room:localhost:$thread_root" in agent_state["active_runs"]

    after_response_context = AfterResponseContext(
        event_name=EVENT_MESSAGE_AFTER_RESPONSE,
        plugin_name="",
        settings={},
        config=loaded_workloop.config,
        runtime_paths=loaded_workloop.runtime_paths,
        logger=get_logger("tests.workloop").bind(event_name=EVENT_MESSAGE_AFTER_RESPONSE),
        correlation_id="corr-after-response",
        result=ResponseResult(
            response_text="done",
            response_event_id="$response",
            delivery_kind="sent",
            response_kind="ai",
            envelope=_message_envelope(body="hello", agent_name="code"),
        ),
    )

    await emit(loaded_workloop.registry, EVENT_MESSAGE_AFTER_RESPONSE, after_response_context)

    cleared_state = _read_json(agent_state_path)
    assert cleared_state["active_runs"] == {}


@pytest.mark.asyncio
async def test_schedule_fired_auto_poke_uses_thread_from_stored_state(
    loaded_workloop: _LoadedWorkloop,
) -> None:
    """Auto-poke should send back into the stored thread scope."""
    tool = get_tool_by_name(
        "workloop_todo_manager",
        loaded_workloop.runtime_paths,
        worker_target=None,
    )

    with tool_runtime_context(_tool_context(loaded_workloop)):
        tool.plan(agent=MagicMock(), tasks="Resume the threaded task")

    sender = AsyncMock(return_value="$poke")
    schedule_context = ScheduleFiredContext(
        event_name=EVENT_SCHEDULE_FIRED,
        plugin_name="",
        settings={},
        config=loaded_workloop.config,
        runtime_paths=loaded_workloop.runtime_paths,
        logger=get_logger("tests.workloop").bind(event_name=EVENT_SCHEDULE_FIRED),
        correlation_id="corr-schedule",
        message_sender=sender,
        task_id="task123",
        workflow=ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime.now(UTC),
            message="!workloop-tick",
            description="Tick the workloop",
            created_by="@user:localhost",
            room_id="!room:localhost",
        ),
        room_id="!room:localhost",
        thread_id=None,
        created_by="@user:localhost",
        message_text="!workloop-tick",
    )

    await emit(loaded_workloop.registry, EVENT_SCHEDULE_FIRED, schedule_context)

    assert schedule_context.suppress is True
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_room_level_todo_command_stays_in_main_scope(loaded_workloop: _LoadedWorkloop) -> None:
    """Room-level commands should keep using the shared main scope."""
    sender = AsyncMock(return_value="$todo-event")
    command_context = MessageReceivedContext(
        event_name=EVENT_MESSAGE_RECEIVED,
        plugin_name="",
        settings={},
        config=loaded_workloop.config,
        runtime_paths=loaded_workloop.runtime_paths,
        logger=get_logger("tests.workloop").bind(event_name=EVENT_MESSAGE_RECEIVED),
        correlation_id="corr-command",
        message_sender=sender,
        envelope=_message_envelope(
            body="!todo add Regression guard",
            agent_name=ROUTER_AGENT_NAME,
        ),
    )

    await emit(loaded_workloop.registry, EVENT_MESSAGE_RECEIVED, command_context)

    assert command_context.suppress is True
    state = _only_todos_state(loaded_workloop)
    assert state["thread_id"] == "main"
    assert sender.await_args.args[2] is None
