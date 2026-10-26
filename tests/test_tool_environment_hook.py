"""`tool:environment` hooks give one tool call's shell commands their variables, locally and in a worker."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.agent_cli.shell_contract import AgentCliShellEnv, bound_agent_cli_shell_env
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.hooks import (
    BUILTIN_EVENT_NAMES,
    EVENT_TOOL_BEFORE_CALL,
    EVENT_TOOL_ENVIRONMENT,
    HookRegistry,
    ToolBeforeCallContext,
    ToolEnvironmentContext,
    default_timeout_ms_for_event,
    hook,
)
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import reload_plugins
from mindroom.tool_system.runtime_context import ToolDispatchContext, tool_runtime_context
from mindroom.tool_system.tool_hooks import build_tool_hook_middleware
from tests.conftest import runtime_paths_for
from tests.test_plugins import _bind_runtime_paths, _preserved_plugin_loader_state
from tests.test_sandbox_proxy import (
    _MIND_EXECUTION_IDENTITY,
    _forward_proxy_to_seeded_runner,
    _live_primary_config,
    _mind_tool_runtime_context,
    _worker_target,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path

    from mindroom.tool_system.invocation import ToolCall, ToolNext
    from mindroom.tool_system.tool_function import ToolFunction, ToolOutcome
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_ALICE = _MIND_EXECUTION_IDENTITY
_BOB = replace(_MIND_EXECUTION_IDENTITY, requester_id="@bob:example.org", session_id="session-2")
# MindRoom's own variables: the shell tool passes PLUGIN_SECRET by configuration, PLUGIN_SHARED_KEY only when asked.
_PROCESS_ENV = {"PLUGIN_SECRET": "primary-secret", "PLUGIN_SHARED_KEY": "primary-shared"}
_PRINT_PLUGIN_ENV = 'printf "%s|%s|%s" "$PLUGIN_VALUE" "$PLUGIN_SECRET" "$PLUGIN_SHARED_KEY"'
_PRINT_GRANT_ENV = (
    'printf "%s|%s|%s|%s" "$MINDROOM_AGENT_CLI_URL" "$MINDROOM_AGENT_CLI_TOKEN" "$MINDROOM_AGENT_CLI_WINDOW" '
    '"$PLUGIN_VALUE"'
)


@hook(EVENT_TOOL_ENVIRONMENT)
async def _shell_environment(ctx: ToolEnvironmentContext) -> None:
    if ctx.registered_toolkit == "shell" and ctx.function_name == "run_shell_command":
        ctx.set_env("PLUGIN_VALUE", f"for {ctx.requester_id}")
        # The removal wins over the pass-through that also matches it.
        ctx.unset_env("PLUGIN_SECRET")
        ctx.pass_through("PLUGIN_*")


def _registry(*callbacks: object) -> HookRegistry:
    plugin = SimpleNamespace(
        name="environment-plugin",
        discovered_hooks=callbacks,
        discovered_automations=(),
        entry_config=PluginEntryConfig(path="./plugins/environment-plugin"),
        plugin_order=0,
    )
    return HookRegistry.from_plugins([plugin])


@dataclass
class _Shell:
    """The shell tool's command function, built to run in the primary or in a worker."""

    function: ToolFunction
    # The worker requests the calls sent; always empty for a local shell.
    sent: list[dict[str, Any]]
    runtime: Callable[[], AbstractContextManager[object]]

    async def call(
        self,
        registry: HookRegistry,
        arguments: dict[str, Any],
        *,
        identity: ToolExecutionIdentity = _ALICE,
        function: ToolFunction | None = None,
    ) -> ToolOutcome:
        """Run one tool call with ``arguments`` through the tool-call policy built from ``registry``."""
        policy = build_tool_hook_middleware(
            registry,
            agent_name="mind",
            dispatch_context=ToolDispatchContext(execution_identity=identity),
            config=None,
            runtime_paths=None,
        )
        with self.runtime():
            return await execute_tool_call(
                function or self.function,
                arguments,
                ToolCallContext(run_id=f"run-{identity.requester_id}", session_id=identity.session_id, user_id=None),
                middleware=(policy,),
            )

    async def run(self, registry: HookRegistry, command: str, **call: Any) -> str:  # noqa: ANN401
        """Run ``command`` as one shell call and return the line it printed."""
        outcome = await self.call(registry, {"args": ["bash", "-c", command]}, **call)
        assert not outcome.is_error, outcome.content
        return outcome.content.rsplit("\n", 1)[-1]


