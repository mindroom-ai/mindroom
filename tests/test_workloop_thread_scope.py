"""Regression tests for workloop thread scoping."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mindroom.tool_system.plugin_imports as plugin_module
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.delivery_gateway import (
    DeliveryGateway,
    DeliveryGatewayDeps,
    FinalDeliveryRequest,
    ResponseHookService,
)
from mindroom.hooks import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_CANCELLED,
    EVENT_MESSAGE_ENRICH,
    EVENT_MESSAGE_RECEIVED,
    EVENT_SCHEDULE_FIRED,
    AfterResponseContext,
    CancelledResponseContext,
    HookRegistry,
    MessageEnrichContext,
    MessageEnvelope,
    MessageReceivedContext,
    ResponseResult,
    ScheduleFiredContext,
    hook,
)
from mindroom.hooks.context import CancelledResponseInfo, HookContextSupport
from mindroom.hooks.execution import emit, emit_collect
from mindroom.hooks.registry import HookRegistryState
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.scheduling import ScheduledWorkflow
from mindroom.tool_system.metadata import _TOOL_REGISTRY, TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.skills import _get_plugin_skill_roots, set_plugin_skill_roots
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_cache_mock,
    make_event_cache_mock,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import ModuleType

    from mindroom.constants import RuntimePaths


@dataclass(frozen=True)
class _LoadedWorkloop:
    config: Config
    runtime_paths: RuntimePaths
    registry: HookRegistry
    poke_module: ModuleType


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


def _plugin(name: str, callbacks: list[object]) -> object:
    return type(
        "PluginStub",
        (),
        {
            "name": name,
            "discovered_hooks": tuple(callbacks),
            "entry_config": PluginEntryConfig(path=f"./plugins/{name}"),
            "plugin_order": 0,
        },
    )()


def _copy_plugin_root(tmp_path: Path) -> Path:
    """Copy the live workloop plugin into tmp_path and patch known fixture drift."""
    copied_root = tmp_path / "plugins" / "workloop"
    shutil.copytree(_plugin_root(), copied_root)
    (tmp_path / "plugins" / "__init__.py").write_text("", encoding="utf-8")
    (copied_root / "__init__.py").write_text("", encoding="utf-8")
    package_prefix = "plugins.workloop"
    for module_path in copied_root.glob("*.py"):
        module_text = module_path.read_text(encoding="utf-8")
        module_text = module_text.replace(
            "from . import commands, formatting, poke, state, todos, types as workloop_types",
            f"from {package_prefix} import commands, formatting, poke, state, todos, types as workloop_types",
        )
        module_text = module_text.replace("from .formatting import ", f"from {package_prefix}.formatting import ")
        module_text = module_text.replace("from .poke import ", f"from {package_prefix}.poke import ")
        module_text = module_text.replace("from .state import ", f"from {package_prefix}.state import ")
        module_text = module_text.replace("from .todos import ", f"from {package_prefix}.todos import ")
        module_text = module_text.replace("from .types import ", f"from {package_prefix}.types import ")
        module_path.write_text(module_text, encoding="utf-8")
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


def _todo_state_path(loaded: _LoadedWorkloop, *, room_id: str, thread_id: str) -> Path:
    canonical_path = _state_root(loaded) / "rooms" / room_id / "threads" / thread_id / "todos.json"
    if canonical_path.exists():
        return canonical_path
    legacy_key = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]", "_", f"{room_id}_{thread_id}")).strip("_")
    return _state_root(loaded) / "threads" / legacy_key / "todos.json"


def _todo_state(loaded: _LoadedWorkloop, *, room_id: str, thread_id: str) -> dict[str, object]:
    return _read_json(_todo_state_path(loaded, room_id=room_id, thread_id=thread_id))


def _todo_titles(state: dict[str, object]) -> list[str]:
    tasks = state.get("tasks")
    if isinstance(tasks, list):
        return [task for task in tasks if isinstance(task, str)]
    items = state.get("items")
    if not isinstance(items, list):
        return []
    titles: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if isinstance(title, str):
            titles.append(title)
    return titles


def _registry_callbacks(registry: HookRegistry) -> list[object]:
    callbacks: list[object] = []
    seen_callbacks: set[object] = set()
    for hooks in registry._hooks_by_event.values():
        for registered_hook in hooks:
            if registered_hook.callback in seen_callbacks:
                continue
            seen_callbacks.add(registered_hook.callback)
            callbacks.append(registered_hook.callback)
    return callbacks


def _tool_context(
    loaded: _LoadedWorkloop,
    *,
    room_id: str = "!room:localhost",
    thread_id: str | None = None,
    resolved_thread_id: str | None = "$thread_root",
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="code",
        room_id=room_id,
        thread_id=thread_id,
        resolved_thread_id=resolved_thread_id,
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=loaded.config,
        runtime_paths=loaded.runtime_paths,
        event_cache=make_event_cache_mock(),
        conversation_cache=make_conversation_cache_mock(),
        room=MagicMock(),
        reply_to_event_id=None,
        storage_path=None,
    )


def _message_envelope(
    *,
    body: str,
    agent_name: str,
    room_id: str = "!room:localhost",
    thread_id: str | None = None,
    resolved_thread_id: str | None = "$thread_root",
    room_mode: bool = False,
) -> MessageEnvelope:
    target = MessageTarget.resolve(
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id="$event",
        thread_start_root_event_id=resolved_thread_id if thread_id is None else None,
        room_mode=room_mode,
    )
    if thread_id is not None:
        target = target.with_thread_root(resolved_thread_id)
    return MessageEnvelope(
        source_event_id="$event",
        room_id=room_id,
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
    plugin_root = _copy_plugin_root(tmp_path)
    sys.path.insert(0, str(tmp_path))
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            plugins=[str(plugin_root)],
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
        poke_module = importlib.import_module("plugins.workloop.poke")
        yield _LoadedWorkloop(
            config=config,
            runtime_paths=runtime_paths_for(config),
            registry=registry,
            poke_module=poke_module,
        )
    finally:
        for module_name in list(sys.modules):
            if module_name == "plugins.workloop" or module_name.startswith("plugins.workloop."):
                sys.modules.pop(module_name, None)
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)
        if sys.path and sys.path[0] == str(tmp_path):
            sys.path.pop(0)


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
    state = _todo_state(loaded_workloop, room_id="!room:localhost", thread_id="$thread_root")
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
    """Room-level commands should keep using the room's main scope."""
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
    state = _todo_state(loaded_workloop, room_id="!room:localhost", thread_id="main")
    assert state["thread_id"] == "main"
    assert sender.await_args.args[2] is None


