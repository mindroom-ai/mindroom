"""Tests for workflow attempts on the script runtime: placement, launch entry, limits, grants and approval.

Level: pure (placement, grants) and local integration (the real manager, store, broker and shim, with a fake
worker client and supervisor).
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest

import mindroom.script_runs.manager as manager_module
from mindroom.approval.tool_approval import ToolApprovalDecision
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.script_runs import launch_contract
from mindroom.script_runs.broker import ScriptToolBroker, ScriptToolCallRequest
from mindroom.script_runs.manager import (
    ScriptRunLimits,
    ScriptRunManagerError,
    workflow_placement,
)
from mindroom.script_runs.models import ScriptCallState, ScriptToolGrant
from mindroom.script_runs.policy import live_workflow_grant, resolve_script_launch_grants
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend
from tests.conftest import bound_worker_launch_config
from tests.test_script_run_manager import _config, _context, _fake_local_supervisor, _manager, _runtime_paths, _started
from tests.test_script_run_policy import _context_for_config
from tests.test_script_tool_broker import _call_through_gateway, _RuntimeResolver
from tests.test_shell_supervisor import _run_shim, _shim_workspace
from tests.workflow_helpers import SCRIPT_RUN_ID, TOKEN, FakeWorkflowGateway

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.script_runs.models import ScriptCallRecord
    from mindroom.shell.execution import ShellRunResult
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

RUN = "wf-" + "c" * 32
_REFUSAL = (
    "Workflows run code. Route this agent to a worker, "
    "or set defaults.allow_shell_without_worker: true to run it on this machine."
)


@pytest.fixture(autouse=True)
def _applied_config() -> Iterator[None]:
    """Bind the applied config script workers launch from, as `mindroom run` does."""
    with bound_worker_launch_config(_config()):
        yield


def _shell_allowed(config: Config) -> Config:
    return config.model_copy(
        update={"defaults": config.defaults.model_copy(update={"allow_shell_without_worker": True})},
    )


def _workflow_grants(_context: ToolRuntimeContext, *, workflow: bool = False) -> tuple[ScriptToolGrant, ...]:
    assert workflow
    return (ScriptToolGrant("calculator", "add"),)


def test_no_backend_and_shell_allowed_runs_locally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's switch for running code as the primary's user also places workflows there."""
    monkeypatch.setattr(manager_module, "background_script_supervision_supported", lambda: True)
    paths = _runtime_paths(tmp_path, mode=None)
    assert workflow_placement(_shell_allowed(_config()), paths) == "local"


def test_no_backend_and_shell_not_allowed_is_refused_with_the_setting_named(tmp_path: Path) -> None:
    """Without a worker or the operator's switch, the refusal names both ways out."""
    assert workflow_placement(_config(), _runtime_paths(tmp_path, mode=None)) == _REFUSAL


def test_explicitly_local_sandbox_mode_runs_locally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandbox the operator turned off is the same statement as the shell switch."""
    monkeypatch.setattr(manager_module, "background_script_supervision_supported", lambda: True)
    assert workflow_placement(_config(), _runtime_paths(tmp_path, mode="local")) == "local"


@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
def test_configured_worker_backend_places_the_attempt_on_a_worker(tmp_path: Path, backend: str) -> None:
    """A dedicated backend wins over the shell switch, so a workflow never runs on the host beside it."""
    paths = _runtime_paths(tmp_path, mode=None, backend=backend)
    assert workflow_placement(_shell_allowed(_config()), paths) == "worker"


def test_sandbox_proxy_places_the_attempt_on_a_worker(tmp_path: Path) -> None:
    """A configured sandbox proxy routes the attempt away from the host as well."""
    paths = _runtime_paths(tmp_path, mode=None)
    paths = replace(paths, process_env={**paths.process_env, "MINDROOM_SANDBOX_PROXY_URL": "http://runner.test"})
    assert workflow_placement(_shell_allowed(_config()), paths) == "worker"


@pytest.mark.asyncio
async def test_configured_worker_that_is_down_fails_and_never_runs_locally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that cannot be reached fails the launch with its own error instead of falling back to the host."""
    manager, _backend, client = _manager(tmp_path, mode=None, backend="docker")
    manager.worker_backend = None
    manager.grant_resolver = _workflow_grants
    local_launches: list[object] = []

    async def launch_local(*args: object, **kwargs: object) -> ShellRunResult:
        local_launches.append(kwargs["argv"])
        return await _started(*args, **kwargs)

    _fake_local_supervisor(monkeypatch, launch=launch_local)
    context = _context(tmp_path, mode=None, backend="docker")
    context = replace(context, config=_shell_allowed(context.config))

    with pytest.raises(ScriptRunManagerError, match="worker backend is unavailable"):
        await manager.run(context, source="print('ok')\n", workflow_run_id=RUN)

    assert local_launches == []
    assert client.launched_run_ids == []
    assert manager.store.list_runs() == []


