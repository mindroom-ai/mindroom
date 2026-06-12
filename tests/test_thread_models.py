"""Tests for per-thread model overrides (!model command and thread_model tool)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import mindroom.tools  # noqa: F401
from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.commands.model_commands import handle_model_command
from mindroom.commands.parsing import CommandType, command_parser
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.custom_tools.thread_model import ThreadModelTools
from mindroom.thread_models import (
    _get_thread_model_override,
    _load_cache,
    _store_path,
    clear_thread_model_override,
    set_thread_model_override,
)
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import bind_runtime_paths, make_event_cache_mock, runtime_paths_for, test_runtime_paths

THREAD_ID = "$thread-root:localhost"
ROOM_ID = "!room:localhost"


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
    runtime_paths = test_runtime_paths(tmp_path)

    assert _get_thread_model_override(runtime_paths, THREAD_ID) is None
    assert _get_thread_model_override(runtime_paths, None) is None

    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        room_id=ROOM_ID,
        set_by="@user:localhost",
    )
    assert _get_thread_model_override(runtime_paths, THREAD_ID) == "large"
    assert _get_thread_model_override(runtime_paths, "$other:localhost") is None

    assert clear_thread_model_override(runtime_paths, THREAD_ID) is True
    assert _get_thread_model_override(runtime_paths, THREAD_ID) is None
    assert clear_thread_model_override(runtime_paths, THREAD_ID) is False


def test_store_ignores_corrupt_file(tmp_path: Path) -> None:
    """A corrupt store file should read as empty and be replaced on write."""
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")

    assert _get_thread_model_override(runtime_paths, THREAD_ID) is None
    # The corrupt parse result is cached so repeat reads skip re-parsing.
    assert _load_cache[path] == (path.stat().st_mtime_ns, {})

    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        room_id=ROOM_ID,
        set_by="@user:localhost",
    )
    assert _get_thread_model_override(runtime_paths, THREAD_ID) == "large"


def test_store_prunes_oldest_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The store should keep only the newest overrides past the cap."""
    runtime_paths = test_runtime_paths(tmp_path)
    monkeypatch.setattr("mindroom.thread_models._MAX_TRACKED_THREADS", 3)

    for index in range(5):
        set_thread_model_override(
            runtime_paths,
            thread_id=f"$thread-{index}:localhost",
            model_name="large",
            room_id=ROOM_ID,
            set_by="@user:localhost",
        )

    stored = json.loads(_store_path(runtime_paths).read_text(encoding="utf-8"))
    assert len(stored) == 3
    assert _get_thread_model_override(runtime_paths, "$thread-4:localhost") == "large"


def test_resolve_runtime_model_prefers_thread_override(tmp_path: Path) -> None:
    """A thread override should beat the agent's authored model."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        room_id=ROOM_ID,
        set_by="@user:localhost",
    )

    runtime_model = config.resolve_runtime_model(
        entity_name="test_agent",
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        runtime_paths=runtime_paths,
    )

    assert runtime_model.model_name == "large"
    assert runtime_model.context_window == 32_000


def test_resolve_runtime_model_thread_override_beats_room_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread override should beat the room_models override."""
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            room_models={"lobby": "default"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=32_000),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    monkeypatch.setattr("mindroom.matrix.state.get_room_alias_from_id", lambda *_args: "lobby")
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        room_id=ROOM_ID,
        set_by="@user:localhost",
    )

    runtime_model = config.resolve_runtime_model(
        entity_name="test_agent",
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        runtime_paths=runtime_paths,
    )

    assert runtime_model.model_name == "large"


def test_resolve_runtime_model_active_model_beats_thread_override(tmp_path: Path) -> None:
    """An explicit active model should beat the thread override."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        room_id=ROOM_ID,
        set_by="@user:localhost",
    )

    runtime_model = config.resolve_runtime_model(
        entity_name="test_agent",
        active_model_name="default",
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        runtime_paths=runtime_paths,
    )

    assert runtime_model.model_name == "default"


def test_resolve_runtime_model_ignores_stale_thread_override(tmp_path: Path) -> None:
    """An override naming a removed model should fall back to authored config."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="removed-model",
        room_id=ROOM_ID,
        set_by="@user:localhost",
    )

    runtime_model = config.resolve_runtime_model(
        entity_name="test_agent",
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        runtime_paths=runtime_paths,
    )

    assert runtime_model.model_name == "default"


