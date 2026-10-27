"""`tool:environment` hooks give one tool call's shell commands their variables, locally and in a worker."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import nullcontext
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom import constants
from mindroom.agent_cli.shell_contract import AgentCliShellEnv, bound_agent_cli_shell_env
from mindroom.api import sandbox_runner
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import resolve_primary_runtime_paths, resolve_runtime_paths
from mindroom.egress_broker import service as egress_service
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
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import reload_plugins
from mindroom.tool_system.runtime_context import ToolDispatchContext, tool_runtime_context
from mindroom.tool_system.tool_environment import ToolEnvironment
from mindroom.tool_system.tool_hooks import build_tool_hook_middleware
from mindroom.workers.backends import local as local_workers
from mindroom.workers.backends._dedicated_worker_common import build_dedicated_worker_runtime_paths
from mindroom.workers.models import WorkerHandle
from tests.api.test_sandbox_runner_api import SANDBOX_TOKEN, _write_general_agent_config
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
# MindRoom's own variables: the shell tool passes PLUGIN_SECRET by configuration, the others only when asked, and
# never the control variables, which no command may get.
_CONTROL_NAMES = ("MINDROOM_CREDENTIALS_ENCRYPTION_KEY", "MINDROOM_SANDBOX_PROXY_TOKEN")
# A well-formed key, since the runtime refuses to start with a malformed one.
_ENCRYPTION_KEY = "cHJpbWFyeS1jcmVkZW50aWFscy1rZXktMzItYnl0ZXM="
_PROCESS_ENV = {
    "PLUGIN_SECRET": "primary-secret",
    "PLUGIN_SHARED_KEY": "primary-shared",
    "DATABASE_URL": "postgres://primary",
    "MINDROOM_CREDENTIALS_ENCRYPTION_KEY": _ENCRYPTION_KEY,
    "MINDROOM_SANDBOX_PROXY_TOKEN": "primary-proxy-token",
}
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
    # The agent workspace a worker command runs in; None for a local shell.
    workspace: Path | None = None

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
        workspace=workspace,
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
async def test_a_removal_also_keeps_an_inherited_variable_from_the_command(
    shell: _Shell,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A variable the command would inherit, such as the proxy MindRoom or the worker runs with, stays removed."""
    monkeypatch.setenv("HTTPS_PROXY", "http://operator-proxy:3128")

    @hook(EVENT_TOOL_ENVIRONMENT)
    async def connect_directly(ctx: ToolEnvironmentContext) -> None:
        ctx.unset_env("HTTPS_PROXY")

    command = 'printf "%s" "${HTTPS_PROXY-unset}"'
    inherited = await shell.run(HookRegistry.empty(), command)
    removed = await shell.run(_registry(connect_directly), command)

    assert (inherited, removed) == ("http://operator-proxy:3128", "unset")
    if shell.sent:
        assert shell.sent[-1]["execution_env_unset"] == ["HTTPS_PROXY"]


@pytest.mark.parametrize("pattern", ["MINDROOM_*", "*"])
@pytest.mark.asyncio
async def test_pass_through_never_hands_a_command_mindroom_s_control_variables(shell: _Shell, pattern: str) -> None:
    """Even a pass-through matching every variable keeps the encryption key and sandbox token from the command."""

    @hook(EVENT_TOOL_ENVIRONMENT)
    async def everything(ctx: ToolEnvironmentContext) -> None:
        ctx.pass_through(pattern)

    command = 'printf "%s|%s|%s" "${MINDROOM_CREDENTIALS_ENCRYPTION_KEY-unset}" "${MINDROOM_SANDBOX_PROXY_TOKEN-unset}" "${PLUGIN_SHARED_KEY-unset}"'

    assert (
        await shell.run(_registry(everything), command)
        == f"unset|unset|{'primary-shared' if pattern == '*' else 'unset'}"
    )
    if shell.sent:
        request = shell.sent[-1]
        assert set(_CONTROL_NAMES).isdisjoint(request["execution_env"])
        assert _ENCRYPTION_KEY not in json.dumps(request)