@pytest.mark.asyncio
async def test_workflow_attempt_has_no_recovery_signature_and_crosses_the_worker_wire_as_a_workflow(
    tmp_path: Path,
) -> None:
    """No new primary adopts a workflow attempt's process, and the worker is told to run the SDK entry."""
    manager, _backend, client = _manager(tmp_path, backend="kubernetes", isolated_script_gateway=True)
    manager.grant_resolver = _workflow_grants
    context = _context(tmp_path, backend="kubernetes", isolated_script_gateway=True)

    attempt = await manager.run(context, source="print('ok')\n", workflow_run_id=RUN)

    stored = manager.store.get_run(attempt.run_id)
    assert (stored.workflow_run_id, stored.entry, stored.recovery_signature) == (RUN, "workflow", None)
    assert stored.grants == (ScriptToolGrant("calculator", "add"),)
    assert client.launch_entries == ["workflow"]


@pytest.mark.asyncio
async def test_local_workflow_attempt_runs_the_shim_with_a_fixed_hash_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launched command stays the shim; the entry and a fixed hash seed reach it through its environment."""
    manager, _backend, _client = _manager(tmp_path, mode="local")
    manager.grant_resolver = _workflow_grants
    launches: list[dict[str, object]] = []

    async def launch_local(*args: object, **kwargs: object) -> ShellRunResult:
        launches.append(kwargs)
        return await _started(*args, **kwargs)

    _fake_local_supervisor(monkeypatch, launch=launch_local)

    attempt = await manager.run(_context(tmp_path, mode="local"), source="print('ok')\n", workflow_run_id=RUN)

    environment = cast("dict[str, str]", launches[0]["env"])
    assert cast("list[str]", launches[0]["argv"])[3:5] == ["-m", "mindroom.script_runs.shim"]
    assert environment["PYTHONHASHSEED"] == "0"
    assert environment[launch_contract.ENTRY_ENV] == "workflow"
    assert (attempt.local_unsafe, attempt.workflow_run_id) == (True, RUN)


@pytest.mark.asyncio
async def test_plain_script_placement_and_launch_are_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell switch places no plain script on the host, and a plain script keeps its entry, seed and limit."""
    manager, _backend, _client = _manager(tmp_path, mode=None)
    manager.worker_backend = None
    context = _context(tmp_path, mode=None)
    with pytest.raises(ScriptRunManagerError, match="require a worker or an explicitly disabled sandbox"):
        await manager.run(replace(context, config=_shell_allowed(context.config)), source="print('ok')\n")

    local_manager, _local_backend, _local_client = _manager(tmp_path / "local", mode="local")
    launches: list[dict[str, object]] = []

    async def launch_local(*args: object, **kwargs: object) -> ShellRunResult:
        launches.append(kwargs)
        return await _started(*args, **kwargs)

    _fake_local_supervisor(monkeypatch, launch=launch_local)
    run = await local_manager.run(_context(tmp_path / "local", mode="local"), source="print('ok')\n")

    environment = cast("dict[str, str]", launches[0]["env"])
    assert environment[launch_contract.ENTRY_ENV] == "script"
    assert environment.get("PYTHONHASHSEED") == os.environ.get("PYTHONHASHSEED")
    assert (run.workflow_run_id, run.entry, run.max_runtime_seconds) == (None, "script", 24 * 60 * 60)


@pytest.mark.asyncio
async def test_static_runner_and_macos_local_runs_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared static runner keeps the script runtime's refusal, and only Linux runs a workflow locally."""
    manager, _backend, client = _manager(tmp_path)
    manager.grant_resolver = _workflow_grants
    manager.worker_backend = StaticSandboxRunnerBackend(api_root="http://runner", auth_token="token")  # noqa: S106
    with pytest.raises(ScriptRunManagerError, match="dedicated Docker or isolated Kubernetes worker backend"):
        await manager.run(_context(tmp_path, backend="static"), source="print('ok')\n", workflow_run_id=RUN)

    monkeypatch.setattr(manager_module, "background_script_supervision_supported", lambda: False)
    local_manager, _local_backend, _local_client = _manager(tmp_path / "local", mode="local")
    local_manager.grant_resolver = _workflow_grants
    with pytest.raises(ScriptRunManagerError) as refused:
        await local_manager.run(_context(tmp_path / "local", mode="local"), source="print('ok')\n", workflow_run_id=RUN)

    assert str(refused.value) == _REFUSAL
    assert client.launched_run_ids == []
    assert manager.store.list_runs() == local_manager.store.list_runs() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("limits", [None, ScriptRunLimits(max_runtime_hours=1)])