def test_resolve_runtime_model_without_thread_id_keeps_authored_model(tmp_path: Path) -> None:
    """Resolution without thread context should ignore thread overrides."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        room_id=ROOM_ID,
        set_by="@user:localhost",
    )

    runtime_model = config.resolve_runtime_model(
        entity_name="test_agent",
        room_id=ROOM_ID,
        runtime_paths=runtime_paths,
    )

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
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
    )
    assert "✅" in response
    assert "`large`" in response
    assert _get_thread_model_override(runtime_paths, THREAD_ID) == "large"

    response = handle_model_command(
        "reset",
        config=config,
        runtime_paths=runtime_paths,
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
    )
    assert "✅" in response
    assert _get_thread_model_override(runtime_paths, THREAD_ID) is None

    response = handle_model_command(
        "reset",
        config=config,
        runtime_paths=runtime_paths,
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
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
    )
    assert "No thread model override" in response
    assert "`default`" in response
    assert "`large`" in response

    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        room_id=ROOM_ID,
        set_by="@user:localhost",
    )
    response = handle_model_command(
        "",
        config=config,
        runtime_paths=runtime_paths,
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
    )
    assert "`large` override" in response


def test_model_command_list_alias_shows_models(tmp_path: Path) -> None:
    """!model list must show the override state, even outside a thread."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    for thread_id in (THREAD_ID, None):
        response = handle_model_command(
            "list",
            config=config,
            runtime_paths=runtime_paths,
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
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
    )
    assert "Unknown model" in response
    assert _get_thread_model_override(runtime_paths, THREAD_ID) is None


def test_model_command_requires_thread_for_set(tmp_path: Path) -> None:
    """Setting an override outside a thread should fail clearly."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    response = handle_model_command(
        "large",
        config=config,
        runtime_paths=runtime_paths,
        room_id=ROOM_ID,
        thread_id=None,
        requester_user_id="@user:localhost",
    )
    assert "only work inside a thread" in response
    assert _get_thread_model_override(runtime_paths, THREAD_ID) is None


def test_model_command_default_sets_model_instead_of_resetting(tmp_path: Path) -> None:
    """A configured model named "default" must be settable, not treated as a reset alias."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)

    response = handle_model_command(
        "default",
        config=config,
        runtime_paths=runtime_paths,
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        requester_user_id="@user:localhost",
    )
    assert "✅" in response
    assert "`default`" in response
    assert _get_thread_model_override(runtime_paths, THREAD_ID) == "default"


def test_resolve_runtime_model_requires_runtime_paths_for_thread_id(tmp_path: Path) -> None:
    """Passing thread_id without runtime_paths must fail loudly, mirroring the room branch."""
    config = _config_with_models(tmp_path)

    with pytest.raises(ValueError, match="thread-specific runtime model"):
        config.resolve_runtime_model(entity_name="test_agent", thread_id=THREAD_ID)


def test_store_drops_records_with_corrupt_set_at(tmp_path: Path) -> None:
    """Records with a non-string set_at must be dropped so prune sorting cannot fail."""
    runtime_paths = test_runtime_paths(tmp_path)
    path = _store_path(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"$bad:localhost": {"model": "large", "set_at": 12345}}),
        encoding="utf-8",
    )

    assert _get_thread_model_override(runtime_paths, "$bad:localhost") is None

    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        room_id=ROOM_ID,
        set_by="@user:localhost",
    )
    assert _get_thread_model_override(runtime_paths, THREAD_ID) == "large"


def _make_tool_context(*, thread_id: str | None = THREAD_ID) -> ToolRuntimeContext:
    config = _config_with_models(Path(tempfile.mkdtemp()))
    return ToolRuntimeContext(
        agent_name="test_agent",
        room_id=ROOM_ID,
        thread_id=thread_id,
        resolved_thread_id=thread_id,
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=AsyncMock(),
        event_cache=make_event_cache_mock(),
        room=None,
        storage_path=None,
    )


