"""Exercise worker-routing policy through real agent materialization."""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING

import nio
import pytest
from agno.run import RunContext
from agno.tools.calculator import CalculatorTools
from agno.tools.function import FunctionCall
from agno.tools.toolkit import Toolkit

from mindroom.agents import create_agent
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.message_target import MessageTarget
from mindroom.shell_execution import ShellRunResult
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity
from mindroom.tools import shell as shell_module
from mindroom.workers.backend import WorkerBackendError
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_matrix_client_mock, make_relation_lookup
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path

    from agno.agent import Agent


def _create_routing_agent(
    tmp_path: Path,
    process_env: dict[str, str],
    *,
    agent_settings: dict[str, object] | None = None,
    defaults: dict[str, object] | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> Agent:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env=process_env,
    )
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "ollama", "id": "test-model"}},
            "agents": {
                "routing": {
                    "display_name": "Routing",
                    "tools": ["shell", "calculator"],
                    "include_default_tools": False,
                    "memory_backend": "none",
                    **(agent_settings or {}),
                },
            },
            "defaults": defaults or {},
        },
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths, usernames={"router": "router", "routing": "routing"})
    return create_agent(
        "routing",
        config,
        runtime_paths,
        execution_identity=execution_identity,
        include_interactive_questions=False,
        persist_runtime_state=False,
        supports_native_tool_approval=True,
    )