@pytest.mark.asyncio
async def test_room_level_todos_are_isolated_per_room(loaded_workloop: _LoadedWorkloop) -> None:
    """Room-level main scopes must stay isolated between rooms."""
    room_a = "!room-a:localhost"
    room_b = "!room-b:localhost"
    sender = AsyncMock(return_value="$todo-event")
    tool = get_tool_by_name(
        "workloop_todo_manager",
        loaded_workloop.runtime_paths,
        worker_target=None,
    )

    room_a_context = MessageReceivedContext(
        event_name=EVENT_MESSAGE_RECEIVED,
        plugin_name="",
        settings={},
        config=loaded_workloop.config,
        runtime_paths=loaded_workloop.runtime_paths,
        logger=get_logger("tests.workloop").bind(event_name=EVENT_MESSAGE_RECEIVED),
        correlation_id="corr-command-room-a",
        message_sender=sender,
        envelope=_message_envelope(
            body="!todo add Room A regression guard",
            agent_name=ROUTER_AGENT_NAME,
            room_id=room_a,
            resolved_thread_id=None,
            room_mode=True,
        ),
    )
    await emit(loaded_workloop.registry, EVENT_MESSAGE_RECEIVED, room_a_context)

    with tool_runtime_context(
        _tool_context(loaded_workloop, room_id=room_b, resolved_thread_id=None),
    ):
        tool.plan(agent=MagicMock(), tasks="Room B regression guard")

    room_a_state = _todo_state(loaded_workloop, room_id=room_a, thread_id="main")
    room_b_state = _todo_state(loaded_workloop, room_id=room_b, thread_id="main")
    assert _todo_titles(room_a_state) == ["Room A regression guard"]
    assert _todo_titles(room_b_state) == ["Room B regression guard"]

    room_a_items = await emit_collect(
        loaded_workloop.registry,
        EVENT_MESSAGE_ENRICH,
        MessageEnrichContext(
            event_name=EVENT_MESSAGE_ENRICH,
            plugin_name="",
            settings={},
            config=loaded_workloop.config,
            runtime_paths=loaded_workloop.runtime_paths,
            logger=get_logger("tests.workloop").bind(event_name=EVENT_MESSAGE_ENRICH),
            correlation_id="corr-enrich-room-a",
            envelope=_message_envelope(
                body="status",
                agent_name="code",
                room_id=room_a,
                resolved_thread_id=None,
                room_mode=True,
            ),
            target_entity_name="code",
            target_member_names=None,
        ),
    )
    room_b_items = await emit_collect(
        loaded_workloop.registry,
        EVENT_MESSAGE_ENRICH,
        MessageEnrichContext(
            event_name=EVENT_MESSAGE_ENRICH,
            plugin_name="",
            settings={},
            config=loaded_workloop.config,
            runtime_paths=loaded_workloop.runtime_paths,
            logger=get_logger("tests.workloop").bind(event_name=EVENT_MESSAGE_ENRICH),
            correlation_id="corr-enrich-room-b",
            envelope=_message_envelope(
                body="status",
                agent_name="code",
                room_id=room_b,
                resolved_thread_id=None,
                room_mode=True,
            ),
            target_entity_name="code",
            target_member_names=None,
        ),
    )

    assert len(room_a_items) == 1
    assert "Room A regression guard" in room_a_items[0].text
    assert "Room B regression guard" not in room_a_items[0].text
    assert len(room_b_items) == 1
    assert "Room B regression guard" in room_b_items[0].text
    assert "Room A regression guard" not in room_b_items[0].text