@pytest.mark.asyncio
async def test_a_hook_s_values_win_over_the_workspace_script_of_a_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A value or removal a hook makes holds even where the workspace's `.mindroom/worker-env.sh` exports the name."""
    shell = _worker_tool(monkeypatch, tmp_path, "shell", "run_shell_command")
    assert shell.workspace is not None
    script = shell.workspace / ".mindroom" / "worker-env.sh"
    script.parent.mkdir()
    script.write_text(
        "export GITHUB_TOKEN=from-script PLUGIN_SECRET=from-script PLUGIN_SCRIPT_ONLY=from-script\n",
        encoding="utf-8",
    )

    @hook(EVENT_TOOL_ENVIRONMENT)
    async def fresh_token(ctx: ToolEnvironmentContext) -> None:
        ctx.set_env("GITHUB_TOKEN", "from-hook")
        ctx.unset_env("PLUGIN_SECRET")

    command = 'printf "%s|%s|%s" "${GITHUB_TOKEN-unset}" "${PLUGIN_SECRET-unset}" "${PLUGIN_SCRIPT_ONLY-unset}"'
    script_only = await shell.run(HookRegistry.empty(), command)
    with_hook = await shell.run(_registry(fresh_token), command)

    assert script_only == "from-script|from-script|from-script"
    assert with_hook == "from-hook|unset|from-script"
    assert shell.sent[-1]["execution_env_set"] == ["GITHUB_TOKEN"]


@pytest.mark.asyncio
async def test_database_url_variables_reach_the_command_wherever_it_runs(shell: _Shell) -> None:
    """Names a worker otherwise keeps from its commands, such as database URLs, reach one a hook gives them to."""

    @hook(EVENT_TOOL_ENVIRONMENT)
    async def database(ctx: ToolEnvironmentContext) -> None:
        ctx.set_env("APP_DATABASE_URL", "postgres://app")
        ctx.pass_through("DATABASE_URL")

    output = await shell.run(_registry(database), 'printf "%s|%s" "${APP_DATABASE_URL-unset}" "${DATABASE_URL-unset}"')

    assert output == "postgres://app|postgres://primary"


def _broker_sets_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_proxy, "egress_broker_enabled", lambda _runtime_paths: True)
    monkeypatch.setattr(sandbox_proxy, "egress_agent_scope", lambda _config, agent_name: agent_name)
    monkeypatch.setattr(egress_service, "execution_env_for_worker", lambda *_args, **_kwargs: {"HTTPS_PROXY": "egress"})


def _worker_sets_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(constants, "worker_proxy_execution_env", lambda _env: {"HTTPS_PROXY": "egress"})


def _remove_the_proxy(ctx: ToolEnvironmentContext) -> None:
    ctx.unset_env("HTTPS_PROXY")


def _set_the_proxy(ctx: ToolEnvironmentContext) -> None:
    ctx.set_env("HTTPS_PROXY", "from-hook")


@pytest.mark.parametrize("change", [_remove_the_proxy, _set_the_proxy], ids=["removed", "set"])
@pytest.mark.parametrize("egress", [_broker_sets_the_proxy, _worker_sets_the_proxy], ids=["broker", "worker-egress"])
@pytest.mark.asyncio
async def test_the_egress_proxy_a_worker_call_gets_wins_over_a_hook(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    egress: Callable[[pytest.MonkeyPatch], None],
    change: Callable[[ToolEnvironmentContext], None],
) -> None:
    """MindRoom's egress proxy for the call, set by the primary's broker or by the worker, applies over a hook's change."""
    shell = _worker_tool(monkeypatch, tmp_path, "shell", "run_shell_command")
    egress(monkeypatch)

    @hook(EVENT_TOOL_ENVIRONMENT)
    async def proxy(ctx: ToolEnvironmentContext) -> None:
        change(ctx)

    assert await shell.run(_registry(proxy), 'printf "%s" "${HTTPS_PROXY-unset}"') == "egress"