def _function(toolkit: object, key: str, name: str) -> ToolFunction:
    return next(function for function in adapt_toolkit(toolkit, key=key).functions if function.name == name)


def _local_shell(tmp_path: Path) -> _Shell:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env=_PROCESS_ENV,
    )
    toolkit = get_tool_by_name(
        "shell",
        runtime_paths,
        disable_sandbox_proxy=True,
        tool_config_overrides={"extra_env_passthrough": "PLUGIN_SECRET"},
        worker_target=None,
    )
    return _Shell(_function(toolkit, "shell", "run_shell_command"), sent=[], runtime=nullcontext)


def _worker_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool_name: str, function_name: str) -> _Shell:
    for name, value in _PROCESS_ENV.items():
        monkeypatch.setenv(name, value)
    primary_paths, workspace, sent = _forward_proxy_to_seeded_runner(
        monkeypatch,
        tmp_path,
        proxy_tools=frozenset({tool_name}),
    )
    live_config = _live_primary_config(primary_paths)
    workspace.mkdir(parents=True)
    toolkit = get_tool_by_name(
        tool_name,
        primary_paths,
        runtime_config=live_config,
        tool_config_overrides={"extra_env_passthrough": "PLUGIN_SECRET"} if tool_name == "shell" else None,
        tool_init_overrides={"base_dir": str(workspace)},
        worker_target=_worker_target(primary_paths, None, "mind", _MIND_EXECUTION_IDENTITY),
    )
    return _Shell(
        _function(toolkit, tool_name, function_name),
        sent=sent,
        runtime=lambda: tool_runtime_context(_mind_tool_runtime_context(primary_paths, live_config)),
    )