async def test_attempt_limit_is_thirty_days_whatever_max_runtime_hours_is(
    tmp_path: Path,
    limits: ScriptRunLimits | None,
) -> None:
    """The attempt's process limit is fixed; the run's working time belongs to the engine."""
    manager, _backend, _client = _manager(tmp_path)
    manager.grant_resolver = _workflow_grants

    attempt = await manager.run(_context(tmp_path), source="print('ok')\n", workflow_run_id=RUN, limits=limits)

    assert attempt.max_runtime_seconds == 30 * 24 * 3600


async def _call_add(
    tmp_path: Path,
    *,
    workflow: bool,
    default: str,
    live_rules: tuple[ApprovalRuleConfig, ...] = (),
    decision: ToolApprovalDecision | None = None,
) -> tuple[ScriptCallRecord, list[str]]:
    """Launch one run under the operator's *default*, call `calculator.add` and return the receipt and the asks."""
    manager, _backend, worker_client = _manager(tmp_path)
    manager.grant_resolver = lambda _context, **_options: (ScriptToolGrant("calculator", "add"),)
    context = _context(tmp_path)
    run = await manager.run(context, source="print('ok')\n", workflow_run_id=RUN if workflow else None)
    # Tool routing stays independent of the faked script-process worker, so the calculator runs here.
    watcher = AgentConfig(display_name="Watcher", worker_scope="user_agent", worker_tools=[], tools=["calculator"])
    live_config = context.config.model_copy(
        update={
            "agents": {**context.config.agents, "watcher": watcher},
            "tool_approval": ToolApprovalConfig(default=default, rules=list(live_rules)),
        },
    )
    asks: list[str] = []
    broker = ScriptToolBroker(
        store=manager.store,
        runtime_resolver=_RuntimeResolver(
            replace(context, config_provider=lambda: live_config),
            approval_events=asks,
            approval_decision=decision,
            worker_id=run.worker_id,
        ),
    )
    broker.open_call_admission()
    request = ScriptToolCallRequest(
        run_id=run.run_id,
        call_id="call-1",
        grant=ScriptToolGrant("calculator", "add"),
        arguments={"a": 1, "b": 2},
    )
    token = worker_client.launch_paths[run.run_id][1].read_text(encoding="utf-8")
    return await _call_through_gateway(broker, request, token), asks


@pytest.mark.asyncio
async def test_default_require_approval_asks_for_an_unlisted_function(tmp_path: Path) -> None:
    """Under the operator's `require_approval` default a workflow's call asks, as the owner's own call would."""
    receipt, asks = await _call_add(tmp_path, workflow=True, default="require_approval")

    assert len(asks) == 1
    assert receipt.state is ScriptCallState.COMPLETED


@pytest.mark.asyncio
async def test_default_auto_approve_runs_a_workflow_call_without_asking_and_a_plain_script_still_asks(
    tmp_path: Path,
) -> None:
    """A workflow answers to the operator's rules unmodified; a plain script keeps its ask-by-default overlay."""
    receipt, asks = await _call_add(tmp_path / "workflow", workflow=True, default="auto_approve")
    _script_receipt, script_asks = await _call_add(tmp_path / "script", workflow=False, default="auto_approve")

    assert asks == []
    assert receipt.state is ScriptCallState.COMPLETED
    assert receipt.result == '{"operation": "addition", "result": 3.0}'
    assert len(script_asks) == 1


@pytest.mark.asyncio
async def test_rule_added_during_a_run_is_honoured_and_its_denial_stops_the_call(tmp_path: Path) -> None:
    """A rule the operator adds after the launch decides the run's next call."""
    receipt, asks = await _call_add(
        tmp_path,
        workflow=True,
        default="auto_approve",
        live_rules=(ApprovalRuleConfig(match="add", action="require_approval"),),
        decision=ToolApprovalDecision(approved=False, reason="Not this sum."),
    )

    assert len(asks) == 1
    assert receipt.state is ScriptCallState.FAILED
    assert receipt.error == {"kind": "approval_denied", "message": "Not this sum.", "retryable": False}


def _owner_context(tmp_path: Path, tools: list[str]) -> ToolRuntimeContext:
    return _context_for_config(
        tmp_path,
        Config(
            agents={
                "general": AgentConfig(display_name="General Agent", tools=tools, delegate_to=["helper"]),
                "helper": AgentConfig(display_name="Helper", tools=[]),
            },
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5-5")},
        ),
    )


