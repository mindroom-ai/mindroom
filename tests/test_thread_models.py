"""Tests for per-thread model overrides (!model command and thread_model tool)."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

import mindroom.tools  # noqa: F401
from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.commands.model_commands import handle_model_command
from mindroom.commands.parsing import CommandType, command_parser, get_command_help
from mindroom.config.agent import AgentConfig
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.custom_tools.thread_model import ThreadModelTools
from mindroom.llm.runs import RunStatus
from mindroom.message_target import MessageTarget
from mindroom.room_model_overrides import set_room_model_override
from mindroom.thread_models import (
    _get_thread_model_override,
    _store_path,
    clear_thread_model_override,
    set_thread_model_override,
)
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.tool_function import ToolOutcome
from mindroom.turn_model import ModelScope, resolve_turn_model
from tests.agent_tool_helpers import toolkit_offers
from tests.authorization_helpers import (
    isolated_membership_index,
    make_test_tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.test_model_selection_commands import OTHER, resolved_thread_models, restricted_config
from tests.test_model_selection_scope import ROOM, USER

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.tool_function import ToolFunction

THREAD_ID = "$thread-root:localhost"
ROOM_ID = "!room:localhost"


def _stored_model(runtime_paths: RuntimePaths, thread_id: str | None) -> str | None:
    record = _get_thread_model_override(runtime_paths, thread_id)
    return None if record is None else record.get("model:test_agent")


def _config_with_models(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=32_000),
            },
        ),
        test_runtime_paths(tmp_path),
    )


def test_store_roundtrip(tmp_path: Path) -> None:
    """Set, get, and clear should persist one override per thread root."""
    config = _config_with_models(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)

    assert _stored_model(runtime_paths, THREAD_ID) is None
    assert _stored_model(runtime_paths, None) is None

    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )
    assert _stored_model(runtime_paths, THREAD_ID) == "large"
    assert _stored_model(runtime_paths, "$other:localhost") is None

    assert clear_thread_model_override(runtime_paths, THREAD_ID, entity_names=("test_agent",), config=config) is True
    assert _stored_model(runtime_paths, THREAD_ID) is None
    assert clear_thread_model_override(runtime_paths, THREAD_ID, entity_names=("test_agent",), config=config) is False


def test_store_ignores_corrupt_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A corrupt store file should read as empty and be replaced on write."""
    config = _config_with_models(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")

    parse_calls = 0
    real_loads = json.loads

    def tracked_loads(payload: str) -> object:
        nonlocal parse_calls
        parse_calls += 1
        return real_loads(payload)

    monkeypatch.setattr("mindroom.durable_write.json.loads", tracked_loads)

    assert _stored_model(runtime_paths, THREAD_ID) is None
    assert _stored_model(runtime_paths, THREAD_ID) is None
    assert parse_calls == 1

    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )
    assert _stored_model(runtime_paths, THREAD_ID) == "large"
    assert parse_calls == 2


def test_store_prunes_oldest_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The store should keep only the newest overrides past the cap."""
    config = _config_with_models(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    monkeypatch.setattr("mindroom.thread_models._MAX_TRACKED_THREADS", 3)

    for index in range(5):
        set_thread_model_override(
            runtime_paths,
            thread_id=f"$thread-{index}:localhost",
            model_name="large",
            entity_names=("test_agent",),
            config=config,
        )

    stored = json.loads(_store_path(runtime_paths).read_text(encoding="utf-8"))
    assert len(stored) == 3
    assert _stored_model(runtime_paths, "$thread-4:localhost") == "large"


def test_store_invalidates_cached_records_when_mtime_changes(tmp_path: Path) -> None:
    """An external file replacement should invalidate the parsed-record cache."""
    config = _config_with_models(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )
    assert _stored_model(runtime_paths, THREAD_ID) == "large"

    path.write_text(
        json.dumps({THREAD_ID: {"model:test_agent": "default", "set_at": "2026-07-09T00:00:00+00:00"}}),
        encoding="utf-8",
    )
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    assert _stored_model(runtime_paths, THREAD_ID) == "default"


def test_resolve_turn_model_prefers_thread_override(tmp_path: Path) -> None:
    """A thread override should beat the agent's authored model."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )

    runtime_model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
        None,
    )

    assert runtime_model.model_name == "large"
    assert runtime_model.context_window == 32_000