@pytest.fixture(params=["local", "worker"])
def shell(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Shell:
    """The shell tool running commands in the primary process or in a worker."""
    if request.param == "local":
        return _local_shell(tmp_path)
    return _worker_tool(monkeypatch, tmp_path, "shell", "run_shell_command")


def test_tool_environment_is_a_builtin_collector_event() -> None:
    """Plugins register for it like any built-in event, with the tool hooks' short default timeout."""
    assert EVENT_TOOL_ENVIRONMENT in BUILTIN_EVENT_NAMES
    assert default_timeout_ms_for_event(EVENT_TOOL_ENVIRONMENT) == 200


@pytest.mark.asyncio
async def test_a_hook_s_variables_reach_the_call_s_command_wherever_it_runs(shell: _Shell) -> None:
    """One collector sets, removes, and passes through variables for the command the same call runs."""
    without_hooks = await shell.run(HookRegistry.empty(), _PRINT_PLUGIN_ENV)
    with_hook = await shell.run(_registry(_shell_environment), _PRINT_PLUGIN_ENV)

    assert without_hooks == "|primary-secret|"
    assert with_hook == "for @alice:example.org||primary-shared"
    if shell.sent:
        execution_env = shell.sent[-1]["execution_env"]
        assert execution_env["PLUGIN_VALUE"] == "for @alice:example.org"
        assert execution_env["PLUGIN_SHARED_KEY"] == "primary-shared"
        assert "PLUGIN_SECRET" not in execution_env


@pytest.mark.asyncio
async def test_concurrent_calls_in_two_replies_each_get_only_their_own_variables(tmp_path: Path) -> None:
    """Both calls collect before either command starts, so a process-wide environment would mix them up."""
    shell = _local_shell(tmp_path)
    collected = 0
    both_collected = asyncio.Event()

    @hook(EVENT_TOOL_ENVIRONMENT, timeout_ms=5000)
    async def per_requester(ctx: ToolEnvironmentContext) -> None:
        nonlocal collected
        ctx.set_env("PLUGIN_VALUE", f"for {ctx.requester_id}")
        collected += 1
        if collected == 2:
            both_collected.set()
        await both_collected.wait()

    registry = _registry(per_requester)
    alice, bob = await asyncio.gather(
        shell.run(registry, _PRINT_PLUGIN_ENV, identity=_ALICE),
        shell.run(registry, _PRINT_PLUGIN_ENV, identity=_BOB),
    )

    assert alice == "for @alice:example.org|primary-secret|"
    assert bob == "for @bob:example.org|primary-secret|"


@pytest.mark.asyncio
async def test_hooks_merge_in_hook_order_and_a_failing_hook_loses_its_changes(tmp_path: Path) -> None:
    """A later hook's value wins, each hook edits only its own copy of the arguments, and a raising hook adds nothing."""
    shell = _local_shell(tmp_path)
    seen_arguments: list[dict[str, Any]] = []

    @hook(EVENT_TOOL_ENVIRONMENT, priority=10)
    async def first(ctx: ToolEnvironmentContext) -> None:
        ctx.set_env("PLUGIN_VALUE", "first")
        ctx.arguments.clear()

    @hook(EVENT_TOOL_ENVIRONMENT, priority=20)
    async def second(ctx: ToolEnvironmentContext) -> None:
        seen_arguments.append(ctx.arguments)
        ctx.set_env("PLUGIN_VALUE", "second")

    @hook(EVENT_TOOL_ENVIRONMENT, priority=30)
    async def broken(ctx: ToolEnvironmentContext) -> None:
        ctx.set_env("PLUGIN_VALUE", "broken")
        ctx.unset_env("PLUGIN_SECRET")
        msg = "hook bug"
        raise RuntimeError(msg)

    output = await shell.run(_registry(first, second, broken), _PRINT_PLUGIN_ENV)

    assert output == "second|primary-secret|"
    assert seen_arguments == [{"args": ["bash", "-c", _PRINT_PLUGIN_ENV]}]


@pytest.mark.asyncio
async def test_environment_hooks_follow_hook_scopes_and_skip_declined_calls(tmp_path: Path) -> None:
    """Agent and room scopes select the hooks a call runs, and a call a gate hook declines collects nothing."""
    shell = _local_shell(tmp_path)
    collected: list[str] = []

    @hook(EVENT_TOOL_ENVIRONMENT, agents=["mind"], rooms=[_ALICE.room_id])
    async def in_scope(ctx: ToolEnvironmentContext) -> None:
        collected.append(ctx.function_name)
        ctx.set_env("PLUGIN_VALUE", "in-scope")

    @hook(EVENT_TOOL_ENVIRONMENT, agents=["other"])
    async def other_agent(ctx: ToolEnvironmentContext) -> None:
        ctx.set_env("PLUGIN_VALUE", "other-agent")

    @hook(EVENT_TOOL_ENVIRONMENT, rooms=["!other:example.org"])
    async def other_room(ctx: ToolEnvironmentContext) -> None:
        ctx.set_env("PLUGIN_VALUE", "other-room")

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def decline_bob(ctx: ToolBeforeCallContext) -> None:
        if ctx.requester_id == _BOB.requester_id:
            ctx.decline("Not for Bob")

    registry = _registry(in_scope, other_agent, other_room, decline_bob)
    output = await shell.run(registry, _PRINT_PLUGIN_ENV)
    declined = await shell.call(registry, {"args": ["bash", "-c", _PRINT_PLUGIN_ENV]}, identity=_BOB)

    assert output == "in-scope|primary-secret|"
    assert declined.is_error
    assert collected == ["run_shell_command"]


def _write_environment_plugin(plugin_root: Path, value: str) -> None:
    (plugin_root / "hooks.py").write_text(
        "from mindroom.plugin_api import EVENT_TOOL_ENVIRONMENT, hook\n"
        "\n"
        "@hook(EVENT_TOOL_ENVIRONMENT)\n"
        "async def shell_value(ctx):\n"
        f"    ctx.set_env('PLUGIN_VALUE', {value!r})\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_a_plugin_reload_replaces_the_environment_hook(tmp_path: Path) -> None:
    """After a hot reload, the next call gets the edited hook's variables and none of the previous version's."""
    shell = _local_shell(tmp_path / "shell")
    plugin_root = tmp_path / "plugins" / "environment-reload"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "environment-reload", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    _write_environment_plugin(plugin_root, "before")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    config = _bind_runtime_paths(Config(plugins=["./plugins/environment-reload"]), config_path)

    with _preserved_plugin_loader_state():
        before = await shell.run(reload_plugins(config, runtime_paths_for(config)).hook_registry, _PRINT_PLUGIN_ENV)
        _write_environment_plugin(plugin_root, "after")
        after = await shell.run(reload_plugins(config, runtime_paths_for(config)).hook_registry, _PRINT_PLUGIN_ENV)

    assert before.startswith("before|")
    assert after.startswith("after|")


def _with_cli_grant(function: ToolFunction) -> ToolFunction:
    """Bind a response's CLI grant innermost around the call, as the agent CLI binds it around its shell leaf."""

    async def bind_grant(call: ToolCall, call_next: ToolNext) -> object:
        with bound_agent_cli_shell_env(AgentCliShellEnv("http://primary.test:8765", "response-grant"), window="bash-1"):
            return await call_next(call.arguments)

    return replace(function, middleware=(*function.middleware, bind_grant))


@pytest.mark.asyncio
async def test_the_cli_grant_reaches_the_command_over_a_hook_s_variables(shell: _Shell) -> None:
    """A response's CLI grant still reaches the command beside a hook's variables, and a hook cannot replace it."""

    @hook(EVENT_TOOL_ENVIRONMENT)
    async def override_grant(ctx: ToolEnvironmentContext) -> None:
        ctx.set_env("PLUGIN_VALUE", "from-hook")
        ctx.set_env("MINDROOM_AGENT_CLI_TOKEN", "from-hook")

    output = await shell.run(_registry(override_grant), _PRINT_GRANT_ENV, function=_with_cli_grant(shell.function))

    assert output == "http://primary.test:8765|response-grant|bash-1|from-hook"
    if shell.sent:
        assert shell.sent[-1]["execution_env"]["MINDROOM_AGENT_CLI_TOKEN"] == "response-grant"  # noqa: S105


@pytest.mark.asyncio
async def test_a_worker_call_of_another_tool_carries_no_hook_variables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only the shell tool's commands receive the changes, so a worker-routed `python` call sends none."""
    python = _worker_tool(monkeypatch, tmp_path, "python", "run_python_code")

    @hook(EVENT_TOOL_ENVIRONMENT)
    async def every_call(ctx: ToolEnvironmentContext) -> None:
        ctx.set_env("PLUGIN_VALUE", "from-hook")

    outcome = await python.call(_registry(every_call), {"code": "print('ran')"})

    assert not outcome.is_error, outcome.content
    assert "PLUGIN_VALUE" not in python.sent[-1].get("execution_env", {})


@pytest.mark.parametrize("name", ["", "A=B", "A B", "A,B", "A\0B"])
def test_a_hook_cannot_name_an_invalid_variable(name: str) -> None:
    """A name the shell tool's environment cannot hold fails in the hook that wrote it, not in the command."""
    ctx = ToolEnvironmentContext(
        function_name="run_shell_command",
        registered_toolkit="shell",
        arguments={},
        agent_name="mind",
        room_id=None,
        requester_id=None,
    )

    for change in (lambda: ctx.set_env(name, "x"), lambda: ctx.unset_env(name), lambda: ctx.pass_through(name)):
        with pytest.raises(ValueError, match="Invalid environment variable name"):
            change()
    with pytest.raises(ValueError, match="string value"):
        ctx.set_env("PLUGIN_VALUE", 1)