@pytest.mark.asyncio
async def test_a_removal_leaves_the_workspace_variables_of_a_worker_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A worker command keeps the workspace as its home even when a hook removes the workspace variables."""
    shell = _worker_tool(monkeypatch, tmp_path, "shell", "run_shell_command")

    @hook(EVENT_TOOL_ENVIRONMENT)
    async def remove_home(ctx: ToolEnvironmentContext) -> None:
        ctx.unset_env("HOME")
        ctx.unset_env("MINDROOM_AGENT_WORKSPACE")

    command = 'printf "%s|%s" "${HOME-unset}" "${MINDROOM_AGENT_WORKSPACE-unset}"'
    workspace = await shell.run(HookRegistry.empty(), command)

    assert await shell.run(_registry(remove_home), command) == workspace
    assert "unset" not in workspace


@pytest.mark.asyncio
async def test_a_hook_s_path_keeps_the_entries_a_dedicated_worker_adds(tmp_path: Path) -> None:
    """A dedicated worker still puts its virtualenv first on a command's `PATH` when a hook sets `PATH`.

    The hook's `PATH` also wins over the one the workspace's `.mindroom/worker-env.sh` exports.
    """
    config_path = tmp_path / "config.yaml"
    _write_general_agent_config(config_path)
    worker_key = "v1:default:unscoped:code"
    worker_runtime = build_dedicated_worker_runtime_paths(
        runtime_paths=resolve_primary_runtime_paths(config_path=config_path, storage_path=tmp_path / "storage"),
        backend_name="Docker",
        worker_key=worker_key,
        config_path=config_path,
        dedicated_root=tmp_path / "worker",
        worker_port=8766,
        shared_storage_root=str(tmp_path / "shared-storage"),
        extra_env={},
    )
    worker_paths = local_workers.local_worker_state_paths_for_root(tmp_path / "worker-root")
    venv_tool = worker_paths.venv_dir / "bin" / "worker-venv-tool"
    venv_tool.parent.mkdir(parents=True)
    venv_tool.write_text("#!/bin/sh\necho from-venv\n", encoding="utf-8")
    venv_tool.chmod(0o755)
    workspace = tmp_path / "workspace"
    (workspace / ".mindroom").mkdir(parents=True)
    (workspace / ".mindroom" / "worker-env.sh").write_text('export PATH="/script/bin:$PATH"\n', encoding="utf-8")
    handle = WorkerHandle(
        worker_id="worker-1",
        worker_key=worker_key,
        endpoint="http://127.0.0.1:8766",
        auth_token=SANDBOX_TOKEN,
        status="ready",
        backend_name="docker",
        last_used_at=0.0,
        created_at=0.0,
    )
    hook_path = os.pathsep.join(("/plugin/bin", os.environ["PATH"]))
    prepared = sandbox_runner._prepare_execute_request(
        sandbox_runner.SandboxRunnerExecuteRequest(
            tool_name="shell",
            function_name="run_shell_command",
            worker_key=worker_key,
            args=[["bash", "-c", 'printf "%s\\n" "$PATH"; worker-venv-tool']],
            execution_env={"PATH": hook_path},
            execution_env_set=["PATH"],
        ),
        worker_runtime,
        prepared_worker=sandbox_runner.sandbox_worker_prep.PreparedWorkerRequest(
            handle=handle,
            paths=worker_paths,
            runtime_overrides={"base_dir": str(workspace)},
        ),
    )

    response = await sandbox_runner._execute_prepared_request_inprocess(
        prepared.request,
        prepared.runtime_paths,
        Config(),
    )

    assert response.ok, response.error
    command_path, tool_output = str(response.result).splitlines()[-2:]
    assert command_path.split(os.pathsep)[:2] == [str(venv_tool.parent), "/plugin/bin"]
    assert "/script/bin" not in command_path
    assert tool_output == "from-venv"


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


def test_a_later_environment_applies_over_an_earlier_one() -> None:
    """Later values and removals win, pass-throughs add up, and a later launcher directory comes first on `PATH`."""
    earlier = ToolEnvironment(values={"A": "1", "B": "1"}, pass_through=("X_*",), path_prepend=("/earlier",))
    later = ToolEnvironment(values={"B": None, "C": "3"}, pass_through=("Y_*",), path_prepend=("/later",))

    assert earlier.then(later) == ToolEnvironment(
        values={"A": "1", "B": None, "C": "3"},
        pass_through=("X_*", "Y_*"),
        path_prepend=("/later", "/earlier"),
    )


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
    with pytest.raises(ValueError, match="PATH cannot be removed"):
        ctx.unset_env("PATH")
