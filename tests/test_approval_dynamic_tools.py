"""Saved approvals rebuild deferred tools without depending on warm process caches."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest
from agno.run.base import RunStatus
from agno.tools.calculator import CalculatorTools
from agno.tools.function import ToolResult
from openai import AsyncOpenAI

from mindroom.agent_storage import create_session_storage, get_agent_session
from mindroom.agents import create_agent
from mindroom.approval_tools import toolkit_owners_for_agents
from mindroom.config.main import Config
from mindroom.config.models import ToolConfigEntry
from mindroom.constants import resolve_runtime_paths
from mindroom.event_journal import ApprovalCall, ApprovalContinuation
from mindroom.history.prompt_tokens import agent_tool_definition_payloads_for_logging
from mindroom.history.session_context import close_agent_runtime_state_dbs
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.mcp.types import MCPDiscoveredTool, MCPServerCatalog
from mindroom.openai_models import MindRoomOpenAIResponses
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, paused_attempt_from_response
from mindroom.tool_system import dynamic_toolkits
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.dynamic_toolkits import get_loaded_tools_for_session, save_loaded_tools_for_session
from mindroom.tool_system.runtime_context import ToolDispatchContext
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _noop_typing
from tests.test_openai_native_compaction import _ANSWER, _event, _response
from tests.test_plugins import _preserved_plugin_loader_state

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


class _ScopedCatalogTransport:
    """Fake only MCP discovery and remote execution; keep real toolkit registration."""

    def __init__(self) -> None:
        self.cached_catalog: MCPServerCatalog | None = None
        self.discoveries: list[tuple[str, CredentialsManager | None, ResolvedWorkerTarget | None]] = []
        self.executed: list[tuple[str, str, dict[str, object]]] = []
        self.catalog = MCPServerCatalog(
            server_id="example",
            tool_name="mcp_example",
            tool_prefix="example",
            tools=(
                MCPDiscoveredTool(
                    remote_name="lookup",
                    function_name="example_lookup",
                    description="Look up a synthetic record",
                    input_schema={"type": "object", "properties": {}},
                    output_schema=None,
                ),
            ),
            instructions=None,
            catalog_hash="synthetic-catalog",
        )

    def cached_request_catalog(
        self,
        server_id: str,
        *,
        worker_target: ResolvedWorkerTarget | None,
    ) -> MCPServerCatalog | None:
        """Construction sees only the previously discovered scoped catalog."""
        assert server_id == "example"
        del worker_target
        return self.cached_catalog

    async def get_request_catalog(
        self,
        server_id: str,
        *,
        credentials_manager: CredentialsManager | None,
        worker_target: ResolvedWorkerTarget | None,
        expected_config: Config | None = None,
    ) -> MCPServerCatalog:
        """Discovery populates the cache, as the external MCP boundary would."""
        del expected_config
        self.discoveries.append((server_id, credentials_manager, worker_target))
        self.cached_catalog = self.catalog
        return self.catalog

    async def call_tool(
        self,
        server_id: str,
        remote_tool_name: str,
        arguments: dict[str, object],
        **_kwargs: object,
    ) -> ToolResult:
        """Record exact-once remote execution without contacting a server."""
        self.executed.append((server_id, remote_tool_name, arguments))
        return ToolResult(content="Synthetic record")

    def mcp_tool_unavailable_messages_for_loaded_tools(self, *_args: object) -> list[str]:
        """The synthetic configured server is available."""
        return []

    def function_name_collision_messages_for_loaded_tools(self, *_args: object, **_kwargs: object) -> list[str]:
        """The fixture has no colliding function names."""
        return []


@pytest.fixture(autouse=True)
def _isolate_dynamic_caches() -> Iterator[None]:
    dynamic_toolkits._loaded_tools.clear()
    bind_mcp_server_manager(None)
    yield
    dynamic_toolkits._loaded_tools.clear()
    bind_mcp_server_manager(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_state", ["cold", "newer_load", "newer_unload"])
@pytest.mark.parametrize("approved", [True, False], ids=["approved", "denied"])
async def test_saved_approval_restores_deferred_oauth_tool_with_both_caches_cold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cache_state: str,
    *,
    approved: bool,
) -> None:
    """A real persisted pause must resume after losing toolkit and OAuth catalog caches."""
    await _exercise_saved_approval(tmp_path, monkeypatch, cache_state=cache_state, approved=approved)


@pytest.mark.asyncio
async def test_saved_approval_does_not_repeat_completed_sibling_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resuming one paused call must not repeat an ordinary call completed before the pause."""
    await _exercise_saved_approval(tmp_path, monkeypatch, mixed_calls=True)