def test_resolve_turn_model_thread_override_beats_room_override(
    tmp_path: Path,
) -> None:
    """A thread override should beat the room_models override."""
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            room_models={ROOM_ID: "default"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=32_000),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )

    runtime_model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
        None,
    )

    assert runtime_model.model_name == "large"


def test_resolve_turn_model_active_model_beats_thread_override(tmp_path: Path) -> None:
    """An explicit active model should beat the thread override."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )

    runtime_model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID, explicit="default"),
        None,
    )

    assert runtime_model.model_name == "default"


def test_resolve_turn_model_ignores_stale_thread_override(tmp_path: Path) -> None:
    """An override naming a removed model should fall back to authored config."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="removed-model",
        entity_names=("test_agent",),
        config=config,
    )

    runtime_model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
        None,
    )

    assert runtime_model.model_name == "default"


def test_resolve_turn_model_without_thread_id_keeps_authored_model(tmp_path: Path) -> None:
    """Resolution without thread context should ignore thread overrides."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )

    runtime_model = resolve_turn_model(config, runtime_paths, ModelScope(entity="test_agent", room_id=ROOM_ID), None)

    assert runtime_model.model_name == "default"


def test_model_command_parsing() -> None:
    """The parser should recognize !model with and without arguments."""
    command = command_parser.parse("!model")
    assert command is not None
    assert command.type == CommandType.MODEL
    assert command.args["args_text"] == ""

    command = command_parser.parse("!model large")
    assert command is not None
    assert command.type == CommandType.MODEL
    assert command.args["args_text"] == "large"

    command = command_parser.parse("!model reset")
    assert command is not None
    assert command.type == CommandType.MODEL
    assert command.args["args_text"] == "reset"


def test_model_command_set_and_reset(tmp_path: Path) -> None:
    """!model <name> should persist and !model reset should clear the override."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    response = handle_model_command(
        "large",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
    )
    assert "✅" in response
    assert "`large`" in response
    assert "room-level model selection" in response
    assert _stored_model(runtime_paths, THREAD_ID) == "large"

    set_room_model_override(
        runtime_paths,
        room_id=ROOM_ID,
        model_name="large",
    )

    response = handle_model_command(
        "reset",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
    )
    assert "✅" in response
    assert "room-level model selection" in response
    assert _stored_model(runtime_paths, THREAD_ID) is None
    assert (
        resolve_turn_model(
            config,
            runtime_paths,
            ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
            None,
        ).model_name
        == "large"
    )

    response = handle_model_command(
        "reset",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
    )
    assert "no model override" in response


def test_model_command_show(tmp_path: Path) -> None:
    """Bare !model should report the current override and available models."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    response = handle_model_command(
        "",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
    )
    assert "No thread model override" in response
    assert "room-level model selection" in response
    assert "`default`" in response
    assert "`large`" in response

    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )
    response = handle_model_command(
        "",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
    )
    assert "- `large` (openai large-model) for `test_agent`" in response


@pytest.mark.parametrize("write_args", ["reset", "default"])
def test_model_command_ignores_overrides_of_removed_entities(tmp_path: Path, write_args: str) -> None:
    """A removed agent's thread override is not reported, and the next reset or selection removes it.

    The agent added again under the same name then starts without the old override.
    """
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    readded_config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(display_name="Test Agent", model="default"),
                "removed_agent": AgentConfig(display_name="Removed Agent", model="default"),
            },
            models=config.models,
        ),
        runtime_paths,
    )
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent", "removed_agent"),
        config=readded_config,
    )

    def model_command(args_text: str, command_config: Config = config) -> str:
        return handle_model_command(
            args_text,
            config=command_config,
            runtime_paths=runtime_paths,
            membership_index=isolated_membership_index(),
            room_id=ROOM_ID,
            thread_id=THREAD_ID,
            requester_user_id="@user:localhost",
        )

    assert "`removed_agent`" not in model_command("")
    assert "✅" in model_command(write_args)
    assert "`removed_agent`" not in model_command("", readded_config)
    runtime_model = resolve_turn_model(
        readded_config,
        runtime_paths,
        ModelScope(entity="removed_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
        None,
    )
    assert runtime_model.model_name == "default"


def test_model_help_describes_room_level_fallback() -> None:
    """Reset help must describe the room-level fallback that actually runs."""
    help_text = get_command_help("model")

    assert "room-level model selection" in help_text


def test_model_command_list_alias_shows_models(tmp_path: Path) -> None:
    """!model list must show the override state, even outside a thread."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    for thread_id in (THREAD_ID, None):
        response = handle_model_command(
            "list",
            config=config,
            runtime_paths=runtime_paths,
            membership_index=isolated_membership_index(),
            room_id=ROOM_ID,
            thread_id=thread_id,
            requester_user_id="@user:localhost",
        )
        assert "No thread model override" in response
        assert "`default`" in response
        assert "`large`" in response