def test_grants_include_memory_and_delegate_but_not_compact_context(tmp_path: Path) -> None:
    """A workflow may call what a plain script may not, except the tools that act on a calling turn."""
    context = _owner_context(tmp_path, ["calculator", "memory", "delegate", "compact_context", "dynamic_tools"])

    workflow_toolkits = {grant.toolkit_name for grant in resolve_script_launch_grants(context, workflow=True)}
    script_toolkits = {grant.toolkit_name for grant in resolve_script_launch_grants(context)}

    assert {"calculator", "memory", "delegate"} <= workflow_toolkits
    assert not {"compact_context", "dynamic_tools", "slack"} & workflow_toolkits
    assert script_toolkits == {"calculator"}


def test_tool_removed_after_launch_is_refused_at_the_call(tmp_path: Path) -> None:
    """The grant is resolved from the owner's live tools at every call, in both directions."""
    context = _owner_context(tmp_path, ["calculator"])
    assert live_workflow_grant(context, "calculator", "add") == ScriptToolGrant("calculator", "add")
    assert live_workflow_grant(context, "memory", "add_memory") is None
    assert live_workflow_grant(context, "calculator", "no_such_function") is None

    live = _owner_context(tmp_path, ["memory"])
    reloaded = replace(context, config_provider=lambda: live.config)

    assert live_workflow_grant(reloaded, "calculator", "add") is None
    assert live_workflow_grant(reloaded, "memory", "add_memory") == ScriptToolGrant("memory", "add_memory")
    assert live_workflow_grant(reloaded, "compact_context", "compact_context") is None


@pytest.mark.asyncio
async def test_validated_shim_hands_a_workflow_to_the_sdk_and_removes_the_token(tmp_path: Path) -> None:
    """The shim checks the snapshot exactly as for a script, then calls `main(args)` through the SDK."""
    workspace, source_path, token_path = _shim_workspace(
        tmp_path,
        source='def main(args):\n    return {"name": __name__, "topic": args["topic"]}\n',
        capability=TOKEN,
    )
    with FakeWorkflowGateway(tmp_path / "unused-capability") as gateway:
        gateway.args = {"topic": "tides"}
        env = {
            launch_contract.ENTRY_ENV: "workflow",
            launch_contract.GATEWAY_URL_ENV: gateway.url,
            launch_contract.RUN_ID_ENV: SCRIPT_RUN_ID,
        }
        await _run_shim(workspace, source_path, token_path, env=env)

        assert gateway.finished == {"result": {"name": "__workflow__", "topic": "tides"}}
        assert not token_path.exists()

        token_path.write_text(TOKEN, encoding="utf-8")
        gateway.finished = None
        requests_before = len(gateway.requests)
        output = await _run_shim(
            workspace,
            source_path,
            token_path,
            env={**env, launch_contract.SOURCE_DIGEST_ENV: "0" * 64},
        )

        assert "Script source digest does not match the launch receipt." in output
        assert len(gateway.requests) == requests_before
        assert gateway.finished is None
        assert not token_path.exists()


@pytest.mark.asyncio
async def test_workflow_attempts_and_plain_scripts_are_admitted_from_separate_counts(tmp_path: Path) -> None:
    """A full set of workflow attempts leaves the owner's plain scripts their slots, and the other way round."""
    manager, _backend, _client = _manager(tmp_path)
    manager.grant_resolver = lambda _context, **_options: (ScriptToolGrant("calculator", "add"),)
    context = _context(tmp_path)
    one = ScriptRunLimits(max_concurrent_runs=1)

    attempt = await manager.run(context, source="print('ok')\n", workflow_run_id=RUN, limits=one)
    script = await manager.run(context, source="print('ok')\n", limits=one)

    with pytest.raises(ScriptRunManagerError, match="concurrent-run limit"):
        await manager.run(context, source="print('ok')\n", workflow_run_id="wf-" + "d" * 32, limits=one)
    with pytest.raises(ScriptRunManagerError, match="concurrent-run limit"):
        await manager.run(context, source="print('ok')\n", limits=one)
    assert [run.run_id for run in manager.store.list_runs(include_finished=False)] == [script.run_id, attempt.run_id]


@pytest.mark.asyncio
async def test_plain_scripts_at_their_limit_do_not_refuse_a_workflow_attempt(tmp_path: Path) -> None:
    """The plain-script limit counts plain scripts only, so a workflow attempt still starts beside a full set."""
    manager, _backend, _client = _manager(tmp_path)
    manager.grant_resolver = lambda _context, **_options: (ScriptToolGrant("calculator", "add"),)
    context = _context(tmp_path)

    for _ in range(3):
        await manager.run(context, source="print('ok')\n")
    attempt = await manager.run(context, source="print('ok')\n", workflow_run_id=RUN)

    assert attempt.workflow_run_id == RUN
    with pytest.raises(ScriptRunManagerError, match="concurrent-run limit"):
        await manager.run(context, source="print('ok')\n")