@pytest.mark.asyncio
async def test_saved_approval_restores_deferred_local_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recorded toolkit identity restores a deferred local function after restart."""
    await _exercise_saved_approval(tmp_path, monkeypatch, tool_name="calculator")


@pytest.mark.asyncio
@pytest.mark.parametrize("becomes_unavailable", [False, True], ids=["restored", "unavailable-owner"])
@pytest.mark.parametrize("replace_owner", ["none", "removed", "collision", "initial-collision"])
async def test_saved_approval_restores_deferred_plugin_without_function_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    becomes_unavailable: bool,
    replace_owner: str,
) -> None:
    """Existing plugins may expose real functions without declaring their names in metadata."""
    plugin_path = tmp_path / "approval-plugin"
    plugin_path.mkdir()
    (plugin_path / "mindroom.plugin.json").write_text(
        json.dumps({"name": "approval-plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_path / "tools.py").write_text(
        "from agno.tools.calculator import CalculatorTools\n"
        "from mindroom.tool_system.declarations import ToolCategory\n"
        "from mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class ApprovalCalculator(CalculatorTools):\n"
        "    unavailable = False\n"
        "    executed_calls = []\n"
        "\n"
        "    def __init__(self):\n"
        "        if self.unavailable:\n"
        "            raise RuntimeError('Synthetic tool unavailable')\n"
        "        super().__init__()\n"
        "\n"
        "    def add(self, a: float, b: float) -> str:\n"
        "        self.executed_calls.append((a, b))\n"
        "        return super().add(a, b)\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='approval_calculator',\n"
        "    display_name='Approval Calculator',\n"
        "    description='Perform synthetic arithmetic',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def approval_calculator_tools():\n"
        "    return ApprovalCalculator\n",
        encoding="utf-8",
    )
    plugin_classes: list[type] = []

    def prepare_plugin_for_resume() -> None:
        factory = TOOL_METADATA["approval_calculator"].factory
        assert factory is not None
        plugin_class = factory()
        plugin_classes.append(plugin_class)
        assert plugin_class.executed_calls == []
        plugin_class.unavailable = becomes_unavailable

    with _preserved_plugin_loader_state():
        await _exercise_saved_approval(
            tmp_path,
            monkeypatch,
            tool_name="approval_calculator",
            plugin_path=plugin_path,
            before_resume=prepare_plugin_for_resume,
            expect_unavailable=becomes_unavailable or replace_owner in {"removed", "collision"},
            replacement_tool_name="calculator" if replace_owner in {"removed", "collision"} else None,
            initial_collision=replace_owner == "initial-collision",
            keep_original_owner=replace_owner == "collision",
        )
        assert plugin_classes[0].executed_calls == (
            [] if becomes_unavailable or replace_owner in {"removed", "collision"} else [(2, 3)]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ["constructor", "get_async_functions"])
@pytest.mark.parametrize("error_type", ["RuntimeError", "KeyError"])
async def test_saved_approval_ignores_unavailable_unrelated_deferred_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
    error_type: str,
) -> None:
    """An unused plugin's discovery failure cannot block a valid persisted calculator approval."""
    plugin_path = tmp_path / "unavailable-plugin"
    plugin_path.mkdir()
    (plugin_path / "mindroom.plugin.json").write_text(
        json.dumps({"name": "unavailable-plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    failing_method = (
        "    def __init__(self):\n"
        if failure_site == "constructor"
        else (
            "    def __init__(self):\n"
            "        super().__init__(name='unavailable', tools=[])\n"
            "\n"
            "    def get_async_functions(self):\n"
        )
    )
    (plugin_path / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from pathlib import Path\n"
        "from mindroom.tool_system.declarations import ToolCategory\n"
        "from mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class UnavailableTools(Toolkit):\n"
        + failing_method
        + "        Path(__file__).with_name('constructed').touch()\n"
        + f"        raise {error_type}('Synthetic plugin unavailable')\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='unavailable_plugin',\n"
        "    display_name='Unavailable Plugin',\n"
        "    description='An unrelated unavailable integration',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def unavailable_tools():\n"
        "    return UnavailableTools\n",
        encoding="utf-8",
    )
    with _preserved_plugin_loader_state():
        await _exercise_saved_approval(
            tmp_path,
            monkeypatch,
            tool_name="calculator",
            plugin_path=plugin_path,
            unrelated_tool_name="unavailable_plugin",
        )
        assert not (plugin_path / "constructed").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["removed", "include", "exclude", "disabled"])
@pytest.mark.parametrize("approved", [True, False], ids=["approved", "denied"])
async def test_saved_approval_respects_current_tool_restrictions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restriction: str,
    *,
    approved: bool,
) -> None:
    """An old approval cannot re-enable a removed or newly excluded remote function."""
    await _exercise_saved_approval(tmp_path, monkeypatch, restriction=restriction, approved=approved)