async def _invoke(agent: Agent, function_name: str, **kwargs: object) -> str:
    for toolkit in agent.tools or []:
        if not isinstance(toolkit, Toolkit):
            continue
        function = toolkit.get_async_functions().get(function_name)
        if function is None:
            continue
        assert function.entrypoint is not None
        result = function.entrypoint(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        assert isinstance(result, str)
        return result
    pytest.fail(f"Agent did not materialize {function_name}")


@pytest.fixture(autouse=True)
def _local_execution_sinks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep real tool construction while preventing any local command execution."""

    async def run_command(*_args: object, **_kwargs: object) -> ShellRunResult:
        return ShellRunResult(message="local:shell")

    def add(self: CalculatorTools, a: float, b: float) -> str:
        del self, a, b
        return "local:calculator"

    monkeypatch.setattr(shell_module, "run_command", run_command)
    monkeypatch.setattr(CalculatorTools, "add", add)
    monkeypatch.delenv("MINDROOM_SANDBOX_SHELL_SUPERVISOR_SOCKET", raising=False)


@pytest.fixture
def proxy_targets(monkeypatch: pytest.MonkeyPatch) -> list[ResolvedWorkerTarget | None]:
    """Replace only remote dispatch; retain resolver, registry and proxy wrappers."""
    targets: list[ResolvedWorkerTarget | None] = []

    def dispatch(
        *,
        tool_name: str,
        worker_target: ResolvedWorkerTarget | None,
        **_kwargs: object,
    ) -> str:
        targets.append(worker_target)
        return f"worker:{tool_name}"

    monkeypatch.setattr(sandbox_proxy, "_call_proxy_sync", dispatch)
    return targets


@pytest.mark.asyncio
@pytest.mark.usefixtures("proxy_targets")
@pytest.mark.parametrize(
    ("mode", "proxy_tools", "with_url", "expected"),
    [
        pytest.param(None, None, False, ("local:shell", "local:calculator"), id="plain-local"),
        pytest.param(None, None, True, ("worker:shell", "worker:calculator"), id="proxy-url-default"),
        pytest.param(None, "calculator", True, ("local:shell", "worker:calculator"), id="proxy-tools"),
        pytest.param(None, "*", False, ("worker:shell", "worker:calculator"), id="explicit-wildcard"),
        pytest.param("off", None, False, ("local:shell", "local:calculator"), id="off-no-url"),
        pytest.param("off", None, True, ("local:shell", "local:calculator"), id="off"),
        pytest.param("local", None, True, ("local:shell", "local:calculator"), id="local"),
        pytest.param("disabled", None, True, ("local:shell", "local:calculator"), id="disabled"),
        pytest.param("all", None, True, ("worker:shell", "worker:calculator"), id="all"),
        pytest.param("sandbox_all", None, True, ("worker:shell", "worker:calculator"), id="sandbox-all"),
        pytest.param("selective", "shell", True, ("worker:shell", "local:calculator"), id="selective-shell"),
        pytest.param("selective", "calculator", True, ("local:shell", "worker:calculator"), id="selective-calculator"),
        pytest.param("selective", None, True, ("local:shell", "local:calculator"), id="selective-empty"),
    ],
)
async def test_omitted_worker_tools_honors_environment(
    tmp_path: Path,
    mode: str | None,
    proxy_tools: str | None,
    with_url: bool,
    expected: tuple[str, str],
) -> None:
    """Omitted YAML must not override the environment with metadata defaults."""
    process_env = {}
    if mode is not None:
        process_env["MINDROOM_SANDBOX_EXECUTION_MODE"] = mode
    if proxy_tools is not None:
        process_env["MINDROOM_SANDBOX_PROXY_TOOLS"] = proxy_tools
    if with_url:
        process_env["MINDROOM_SANDBOX_PROXY_URL"] = "http://sandbox.invalid"
    agent = _create_routing_agent(tmp_path, process_env)

    assert (await _invoke(agent, "run_shell_command", args=["unused"])).endswith(expected[0])
    assert await _invoke(agent, "add", a=1, b=2) == expected[1]
    if expected == ("local:shell", "local:calculator"):
        assert "No tools use a worker runtime." in (agent.role or "")


@pytest.mark.asyncio
@pytest.mark.usefixtures("proxy_targets")
@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
@pytest.mark.parametrize("with_static_url", [False, True])
async def test_dedicated_defaults_ignore_static_runner_url(
    tmp_path: Path,
    backend: str,
    with_static_url: bool,
) -> None:
    """An unused static URL must not change the dedicated backend's tool policy."""
    process_env = {"MINDROOM_WORKER_BACKEND": backend}
    if with_static_url:
        process_env["MINDROOM_SANDBOX_PROXY_URL"] = "http://unused-static.invalid"
    agent = _create_routing_agent(tmp_path, process_env)

    assert (await _invoke(agent, "run_shell_command", args=["unused"])).endswith("worker:shell")
    assert await _invoke(agent, "add", a=1, b=2) == "local:calculator"


@pytest.mark.parametrize("worker_tools", [None, ["reasoning"]])
def test_reasoning_retains_primary_run_state(
    tmp_path: Path,
    proxy_targets: list[ResolvedWorkerTarget | None],
    worker_tools: list[str] | None,
) -> None:
    """Broad or explicit routing must preserve the caller's reasoning scratchpad."""
    agent = _create_routing_agent(
        tmp_path,
        {"MINDROOM_SANDBOX_EXECUTION_MODE": "all", "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid"},
        agent_settings={"tools": ["reasoning"], "worker_tools": worker_tools},
    )
    toolkit = next(tool for tool in agent.tools or [] if isinstance(tool, Toolkit) and "think" in tool.functions)
    function = toolkit.functions["think"]
    context = RunContext(run_id="reasoning-run", session_id="reasoning-session", session_state={})
    function._run_context = context
    result = FunctionCall(function=function, arguments={"title": "Step", "thought": "Preserve run state"}).execute()

    assert result.status == "success"
    assert context.session_state is not None
    assert context.session_state["reasoning_steps"]
    assert proxy_targets == []
    assert "No tools use a worker runtime." in (agent.role or "")


@pytest.mark.asyncio
@pytest.mark.usefixtures("proxy_targets")
@pytest.mark.parametrize(
    ("agent_settings", "defaults", "mode", "expected"),
    [
        pytest.param({}, {"worker_tools": ["shell"]}, "off", ("worker:shell", "local:calculator"), id="defaults-list"),
        pytest.param({"worker_tools": ["shell"]}, {}, "off", ("worker:shell", "local:calculator"), id="agent-list"),
        pytest.param(
            {"worker_tools": ["calculator"]},
            {"worker_tools": ["shell"]},
            "all",
            ("local:shell", "worker:calculator"),
            id="agent-over-defaults",
        ),
        pytest.param(
            {"worker_tools": []},
            {"worker_tools": ["shell"]},
            "all",
            ("local:shell", "local:calculator"),
            id="agent-empty",
        ),
        pytest.param({}, {"worker_tools": []}, "all", ("local:shell", "local:calculator"), id="defaults-empty"),
    ],
)
async def test_authored_worker_tools_retains_precedence(
    tmp_path: Path,
    agent_settings: dict[str, object],
    defaults: dict[str, object],
    mode: str,
    expected: tuple[str, str],
) -> None:
    """Agent lists override defaults, and authored lists override environment modes."""
    agent = _create_routing_agent(
        tmp_path,
        {"MINDROOM_SANDBOX_EXECUTION_MODE": mode, "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid"},
        agent_settings=agent_settings,
        defaults=defaults,
    )

    assert (await _invoke(agent, "run_shell_command", args=["unused"])).endswith(expected[0])
    assert await _invoke(agent, "add", a=1, b=2) == expected[1]


@pytest.mark.asyncio
@pytest.mark.usefixtures("proxy_targets")
async def test_runner_mode_keeps_real_agent_tools_local(tmp_path: Path) -> None:
    """A runner must never wrap tools recursively, even with explicit routing."""
    agent = _create_routing_agent(
        tmp_path,
        {"MINDROOM_SANDBOX_RUNNER_MODE": "true", "MINDROOM_SANDBOX_EXECUTION_MODE": "all"},
        agent_settings={"worker_tools": ["shell", "calculator"]},
    )

    assert (await _invoke(agent, "run_shell_command", args=["unused"])).endswith("local:shell")
    assert await _invoke(agent, "add", a=1, b=2) == "local:calculator"


@pytest.mark.asyncio
@pytest.mark.usefixtures("proxy_targets")
@pytest.mark.parametrize("worker_tools", [None, ["usage_stats"]])
async def test_primary_runtime_tool_stays_local_after_real_materialization(
    tmp_path: Path,
    worker_tools: list[str] | None,
) -> None:
    """Primary-only toolkit calls must not become remote under all or explicit routing."""
    agent = _create_routing_agent(
        tmp_path,
        {"MINDROOM_SANDBOX_EXECUTION_MODE": "all", "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid"},
        agent_settings={"tools": ["usage_stats"], "worker_tools": worker_tools},
    )

    result = json.loads(await _invoke(agent, "get_my_usage"))
    assert result["code"] == "context_unavailable"
    assert "No tools use a worker runtime." in (agent.role or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "worker_tools"),
    [
        pytest.param(None, None, id="proxy-url-default"),
        pytest.param("all", None, id="all-tools"),
        pytest.param("off", ["scheduler"], id="explicit-worker-list"),
    ],
)
async def test_room_context_tool_uses_primary_matrix_client(
    tmp_path: Path,
    proxy_targets: list[ResolvedWorkerTarget | None],
    mode: str | None,
    worker_tools: list[str] | None,
) -> None:
    """Worker routing must retain the live Matrix client needed to list schedules."""
    process_env = {"MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid"}
    if mode is not None:
        process_env["MINDROOM_SANDBOX_EXECUTION_MODE"] = mode
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env=process_env,
    )
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "ollama", "id": "test-model"}},
            "agents": {
                "routing": {
                    "display_name": "Routing",
                    "tools": ["scheduler"],
                    "worker_tools": worker_tools,
                    "include_default_tools": False,
                    "memory_backend": "none",
                },
            },
        },
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths, usernames={"router": "router", "routing": "routing"})
    target = MessageTarget.resolve(room_id="!schedules:localhost", thread_id="$thread", reply_to_event_id=None)
    client = make_matrix_client_mock()
    client.room_get_state.return_value = nio.RoomGetStateResponse(events=[], room_id=target.room_id)
    context = make_test_tool_runtime_context(
        agent_name="routing",
        target=target,
        requester_id="@user:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(),
    )
    agent = create_agent(
        "routing",
        config,
        runtime_paths,
        execution_identity=build_execution_identity_from_runtime_context(context),
        include_interactive_questions=False,
        persist_runtime_state=False,
        supports_native_tool_approval=True,
    )
    toolkit = next(
        tool for tool in agent.tools or [] if isinstance(tool, Toolkit) and "list_schedules" in tool.async_functions
    )
    with tool_runtime_context(context):
        result = await FunctionCall(function=toolkit.async_functions["list_schedules"], arguments={}).aexecute()

    assert result.status == "success"
    assert result.result == "No scheduled tasks found."
    client.room_get_state.assert_awaited_once_with(target.room_id)
    assert proxy_targets == []
    assert "No tools use a worker runtime." in (agent.role or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
@pytest.mark.parametrize("unsafe_local", [False, True])
async def test_dedicated_backend_without_configuration_still_fails_closed(
    tmp_path: Path,
    backend: str,
    unsafe_local: bool,
) -> None:
    """Missing dedicated-worker configuration must not fall back to host execution."""
    agent = _create_routing_agent(
        tmp_path,
        {
            "MINDROOM_WORKER_BACKEND": backend,
            "MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS": str(unsafe_local).lower(),
        },
    )

    with pytest.raises(WorkerBackendError):
        await _invoke(agent, "run_shell_command", args=["unused"])


@pytest.mark.asyncio
async def test_authored_worker_tools_fails_closed_without_static_proxy(tmp_path: Path) -> None:
    """The off mode cannot silently undo an explicit YAML worker requirement."""
    agent = _create_routing_agent(
        tmp_path,
        {"MINDROOM_SANDBOX_EXECUTION_MODE": "off"},
        agent_settings={"worker_tools": ["shell"]},
    )

    with pytest.raises(RuntimeError, match="MINDROOM_SANDBOX_PROXY_URL"):
        await _invoke(agent, "run_shell_command", args=["unused"])


@pytest.mark.asyncio
async def test_unsafe_local_flag_remains_limited_to_default_execution_tools(tmp_path: Path) -> None:
    """The explicit unsafe static fallback applies to shell, but not arbitrary tools."""
    agent = _create_routing_agent(
        tmp_path,
        {"MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS": "true"},
        agent_settings={"worker_tools": ["shell", "calculator"]},
    )

    assert (await _invoke(agent, "run_shell_command", args=["unused"])).endswith("local:shell")
    with pytest.raises(RuntimeError, match="MINDROOM_SANDBOX_PROXY_URL"):
        await _invoke(agent, "add", a=1, b=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "off", "all", "selective"])
