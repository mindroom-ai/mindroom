"""Saved approvals rebuild deferred tools without depending on warm process caches."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.tools.function import ToolResult

from mindroom import agents
from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.ai import ai_response
from mindroom.config.main import Config
from mindroom.config.models import ToolConfigEntry
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.calculator import CalculatorTools
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.mcp.types import MCPDiscoveredTool, MCPServerCatalog
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponsePausedForApproval
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, install_model_providers, make_turn_context
from tests.delegation_helpers import approval_calls_for, approval_executor, resume
from tests.fakes.scripted_model import ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.test_plugins import _preserved_plugin_loader_state

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.event_journal import ApprovalCall
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


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False], ids=["approved", "denied"])
async def test_saved_approval_restores_deferred_oauth_tool_with_a_cold_catalog_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    approved: bool,
) -> None:
    """A real persisted pause must resume after losing the OAuth catalog cache."""
    await _exercise_saved_approval(tmp_path, monkeypatch, approved=approved)


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
        "from mindroom.custom_tools.calculator import CalculatorTools\n"
        "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\n"
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
        "    file_access=ToolFileAccess.NONE,\n"
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
        "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\n"
        "from mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class UnavailableTools(Toolkit):\n"
        + failing_method
        + "        Path(__file__).with_name('constructed').touch()\n"
        + f"        raise {error_type}('Synthetic plugin unavailable')\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='unavailable_plugin',\n"
        "    file_access=ToolFileAccess.NONE,\n"
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


def _saved_run(config: Config, paths: RuntimePaths, identity: ToolExecutionIdentity, run_id: str) -> RunRecord:
    """Read the stored run through a new store handle, not an actor cache."""
    run = create_session_storage("general", config, paths, identity).get_run(run_id)
    assert isinstance(run, RunRecord)
    return run


def _assert_saved_run(
    config: Config,
    paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    run_id: str,
    status: RunStatus,
) -> None:
    """Observe normalized persistence through a new SQLite handle, not an actor cache."""
    run = _saved_run(config, paths, identity, run_id)
    assert run.status is status
    assert run.metadata is not None
    assert run.metadata["approval_probe"] == "saved"


def _observe_ordinary_calculator_calls(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
) -> list[tuple[float, float]]:
    calls_seen: list[tuple[float, float]] = []
    if not enabled:
        return calls_seen
    config.agents["general"].tools.append(ToolConfigEntry(name="calculator"))
    original_add = CalculatorTools.add

    def add(self: CalculatorTools, a: float, b: float) -> str:
        """Count real arithmetic executions without replacing the local tool behavior."""
        calls_seen.append((a, b))
        return original_add(self, a, b)

    monkeypatch.setattr(CalculatorTools, "add", add)
    return calls_seen


# The calls an approving requester grants; a same-name sibling (``call_denied``) is declined.
_APPROVABLE = frozenset({"call_example", "call_second"})


def _deferred_call(call_id: str, function_name: str, **arguments: object) -> dict[str, object]:
    """Call a deferred function through the call_tool wrapper a model without native tool search sees."""
    return tool_call("call_tool", call_id, name=function_name, arguments=arguments)


async def _exercise_saved_approval(  # noqa: C901, PLR0912, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tool_name: str = "mcp_example",
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
    arguments: dict[str, object] = {} if tool_name == "mcp_example" else {"a": 2, "b": 3}
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MATRIX_HOMESERVER": "https://matrix.example.org", "MINDROOM_NAMESPACE": ""},
    )
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": []},
                "memory": {"backend": "none"},
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
                "models": {"default": {"provider": "synthetic", "id": "synthetic"}},
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
    ordinary_calls = _observe_ordinary_calculator_calls(config, monkeypatch, enabled=mixed_calls)
    second_call = _deferred_call("call_second", function_name, **arguments)
    first_turn: list[ScriptStep] = calls(
        *([tool_call("add", "call_ordinary", a=2, b=3)] if mixed_calls else []),
        _deferred_call("call_example", function_name, **arguments),
        *([_deferred_call("call_denied", function_name, **arguments)] if mixed_decisions else []),
    )
    model = ScriptedModel(
        [
            first_turn,
            *([calls(second_call)] if repeat_pause or retry_denied else []),
            answer("Ready"),
        ],
    )
    install_model_providers(monkeypatch, {"default": model})
    storage = create_session_storage("general", config, paths, identity)
    initial = agents.create_agent(
        "general",
        config,
        paths,
        identity,
        history_storage=storage,
        supports_native_tool_approval=True,
    )
    offered = prepare_run_tools(initial.spec).functions
    original_names = set(offered)
    if plugin_path is not None:
        assert TOOL_METADATA[unrelated_tool_name or tool_name].function_names == ()
    if tool_name == "mcp_example":
        assert {"example_connection_status", "example_list_tools", "example_call_tool"} <= original_names
        assert function_name not in original_names
        await offered["example_list_tools"].entrypoint()
    else:
        assert function_name in original_names
    initial_discoveries = list(transport.discoveries)
    with pytest.raises(ResponsePausedForApproval) as raised:
        await ai_response(
            make_turn_context(
                "general",
                session_id=identity.session_id,
                room_id=identity.room_id,
                thread_id=identity.thread_id,
                requester_id=identity.requester_id,
                matrix_run_metadata={"approval_probe": "saved"},
            ),
            prompt="Look up the synthetic record",
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
            supports_native_tool_approval=True,
        )
    paused = raised.value.paused
    assert paused.toolkit_owners[("general", function_name)] == tool_name
    assert transport.executed == []
    assert ordinary_calls == ([(2, 3)] if mixed_calls else [])
    assert [tool.tool_call_id for tool in paused.tools] == [
        "call_example",
        *(["call_denied"] if mixed_decisions else []),
    ]
    _assert_saved_run(config, paths, identity, paused.run_id, RunStatus.paused)

    transport.cached_catalog = None
    transport.discoveries.clear()
    _apply_current_tool_restriction(config, tool_name, restriction)
    before_resume()
    if replacement_tool_name is not None:
        config.agents["general"].tools = [
            ToolConfigEntry(name=replacement_tool_name),
            *(config.agents["general"].tools if keep_original_owner else []),
        ]
    approval_calls = tuple(
        replace(
            call,
            tool_name=changed_call_name or call.tool_name,
            toolkit_name=None if missing_origin else call.toolkit_name,
            human_approval_required=True,
        )
        for call in approval_calls_for(paused, default_agent="general", toolkit_names={function_name: tool_name})
    )
    executor = approval_executor(config, paths)

    async def continue_saved_run(
        attempt: PausedAttempt,
        approval_calls_now: tuple[ApprovalCall, ...],
    ) -> CompletedApprovalRun | PausedAttempt:
        return await resume(
            executor,
            attempt,
            approval_calls_now,
            agent_name="general",
            identity=identity,
            config=config,
            runtime_paths=paths,
            decisions={call.tool_call_id: approved and call.tool_call_id in _APPROVABLE for call in approval_calls_now},
            denial_reasons={
                call.tool_call_id: None if approved and call.tool_call_id in _APPROVABLE else "Declined by requester"
                for call in approval_calls_now
            },
            approval_id="approval-example",
            request_body="Look up the synthetic record",
        )

    if (restriction is not None or expect_unavailable) and approved:
        with pytest.raises((ValueError, RuntimeError), match=r"(?i)(tool|function|approval)"):
            await continue_saved_run(paused, approval_calls)
        assert transport.executed == []
        assert len(model.requests) == 1
        _assert_saved_run(config, paths, identity, paused.run_id, RunStatus.paused)
        if restriction == "removed":
            assert transport.discoveries == []
        return
    result = await continue_saved_run(paused, approval_calls)
    if repeat_pause:
        assert isinstance(result, PausedAttempt)
        assert result.toolkit_owners[("general", function_name)] == tool_name
        assert [tool.tool_call_id for tool in result.tools] == ["call_second"]
        result = await continue_saved_run(
            result,
            tuple(
                replace(call, human_approval_required=True)
                for call in approval_calls_for(
                    result,
                    default_agent="general",
                    toolkit_names={function_name: tool_name},
                )
            ),
        )
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
        tool_results = [
            message for message in model.requests[-1].messages if message.role == "tool" and message.tool_call_id
        ]
        approved_results = [message for message in tool_results if message.tool_call_id == "call_example"]
        assert len(approved_results) == 1
        assert json.loads(str(approved_results[0].content)) == {"operation": "addition", "result": 5}
    assert len(model.requests) == 2 + repeat_pause + retry_denied
    if not approved:
        assert all(tool.name != function_name for request in model.requests[1:] for tool in request.tools)
    if mixed_decisions:
        denied_results = [
            message
            for message in model.requests[-1].messages
            if message.role == "tool" and message.tool_call_id == "call_denied"
        ]
        assert len(denied_results) == 1
        assert "Declined by requester" in str(denied_results[0].content)
    _assert_saved_run(config, paths, identity, paused.run_id, RunStatus.completed)
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