def test_model_command_rejects_unknown_model(tmp_path: Path) -> None:
    """!model should reject names missing from config.models."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    response = handle_model_command(
        "nonexistent",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
    )
    assert "Unknown model" in response
    assert _stored_model(runtime_paths, THREAD_ID) is None


def test_model_command_requires_thread_for_set(tmp_path: Path) -> None:
    """Setting an override outside a thread should fail clearly."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    response = handle_model_command(
        "large",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=None,
        requester_user_id="@user:localhost",
    )
    assert "only work inside a thread" in response
    assert _stored_model(runtime_paths, THREAD_ID) is None


def test_model_command_default_sets_model_instead_of_resetting(tmp_path: Path) -> None:
    """A configured model named "default" must be settable, not treated as a reset alias."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    response = handle_model_command(
        "default",
        config=config,
        runtime_paths=runtime_paths,
        membership_index=isolated_membership_index(),
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
    )
    assert "✅" in response
    assert "`default`" in response
    assert _stored_model(runtime_paths, THREAD_ID) == "default"


def test_store_drops_records_with_corrupt_set_at(tmp_path: Path) -> None:
    """Records with a non-string set_at must be dropped so prune sorting cannot fail."""
    config = _config_with_models(tmp_path)
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"$bad:localhost": {"model:test_agent": "large", "set_at": 12345}}),
        encoding="utf-8",
    )

    assert _stored_model(runtime_paths, "$bad:localhost") is None

    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )
    assert _stored_model(runtime_paths, THREAD_ID) == "large"


def _make_tool_context(tmp_path: Path, *, thread_id: str | None = THREAD_ID) -> ToolRuntimeContext:
    config = _config_with_models(tmp_path)
    return make_test_tool_runtime_context(
        agent_name="test_agent",
        target=MessageTarget.resolve(
            room_id=ROOM_ID,
            thread_id=thread_id,
            reply_to_event_id=None,
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=None,
        storage_path=None,
    )


def test_thread_model_tool_registered(tmp_path: Path) -> None:
    """The thread_model tool should be available from the metadata registry."""
    config = _config_with_models(tmp_path)
    toolkit = get_tool_by_name(
        "thread_model",
        runtime_paths_for(config),
        worker_target=None,
        tools=builtin_tool_registry(),
    )

    assert "thread_model" in builtin_tool_registry()
    assert isinstance(toolkit, ThreadModelTools)
    assert "list_models" in toolkit.functions
    assert "list_models" in builtin_tool_registry().entries["thread_model"].function_names


def test_thread_model_switch_may_stop_the_run_but_does_not_stop_every_call() -> None:
    """Only a switch for the current response stops the run, and every switch still runs inside the run that made it."""
    tool = ThreadModelTools()
    function = toolkit_offers(tool, "switch_thread_model")

    assert (function.stop_after_call, function.may_stop_run, function.controls_run) == (False, True, True)
    assert function.parameters["properties"]["model_name"]["type"] == "string"
    assert function.parameters["properties"]["when"]["enum"] == ["after-toolcall", "next-turn"]
    assert function.parameters["required"] == ["model_name"]


def _configured_thread_model_functions(context: ToolRuntimeContext, workspace: Path) -> dict[str, ToolFunction]:
    """Return the thread-model functions as an agent with a workspace is offered them, auto-saving any output."""
    toolkit = get_tool_by_name(
        "thread_model",
        context.runtime_paths,
        worker_target=None,
        tools=builtin_tool_registry(),
        tool_output_workspace_root=workspace,
        tool_output_auto_save_threshold_bytes=1,
    )
    return {function.name: function for function in adapt_toolkit(toolkit, key="thread_model").functions}


def test_a_configured_switch_offers_no_output_path(tmp_path: Path) -> None:
    """A switch's result is for its run, so the model cannot ask to save it to a file, as it can for the other calls."""
    functions = _configured_thread_model_functions(_make_tool_context(tmp_path), tmp_path / "workspace")

    assert OUTPUT_PATH_ARGUMENT not in functions["switch_thread_model"].parameters["properties"]
    assert OUTPUT_PATH_ARGUMENT in functions["get_thread_model"].parameters["properties"]