def _apply_current_tool_restriction(config: Config, tool_name: str, restriction: str | None) -> None:
    if restriction == "removed":
        config.agents["general"].tools = [entry for entry in config.agents["general"].tools if entry.name != tool_name]
    elif restriction == "disabled":
        config.mcp_servers["example"] = config.mcp_servers["example"].model_copy(update={"enabled": False})
    elif restriction is not None:
        overrides = {"include_tools": ["other"]} if restriction == "include" else {"exclude_tools": ["lookup"]}
        config.agents["general"].tools = [
            ToolConfigEntry(name=tool_name, defer=True, overrides=overrides),
            ToolConfigEntry(name="sleep", defer=True),
        ]


def _assert_saved_run(
    config: Config,
    paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    run_id: str,
    status: RunStatus,
    original_schema: list[dict[str, object]],
) -> None:
    """Observe normalized persistence through a new SQLite handle, not an actor cache."""
    assert identity.session_id is not None
    storage = create_session_storage("general", config, paths, identity)
    try:
        session = get_agent_session(storage, identity.session_id)
        assert session is not None
        run = session.get_run(run_id)
        assert run is not None
        assert run.status == status
        assert run.metadata is not None
        assert run.metadata["tools_schema"] == original_schema
    finally:
        storage.close()


def _observe_ordinary_calculator_calls(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
) -> list[tuple[float, float]]:
    calls: list[tuple[float, float]] = []
    if not enabled:
        return calls
    config.agents["general"].tools.append(ToolConfigEntry(name="calculator"))
    original_add = CalculatorTools.add

    def add(self: CalculatorTools, a: float, b: float) -> str:
        """Count real arithmetic executions without replacing the local tool behavior."""
        calls.append((a, b))
        return original_add(self, a, b)

    monkeypatch.setattr(CalculatorTools, "add", add)
    return calls