def test_thread_model_tool_registered() -> None:
    """The thread_model tool should be available from the metadata registry."""
    config = _config_with_models(Path(tempfile.mkdtemp()))

    assert "thread_model" in TOOL_METADATA
    assert isinstance(
        get_tool_by_name("thread_model", runtime_paths_for(config), worker_target=None),
        ThreadModelTools,
    )


@pytest.mark.asyncio
async def test_thread_model_tool_requires_runtime_context() -> None:
    """Tool calls should fail clearly outside Matrix runtime context."""
    payload = json.loads(await ThreadModelTools().switch_thread_model("large"))

    assert payload["status"] == "error"
    assert payload["tool"] == "thread_model"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_thread_model_tool_requires_thread() -> None:
    """Switching should fail clearly without an active thread."""
    context = _make_tool_context(thread_id=None)
    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().switch_thread_model("large"))

    assert payload["status"] == "error"
    assert "thread" in payload["message"]


@pytest.mark.asyncio
async def test_thread_model_tool_switch_get_and_reset() -> None:
    """Switch, get, and reset should round-trip through the store."""
    context = _make_tool_context()
    tool = ThreadModelTools()

    with tool_runtime_context(context):
        switch_payload = json.loads(await tool.switch_thread_model("large"))
        get_payload = json.loads(await tool.get_thread_model())
        reset_payload = json.loads(await tool.reset_thread_model())

    assert switch_payload["status"] == "ok"
    assert switch_payload["model"] == "large"
    assert switch_payload["model_id"] == "large-model"
    assert get_payload["override"] == "large"
    assert reset_payload["status"] == "ok"
    assert reset_payload["cleared"] is True
    assert _get_thread_model_override(context.runtime_paths, THREAD_ID) is None


@pytest.mark.asyncio
async def test_thread_model_tool_rejects_unknown_model() -> None:
    """Switching should reject names missing from config.models."""
    context = _make_tool_context()

    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().switch_thread_model("nonexistent"))

    assert payload["status"] == "error"
    assert payload["available_models"] == ["default", "large"]
    assert _get_thread_model_override(context.runtime_paths, THREAD_ID) is None


@pytest.mark.asyncio
async def test_thread_model_tool_reports_stale_override_as_inactive() -> None:
    """An override naming a removed model must not be reported as active."""
    context = _make_tool_context()
    set_thread_model_override(
        context.runtime_paths,
        thread_id=THREAD_ID,
        model_name="removed-model",
        room_id=ROOM_ID,
        set_by="@user:localhost",
    )

    with tool_runtime_context(context):
        payload = json.loads(await ThreadModelTools().get_thread_model())

    assert payload["status"] == "ok"
    assert payload["override"] is None
    assert payload["stale_override"] == "removed-model"
    assert "no longer configured" in payload["note"]


def test_ai_run_metadata_uses_preparation_time_model(tmp_path: Path) -> None:
    """Run metadata must describe the model that produced the response, not the next override."""
    config = _config_with_models(tmp_path)
    runtime_paths = runtime_paths_for(config)
    prepared_model_name = config.resolve_runtime_model(
        entity_name="test_agent",
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        runtime_paths=runtime_paths,
    ).model_name

    # A mid-run switch_thread_model call persists a new override before the
    # metadata for the current response is built.
    set_thread_model_override(
        runtime_paths,
        thread_id=THREAD_ID,
        model_name="large",
        room_id=ROOM_ID,
        set_by="@user:localhost",
    )

    metadata = build_ai_run_metadata_content(
        config=config,
        model_name=prepared_model_name,
        run_id="run-1",
        session_id="session-1",
        status="completed",
        model="default-model",
        model_provider="openai",
    )

    assert metadata[AI_RUN_METADATA_KEY]["model"] == {
        "config": "default",
        "id": "default-model",
        "provider": "openai",
    }