@pytest.mark.asyncio
async def test_cancelled_response_clears_active_run_without_updating_last_response(
    loaded_workloop: _LoadedWorkloop,
) -> None:
    """message:cancelled should clear the active run but NOT update last_response_at."""
    tool = get_tool_by_name(
        "workloop_todo_manager",
        loaded_workloop.runtime_paths,
        worker_target=None,
    )

    with tool_runtime_context(_tool_context(loaded_workloop)):
        tool.plan(agent=MagicMock(), tasks="Task that will be cancelled")

    # Trigger enrichment to mark agent as busy
    enrich_context = MessageEnrichContext(
        event_name=EVENT_MESSAGE_ENRICH,
        plugin_name="",
        settings={},
        config=loaded_workloop.config,
        runtime_paths=loaded_workloop.runtime_paths,
        logger=get_logger("tests.workloop").bind(event_name=EVENT_MESSAGE_ENRICH),
        correlation_id="corr-enrich-cancel",
        envelope=_message_envelope(body="hello", agent_name="code"),
        target_entity_name="code",
        target_member_names=None,
    )

    await emit_collect(loaded_workloop.registry, EVENT_MESSAGE_ENRICH, enrich_context)

    agent_state_path = _state_root(loaded_workloop) / "agents" / "code.json"
    agent_state = _read_json(agent_state_path)
    assert "!room:localhost:$thread_root" in agent_state["active_runs"]

    # Record any existing last_response_at
    last_response_before = agent_state.get("last_response_at")

    # Fire message:cancelled — should clear active_run but NOT set last_response_at
    cancelled_context = CancelledResponseContext(
        event_name=EVENT_MESSAGE_CANCELLED,
        plugin_name="",
        settings={},
        config=loaded_workloop.config,
        runtime_paths=loaded_workloop.runtime_paths,
        logger=get_logger("tests.workloop").bind(event_name=EVENT_MESSAGE_CANCELLED),
        correlation_id="corr-cancelled",
        info=CancelledResponseInfo(
            envelope=_message_envelope(body="hello", agent_name="code"),
            visible_response_event_id="$partial",
            response_kind="ai",
        ),
    )

    await emit(loaded_workloop.registry, EVENT_MESSAGE_CANCELLED, cancelled_context)

    cleared_state = _read_json(agent_state_path)
    assert cleared_state["active_runs"] == {}, "active_run should be cleared on cancellation"
    assert cleared_state.get("last_response_at") == last_response_before, (
        "last_response_at must NOT be updated on cancellation"
    )


@pytest.mark.asyncio
async def test_cancelled_then_poke_scan_can_repoke(
    loaded_workloop: _LoadedWorkloop,
) -> None:
    """After cancellation clears the active run, the next poke scan should be able to re-poke."""
    tool = get_tool_by_name(
        "workloop_todo_manager",
        loaded_workloop.runtime_paths,
        worker_target=None,
    )

    with tool_runtime_context(_tool_context(loaded_workloop)):
        tool.plan(agent=MagicMock(), tasks="Re-pokeable task")

    # Enrich to mark busy
    enrich_context = MessageEnrichContext(
        event_name=EVENT_MESSAGE_ENRICH,
        plugin_name="",
        settings={},
        config=loaded_workloop.config,
        runtime_paths=loaded_workloop.runtime_paths,
        logger=get_logger("tests.workloop").bind(event_name=EVENT_MESSAGE_ENRICH),
        correlation_id="corr-enrich-repoke",
        envelope=_message_envelope(body="do work", agent_name="code"),
        target_entity_name="code",
        target_member_names=None,
    )
    await emit_collect(loaded_workloop.registry, EVENT_MESSAGE_ENRICH, enrich_context)

    # Fire cancellation
    cancelled_context = CancelledResponseContext(
        event_name=EVENT_MESSAGE_CANCELLED,
        plugin_name="",
        settings={},
        config=loaded_workloop.config,
        runtime_paths=loaded_workloop.runtime_paths,
        logger=get_logger("tests.workloop").bind(event_name=EVENT_MESSAGE_CANCELLED),
        correlation_id="corr-cancelled-repoke",
        info=CancelledResponseInfo(
            envelope=_message_envelope(body="do work", agent_name="code"),
        ),
    )
    await emit(loaded_workloop.registry, EVENT_MESSAGE_CANCELLED, cancelled_context)

    # Verify agent state is cleared
    agent_state_path = _state_root(loaded_workloop) / "agents" / "code.json"
    cleared_state = _read_json(agent_state_path)
    assert cleared_state["active_runs"] == {}

    # Now a poke scan should consider the agent idle and pokeable
    # (The _should_poke_agent check won't block on active_runs since they're cleared)
    now = datetime.now(UTC)
    can_poke = loaded_workloop.poke_module._should_poke_agent(
        _state_root(loaded_workloop),
        "code",
        now,
        cooldown=0,
        grace=0,
        stale_busy=600,
        scope_key="!room:localhost:$thread_root",
        min_idle=0,
    )
    assert can_poke, "agent should be pokeable after cancellation clears active_run"