async def test_dedicated_routing_preserves_requester_isolation(
    tmp_path: Path,
    proxy_targets: list[ResolvedWorkerTarget | None],
    mode: str | None,
) -> None:
    """Environment routing retains the resolved user-agent worker boundary."""
    process_env = {"MINDROOM_WORKER_BACKEND": "kubernetes"}
    if mode is not None:
        process_env["MINDROOM_SANDBOX_EXECUTION_MODE"] = mode
    if mode == "selective":
        process_env["MINDROOM_SANDBOX_PROXY_TOOLS"] = "shell"
    for requester_id in ("@alice:example.com", "@bob:example.com"):
        identity = ToolExecutionIdentity(
            channel="openai_compat",
            agent_name="routing",
            requester_id=requester_id,
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
            session_id=requester_id,
        )
        agent = _create_routing_agent(
            tmp_path,
            process_env,
            agent_settings={"worker_scope": "user_agent"},
            execution_identity=identity,
        )
        result = await _invoke(agent, "run_shell_command", args=["unused"])
        assert result.endswith("local:shell" if mode == "off" else "worker:shell")

    if mode == "off":
        assert proxy_targets == []
    else:
        assert len(proxy_targets) == 2
        alice, bob = proxy_targets
        assert alice is not None
        assert bob is not None
        assert alice.worker_scope == bob.worker_scope == "user_agent"
        assert alice.worker_key is not None
        assert bob.worker_key is not None
        assert alice.worker_key != bob.worker_key
        assert alice.execution_identity is not None
        assert alice.execution_identity.requester_id == "@alice:example.com"
        assert bob.execution_identity is not None
        assert bob.execution_identity.requester_id == "@bob:example.com"


@pytest.mark.parametrize("backend", ["static_runner", "docker", "kubernetes"])
def test_unknown_execution_mode_rejected_before_tool_materialization(
    tmp_path: Path,
    backend: str,
) -> None:
    """A typo must not silently make an isolation configuration execute locally."""
    with pytest.raises(ValueError, match="MINDROOM_SANDBOX_EXECUTION_MODE"):
        _create_routing_agent(
            tmp_path,
            {
                "MINDROOM_WORKER_BACKEND": backend,
                "MINDROOM_SANDBOX_EXECUTION_MODE": "sandobx_all",
            },
        )