@pytest.mark.asyncio
async def test_a_configured_switch_for_the_current_response_is_never_auto_saved(tmp_path: Path) -> None:
    """A saved result would answer with a receipt, which no longer stops the run or names the new model."""
    context = _make_tool_context(tmp_path)
    switch = _configured_thread_model_functions(context, tmp_path / "workspace")["switch_thread_model"]

    with tool_runtime_context(context):
        outcome = await execute_tool_call(
            switch,
            {"model_name": "large", "when": "after-toolcall"},
            ToolCallContext(run_id="run", session_id="session", call_id="call-switch", user_id=None),
            middleware=(),
        )

    assert outcome.stop_after_call is True
    assert json.loads(outcome.content)["model"] == "large"
    assert not (tmp_path / "workspace").exists()


@pytest.mark.asyncio
async def test_thread_model_tool_requires_runtime_context() -> None:
    """Tool calls should fail clearly outside Matrix runtime context."""
    payload = json.loads(await ThreadModelTools().switch_thread_model("large"))

    assert payload["status"] == "error"
    assert payload["tool"] == "thread_model"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_thread_model_tool_lists_configured_models_without_thread(tmp_path: Path) -> None:
    """Removing explicit discovery would leave agents unable to choose a configured alias."""
    context = _make_tool_context(tmp_path, thread_id=None)

    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().list_models())

    assert payload == {
        "action": "list",
        "models": [
            {"model_id": "default-model", "name": "default", "provider": "openai"},
            {"model_id": "large-model", "name": "large", "provider": "openai"},
        ],
        "status": "ok",
        "tool": "thread_model",
    }


@pytest.mark.asyncio
async def test_thread_model_tool_requires_thread(tmp_path: Path) -> None:
    """Switching should fail clearly without an active thread."""
    context = _make_tool_context(tmp_path, thread_id=None)
    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().switch_thread_model("large"))

    assert payload["status"] == "error"
    assert "thread" in payload["message"]


@pytest.mark.asyncio
async def test_thread_model_tool_switch_get_and_reset(tmp_path: Path) -> None:
    """Switch, get, and reset should round-trip through the store."""
    context = _make_tool_context(tmp_path)
    tool = ThreadModelTools()

    with tool_runtime_context(context):
        switch_payload = json.loads(await tool.switch_thread_model("large"))
        get_payload = json.loads(await tool.get_thread_model())
        reset_payload = json.loads(await tool.reset_thread_model())

    assert switch_payload["status"] == "ok"
    assert switch_payload["model"] == "large"
    assert switch_payload["model_id"] == "large-model"
    assert get_payload["overrides"] == {"router": "large", "test_agent": "large"}
    assert reset_payload["status"] == "ok"
    assert reset_payload["cleared"] is True
    assert _stored_model(context.runtime_paths, THREAD_ID) is None