async def _exercise_saved_approval(  # noqa: C901, PLR0912, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tool_name: str = "mcp_example",
    cache_state: str = "cold",
    approved: bool = True,
    restriction: str | None = None,
    mixed_calls: bool = False,
    plugin_path: Path | None = None,
    unrelated_tool_name: str | None = None,
    before_resume: Callable[[], None] = lambda: None,
    expect_unavailable: bool = False,
    replacement_tool_name: str | None = None,
    keep_original_owner: bool = False,
    missing_origin: bool = False,
    changed_call_name: str | None = None,
    repeat_pause: bool = False,
    mixed_decisions: bool = False,
    retry_denied: bool = False,
    initial_collision: bool = False,
) -> None:
    function_name = "example_lookup" if tool_name == "mcp_example" else "add"
    arguments = {} if tool_name == "mcp_example" else {"a": 2, "b": 3}
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MATRIX_HOMESERVER": "https://matrix.example.org", "MINDROOM_NAMESPACE": ""},
    )
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": [], "learning": False},
                "plugins": [str(plugin_path)] if plugin_path is not None else [],
                "agents": {
                    "general": {
                        "display_name": "General",
                        "tools": [
                            {tool_name: {"defer": True}},
                            {"sleep": {"defer": True}},
                            *([{unrelated_tool_name: {"defer": True}}] if unrelated_tool_name is not None else []),
                        ],
                    },
                },
                "models": {"default": {"provider": "openai", "id": "test-model", "api": "responses"}},
                "mcp_servers": {
                    "example": {
                        "transport": "streamable-http",
                        "url": "https://mcp.example.org/api",
                        "auth": {"type": "oauth"},
                    },
                },
                "tool_approval": {
                    "default": "auto_approve",
                    "rules": [{"match": function_name, "action": "require_approval"}],
                },
            },
        ),
        paths,
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="approval-session",
    )
    if initial_collision:
        config.agents["general"].tools.append(ToolConfigEntry(name="calculator"))
    transport = _ScopedCatalogTransport()
    bind_mcp_server_manager(transport)  # type: ignore[arg-type]
    requests: list[dict[str, Any]] = []
    ordinary_calls = _observe_ordinary_calculator_calls(config, monkeypatch, enabled=mixed_calls)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get("stream") and (repeat_pause or retry_denied) and len(requests) == 2:
            call = {
                "type": "function_call",
                "id": "fc_second",
                "call_id": "call_second",
                "name": function_name,
                "arguments": json.dumps(arguments),
                "status": "completed",
            }
            events = _event("response.created", response={**_response([]), "status": "in_progress"})
            events += _event("response.output_item.added", output_index=0, item={**call, "arguments": ""})
            events += _event("response.function_call_arguments.delta", output_index=0, delta=json.dumps(arguments))
            events += _event("response.output_item.done", output_index=0, item=call)
            events += _event("response.completed", response=_response([call]))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=events)
        if payload.get("stream"):
            events = _event("response.output_text.delta", delta="Ready", output_index=0, content_index=0)
            events += _event("response.completed", response=_response([_ANSWER]))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=events)
        call = {
            "type": "function_call",
            "id": "fc_example",
            "call_id": "call_example",
            "name": function_name,
            "arguments": json.dumps(arguments),
            "status": "completed",
        }
        ordinary = {
            "type": "function_call",
            "id": "fc_ordinary",
            "call_id": "call_ordinary",
            "name": "add",
            "arguments": '{"a":2,"b":3}',
            "status": "completed",
        }
        output = [ordinary, call] if mixed_calls else [call]
        if mixed_decisions:
            output.append({**call, "id": "fc_denied", "call_id": "call_denied"})
        return httpx.Response(200, json=_response(output))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = AsyncOpenAI(api_key="test-key", http_client=http_client)
        monkeypatch.setattr(
            "mindroom.agents._load_agent_model_instance",
            lambda *_args, **_kwargs: MindRoomOpenAIResponses(id="test-model", async_client=client, store=False),
        )
        save_loaded_tools_for_session(
            agent_name="general",
            session_id=identity.session_id,
            loaded_tools=[tool_name],
        )
        storage = create_session_storage("general", config, paths, identity)
        initial = create_agent(
            "general",
            config,
            paths,
            identity,
            session_id=identity.session_id,
            history_storage=storage,
            supports_native_tool_approval=True,
        )
        original_schema = agent_tool_definition_payloads_for_logging(initial)
        original_names = {entry["name"] for entry in original_schema}
        if plugin_path is not None:
            assert TOOL_METADATA[unrelated_tool_name or tool_name].function_names == ()
        if tool_name == "mcp_example":
            assert {"example_connection_status", "example_list_tools", "example_call_tool"} <= original_names
            assert function_name not in original_names
            toolkit = next(tool for tool in initial.tools or [] if tool.name == "mcp_example")
            await toolkit.async_functions["example_list_tools"].entrypoint()
        else:
            assert function_name in original_names
        initial_discoveries = list(transport.discoveries)
        close_agent_runtime_state_dbs(initial, shared_scope_storage=storage)
        actor = create_agent(
            "general",
            config,
            paths,
            identity,
            session_id=identity.session_id,
            history_storage=storage,
            supports_native_tool_approval=True,
        )
        paused = await actor.arun(
            "Look up the synthetic record",
            session_id=identity.session_id,
            user_id=identity.requester_id,
            metadata={"tools_schema": original_schema},
        )
        assert paused.status == RunStatus.paused
        captured = paused_attempt_from_response(
            paused,
            fallback_session_id=identity.session_id,
            fallback_run_id=paused.run_id,
            toolkit_owners=toolkit_owners_for_agents([actor]),
        )
        assert captured is not None
        assert captured.toolkit_owners is not None
        assert captured.toolkit_owners[("general", function_name)] == tool_name
        assert transport.executed == []
        assert ordinary_calls == ([(2, 3)] if mixed_calls else [])
        requirement = (paused.requirements or [])[0]
        assert requirement.tool_execution is not None
        tool_call_id = requirement.tool_execution.tool_call_id
        assert tool_call_id is not None
        close_agent_runtime_state_dbs(actor, shared_scope_storage=storage)
        storage.close()
        _assert_saved_run(config, paths, identity, paused.run_id, RunStatus.paused, original_schema)

        dynamic_toolkits._loaded_tools.clear()
        transport.cached_catalog = None
        transport.discoveries.clear()
        if cache_state != "cold":
            save_loaded_tools_for_session(
                agent_name="general",
                session_id=identity.session_id,
                loaded_tools=["sleep"] if cache_state == "newer_load" else [],
            )
        selection_before = get_loaded_tools_for_session(
            agent_name="general",
            config=config,
            session_id=identity.session_id,
        )
        cache_before = deepcopy(dynamic_toolkits._loaded_tools)
        _apply_current_tool_restriction(config, tool_name, restriction)
        before_resume()
        if replacement_tool_name is not None:
            config.agents["general"].tools = [
                ToolConfigEntry(name=replacement_tool_name),
                *(config.agents["general"].tools if keep_original_owner else []),
            ]
        continuation = ApprovalContinuation(
            approval_id="approval-example",
            run_id=paused.run_id,
            session_id=identity.session_id,
            entity_kind="agent",
            entity_name="general",
            room_id=identity.room_id,
            thread_id=identity.thread_id,
            requester_id=identity.requester_id,
            response_event_id="$waiting",
            source_event_ids=("$source",),
            state="claimed",
            calls=(
                ApprovalCall(
                    tool_call_id=tool_call_id,
                    tool_name=changed_call_name or function_name,
                    toolkit_name=None if missing_origin else captured.toolkit_owners[("general", function_name)],
                    invoking_agent="general",
                    expires_at_ns=2**62,
                    human_approval_required=True,
                ),
            ),
        )
        if mixed_decisions:
            continuation = replace(
                continuation,
                calls=(
                    *continuation.calls,
                    replace(continuation.calls[0], tool_call_id="fc_denied"),
                ),
            )
        runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
        execution = replace(runner._approval_execution, config=lambda: config, runtime_paths=paths)
        monkeypatch.setattr(
            execution.knowledge_access,
            "resolve_for_agent_async",
            AsyncMock(return_value=SimpleNamespace(knowledge=None)),
        )
        monkeypatch.setattr("mindroom.approval_execution.typing_indicator", _noop_typing)

        async def continue_saved_run() -> CompletedApprovalRun | PausedAttempt:
            return await execution.continue_run(
                continuation,
                execution_identity=identity,
                tool_dispatch=ToolDispatchContext(execution_identity=identity),
                decisions={
                    call.tool_call_id: approved and call.tool_call_id == tool_call_id for call in continuation.calls
                },
                denial_reasons={
                    call.tool_call_id: None
                    if approved and call.tool_call_id == tool_call_id
                    else "Declined by requester"
                    for call in continuation.calls
                },
                tool_trace_collector=[],
                typing_log_context={},
            )

        if (restriction is not None or expect_unavailable) and approved:
            with pytest.raises((ValueError, RuntimeError), match=r"(?i)(tool|function|approval)"):
                await continue_saved_run()
            assert transport.executed == []
            assert len(requests) == 1
            assert dynamic_toolkits._loaded_tools == cache_before
            _assert_saved_run(config, paths, identity, paused.run_id, RunStatus.paused, original_schema)
            if restriction == "removed":
                assert transport.discoveries == []
            return
        result = await continue_saved_run()
        if repeat_pause:
            assert isinstance(result, PausedAttempt)
            assert result.toolkit_owners is not None
            assert result.toolkit_owners[("general", function_name)] == tool_name
            assert result.tools[0].tool_call_id == "fc_second"
            tool_call_id = "fc_second"
            continuation = replace(
                continuation,
                calls=(
                    replace(
                        continuation.calls[0],
                        tool_call_id=tool_call_id,
                        toolkit_name=result.toolkit_owners[("general", function_name)],
                    ),
                ),
            )
            result = await continue_saved_run()
        assert isinstance(result, CompletedApprovalRun)
        if tool_name == "mcp_example":
            assert transport.executed == ([("example", "lookup", {})] * (1 + repeat_pause) if approved else [])
            assert len(transport.discoveries) == int(approved) * (1 + repeat_pause)
            if approved:
                assert transport.discoveries[0][0] == "example"
                assert transport.discoveries[0][1] is not None
                assert transport.discoveries[0][2] == initial_discoveries[0][2]
        else:
            assert transport.discoveries == []
            tool_results = [item for item in requests[-1]["input"] if item.get("type") == "function_call_output"]
            assert len(tool_results) == 1
            assert json.loads(tool_results[0]["output"]) == {"operation": "addition", "result": 5}
        assert len(requests) == 2 + repeat_pause + retry_denied
        if not approved:
            assert all(
                tool.get("name") != function_name for request in requests[1:] for tool in request.get("tools", [])
            )
        if mixed_decisions:
            denied_results = [
                item
                for item in requests[-1]["input"]
                if item.get("type") == "function_call_output" and item.get("call_id") == "call_denied"
            ]
            assert len(denied_results) == 1
            assert "Declined by requester" in denied_results[0]["output"]
        assert dynamic_toolkits._loaded_tools == cache_before
        if restriction is None:
            assert (
                get_loaded_tools_for_session(
                    agent_name="general",
                    config=config,
                    session_id=identity.session_id,
                )
                == selection_before
            )
        _assert_saved_run(config, paths, identity, paused.run_id, RunStatus.completed, original_schema)
        assert ordinary_calls == ([(2, 3)] if mixed_calls else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False], ids=["approved", "denied"])