@pytest.mark.asyncio
async def test_late_after_response_cancellation_still_runs_workloop_cleanup(
    loaded_workloop: _LoadedWorkloop,
) -> None:
    """Late cancellation during after_response should still clear busy state as delivered."""
    tool = get_tool_by_name(
        "workloop_todo_manager",
        loaded_workloop.runtime_paths,
        worker_target=None,
    )
    response_envelope = _message_envelope(body="hello", agent_name="code")

    with tool_runtime_context(_tool_context(loaded_workloop)):
        tool.plan(agent=MagicMock(), tasks="Task that must clear after late cancellation")

    await emit_collect(
        loaded_workloop.registry,
        EVENT_MESSAGE_ENRICH,
        MessageEnrichContext(
            event_name=EVENT_MESSAGE_ENRICH,
            plugin_name="",
            settings={},
            config=loaded_workloop.config,
            runtime_paths=loaded_workloop.runtime_paths,
            logger=get_logger("tests.workloop").bind(event_name=EVENT_MESSAGE_ENRICH),
            correlation_id="corr-enrich-late-cancel",
            envelope=response_envelope,
            target_entity_name="code",
            target_member_names=None,
        ),
    )

    agent_state_path = _state_root(loaded_workloop) / "agents" / "code.json"
    agent_state = _read_json(agent_state_path)
    assert "!room:localhost:$thread_root" in agent_state["active_runs"]
    assert agent_state.get("last_response_at") is None

    after_started = asyncio.Event()

    @hook(EVENT_MESSAGE_AFTER_RESPONSE, priority=50)
    async def slow_after_response(ctx: AfterResponseContext) -> None:
        del ctx
        after_started.set()
        await asyncio.Event().wait()

    registry = HookRegistry.from_plugins(
        [
            _plugin("slow-after", [slow_after_response]),
            _plugin("workloop", _registry_callbacks(loaded_workloop.registry)),
        ],
    )
    hook_context = HookContextSupport(
        runtime=type("RT", (), {"client": None, "orchestrator": None, "config": loaded_workloop.config})(),
        logger=get_logger("tests.workloop.delivery"),
        runtime_paths=loaded_workloop.runtime_paths,
        agent_name="code",
        hook_registry_state=HookRegistryState(registry),
        hook_send_message=AsyncMock(),
    )
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=hook_context.runtime,
            runtime_paths=loaded_workloop.runtime_paths,
            agent_name="code",
            logger=get_logger("tests.workloop.delivery"),
            redact_message_event=AsyncMock(return_value=True),
            sender_domain="localhost",
            resolver=MagicMock(),
            response_hooks=ResponseHookService(hook_context=hook_context),
        ),
    )

    parsed = MagicMock()
    parsed.formatted_text = "visible response"
    parsed.option_map = None
    parsed.options_list = None

    delivery_result = None

    async def deliver_response() -> None:
        nonlocal delivery_result
        delivery_result = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=response_envelope.target,
                existing_event_id=None,
                response_text="visible response",
                response_kind="ai",
                response_envelope=response_envelope,
                correlation_id="corr-late-workloop-cleanup",
                tool_trace=None,
                extra_content=None,
            ),
        )

    with (
        patch("mindroom.delivery_gateway.interactive.parse_and_format_interactive", return_value=parsed),
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$response")),
    ):
        task = asyncio.create_task(deliver_response())
        await asyncio.wait_for(after_started.wait(), timeout=1)
        task.cancel()
        await task

    assert delivery_result is not None
    assert delivery_result.event_id == "$response"
    assert delivery_result.delivery_kind == "sent"

    cleared_state = _read_json(agent_state_path)
    assert cleared_state["active_runs"] == {}
    assert isinstance(cleared_state.get("last_response_at"), str)