@pytest.mark.asyncio
async def test_thread_model_tool_can_switch_after_current_tool_call(tmp_path: Path) -> None:
    """Ignoring the timing argument would leave the current response on the old model."""
    context = _make_tool_context(tmp_path)

    with tool_runtime_context(context):
        outcome = await ThreadModelTools().switch_thread_model("large", when="after-toolcall")

    # The run stops after the call, so the turn continues the response on the new model.
    assert isinstance(outcome, ToolOutcome)
    assert outcome.stop_after_call is True
    payload = json.loads(outcome.content)
    assert payload["status"] == "ok"
    assert payload["model"] == "large"
    assert payload["when"] == "after-toolcall"
    assert payload["note"] == "The current response will continue with the new model after this tool call."
    assert _stored_model(context.runtime_paths, THREAD_ID) == "large"


@pytest.mark.asyncio
async def test_over_budget_switch_continues_this_response_on_the_fallback(tmp_path: Path) -> None:
    """A mid-response switch to a priced model must not bypass the requester's budget."""
    context = _make_tool_context(tmp_path)
    context.config.models["large"].pricing = ModelPricing(input=5, output=30)
    context.config.models["cheap"] = ModelConfig(provider="openai", id="cheap-model")
    context.config.budgets = BudgetsConfig(fallback_model="cheap", monthly_limit_usd=0)

    with tool_runtime_context(context):
        outcome = await ThreadModelTools().switch_thread_model("large", when="after-toolcall")

    assert isinstance(outcome, ToolOutcome)
    assert outcome.stop_after_call is True
    payload = json.loads(outcome.content)
    assert payload["status"] == "ok"
    assert payload["model"] == "cheap"
    assert payload["requested_model"] == "large"
    assert "budget" in payload["note"]
    assert _stored_model(context.runtime_paths, THREAD_ID) == "large"


@pytest.mark.asyncio
async def test_over_budget_next_turn_switch_says_replies_use_the_fallback(tmp_path: Path) -> None:
    """The agent should not promise the requested model while the requester is over budget."""
    context = _make_tool_context(tmp_path)
    context.config.models["large"].pricing = ModelPricing(input=5, output=30)
    context.config.models["cheap"] = ModelConfig(provider="openai", id="cheap-model")
    context.config.budgets = BudgetsConfig(fallback_model="cheap", monthly_limit_usd=0)

    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().switch_thread_model("large"))

    assert payload["model"] == "large"
    assert "budget" in payload["note"]
    assert "`cheap`" in payload["note"]


@pytest.mark.asyncio
async def test_thread_model_tool_rejects_unknown_switch_timing(tmp_path: Path) -> None:
    """Accepting an unknown timing value could persist a change with undefined execution semantics."""
    context = _make_tool_context(tmp_path)
    entrypoint = toolkit_offers(ThreadModelTools(), "switch_thread_model").entrypoint

    with tool_runtime_context(context), pytest.raises(ValidationError):
        await entrypoint(model_name="large", when="later")

    assert _stored_model(context.runtime_paths, THREAD_ID) is None


@pytest.mark.asyncio
async def test_thread_model_tool_rejects_unknown_model(tmp_path: Path) -> None:
    """Switching should reject names missing from config.models."""
    context = _make_tool_context(tmp_path)

    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().switch_thread_model("nonexistent"))

    assert payload["status"] == "error"
    assert payload["available_models"] == ["default", "large"]
    assert _stored_model(context.runtime_paths, THREAD_ID) is None


@pytest.mark.asyncio
async def test_thread_model_tool_reports_stale_override_as_inactive(tmp_path: Path) -> None:
    """An override naming a removed model must not be reported as active."""
    context = _make_tool_context(tmp_path)
    set_thread_model_override(
        context.runtime_paths,
        thread_id=THREAD_ID,
        model_name="removed-model",
        entity_names=("test_agent",),
        config=context.config,
    )
    set_room_model_override(
        context.runtime_paths,
        room_id=ROOM_ID,
        model_name="large",
    )

    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().get_thread_model())

    assert payload["status"] == "ok"
    assert payload["overrides"] == {}
    assert payload["stale_overrides"] == {"test_agent": "removed-model"}
    assert "no longer configured" in payload["note"]
    assert "room-level model selection" in payload["note"]
    assert (
        resolve_turn_model(
            context.config,
            context.runtime_paths,
            ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
            None,
        ).model_name
        == "large"
    )