async def test_saved_approval_with_unknown_legacy_origin_requires_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    approved: bool,
) -> None:
    """Unknown historical owners cannot be reconstructed from today's catalogs."""
    await _exercise_saved_approval(
        tmp_path,
        monkeypatch,
        missing_origin=True,
        expect_unavailable=approved,
        approved=approved,
    )


@pytest.mark.asyncio
async def test_saved_approval_must_match_persisted_function_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approving another function on the same toolkit cannot authorize the pending call."""
    await _exercise_saved_approval(
        tmp_path,
        monkeypatch,
        tool_name="calculator",
        changed_call_name="multiply",
        expect_unavailable=True,
    )


@pytest.mark.asyncio
async def test_repeated_approval_pause_captures_rebuilt_toolkit_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second native pause retains its newly rebuilt owner and resumes exactly once."""
    await _exercise_saved_approval(tmp_path, monkeypatch, repeat_pause=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False], ids=["mixed", "all-denied"])
async def test_same_function_calls_keep_individual_approval_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    approved: bool,
) -> None:
    """Denying one same-name call cannot deny or execute its sibling."""
    await _exercise_saved_approval(tmp_path, monkeypatch, mixed_decisions=True, approved=approved)


@pytest.mark.asyncio
async def test_denied_missing_tool_cannot_execute_on_followup_model_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native rejection leaves no executable or advertised placeholder behind."""
    await _exercise_saved_approval(tmp_path, monkeypatch, approved=False, restriction="removed", retry_denied=True)