def _restricted_tool_context(tmp_path: Path, requester_id: str) -> ToolRuntimeContext:
    config = restricted_config(tmp_path)
    return make_test_tool_runtime_context(
        agent_name="helper",
        target=MessageTarget.resolve(room_id=ROOM, thread_id="$root", reply_to_event_id=None),
        requester_id=requester_id,
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=None,
        storage_path=None,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_thread_model_tool_switch_leaves_entities_the_requester_cannot_address(tmp_path: Path) -> None:
    """A tool switch for a requester excluded by an agent's access must leave that agent on its own model."""
    context = _restricted_tool_context(tmp_path, USER)

    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().switch_thread_model("expensive"))

    assert payload["status"] == "ok"
    assert resolved_thread_models(context.config, context.runtime_paths) == {
        "helper": "expensive",
        "restricted": "default",
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_thread_model_tool_reports_and_resets_only_the_entities_each_override_governs(tmp_path: Path) -> None:
    """Get must name each override's entities, and reset must keep another setter's restricted selection."""
    other_context = _restricted_tool_context(tmp_path, OTHER)
    context = replace(other_context, requester_id=USER)
    tool = ThreadModelTools()

    with tool_runtime_context(other_context):
        await tool.switch_thread_model("expensive")
    with tool_runtime_context(context):
        await tool.switch_thread_model("cheap")
        get_payload = json.loads(await tool.get_thread_model())
        reset_payload = json.loads(await tool.reset_thread_model())

    assert get_payload["overrides"] == {"helper": "cheap", "restricted": "expensive"}
    assert reset_payload["cleared"] is True
    assert resolved_thread_models(context.config, context.runtime_paths) == {
        "helper": "default",
        "restricted": "expensive",
    }


@pytest.mark.parametrize("display_name", [None, "Default Model"])
def test_ai_run_metadata_uses_preparation_time_model(tmp_path: Path, display_name: str | None) -> None:
    """Run metadata must describe the model that produced the response, not the next override."""
    config = _config_with_models(tmp_path)
    config.models["default"].display_name = display_name
    config.models["large"].display_name = "Large Model"
    runtime_paths = runtime_paths_for(config)
    prepared_model_name = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(entity="test_agent", room_id=ROOM_ID, thread_id=THREAD_ID),
        None,
    ).model_name

    # A mid-run switch_thread_model call persists a new override before the
    # metadata for the current response is built.
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        entity_names=("test_agent",),
        config=config,
    )

    metadata = build_ai_run_metadata_content(
        prices={},
        config=config,
        model_name=prepared_model_name,
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.completed,
        model="default-model",
        model_provider="openai",
        tool_count=0,
    )

    assert metadata[AI_RUN_METADATA_KEY]["model"] == {
        "config": "default",
        "id": "default-model",
        "provider": "openai",
        **({"display_name": display_name} if display_name else {}),
    }


@pytest.mark.parametrize(
    ("model_name", "display_name", "expected_name"),
    [
        ("default", "  Friendly Model  ", "Friendly Model"),
        ("default", None, None),
        ("default", "", None),
        ("default", "  ", None),
        ("removed-model", "Friendly Model", None),
    ],
)
def test_ai_run_metadata_optional_model_display_name(
    tmp_path: Path,
    model_name: str,
    display_name: str | None,
    expected_name: str | None,
) -> None:
    """Publish the configured display name when set while retaining the model alias."""
    config = _config_with_models(tmp_path)
    config.models["default"] = ModelConfig(provider="openai", id="default-model", display_name=display_name)

    metadata = build_ai_run_metadata_content(
        prices={},
        config=config,
        model_name=model_name,
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.completed,
        model="default-model",
        model_provider="openai",
        tool_count=0,
    )

    model = metadata[AI_RUN_METADATA_KEY]["model"]
    assert model["config"] == model_name
    if expected_name is None:
        assert "display_name" not in model
    else:
        assert model["display_name"] == expected_name
