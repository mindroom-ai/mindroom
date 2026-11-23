"""Callers can run a subagent in token-efficient minimal mode."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from mindroom import ai
from mindroom.agent_cli.protocol import ToolCallOperation, ToolListOperation
from mindroom.agent_cli.session import CONFIG_RELOAD_CALL, RELOAD_STOPPED, TurnToolRegistry
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.knowledge import RerankerConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig, ModelPricing
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation.direct import run_direct_child_turn
from mindroom.delegation.execution import delegation_call_job
from mindroom.delegation.legacy_snapshots import migrate_subagent_sessions
from mindroom.delegation.lifecycle import prepare_child_turn, reload_stopped_child_result
from mindroom.delegation.recovery import settled_subagent
from mindroom.delegation.state import SubagentPersona
from mindroom.llm.events import ReportedCall
from mindroom.llm.provider import ModelIdentity
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.response_turn import ResponseTurnContext
from mindroom.runtime_state import clear_api_server_address, set_api_server_address
from mindroom.tool_jobs.runtime import JobSpec
from mindroom.tool_system.agent_tool_calls import PreparedAgentToolCatalog
from mindroom.tool_system.agno_toolkits import toolkit_function
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from mindroom.turn_continuation import TURN_CONTINUATION_LIMIT
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call, usage
from tests.identity_helpers import entity_ids
from tests.minimal_agent_fixtures import (  # noqa: F401 - agent_cli_api is a pytest fixture
    agent_cli_api,
    cli_window,
    install_scripted_shell,
    offered_tools,
    settled_receipt,
    shell_cli_owner,
)
from tests.test_delegation_direct_audit import _identity
from tests.test_mode_commands import _CLI_DEPLOYMENT_ENV
from tests.tool_job_helpers import JOB_TEST_TIMEOUT, admit_delegation_spec, tool_job_runtime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.agent_cli.turn import LiveTurnTools
    from mindroom.agent_modes import AgentMode
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild, PersonaSourceKind
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_access import ToolKey
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_LONG_ROLE = "optional long role " * 200


def _config(*, helper_tools: list[str], approval: ToolApprovalConfig | None = None) -> Config:
    return Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["helper", "plain"]),
            "helper": AgentConfig(display_name="Helper", role=_LONG_ROLE, tools=helper_tools, memory_backend="file"),
            "plain": AgentConfig(display_name="Plain"),
        },
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
        **({"tool_approval": approval} if approval is not None else {}),
    )


def _paths(tmp_path: Path, env: dict[str, str]) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)


def _workspace(tmp_path: Path, agent_name: str) -> Path:
    """Return the workspace of a shared agent stored under ``tmp_path``."""
    return agent_workspace_root_path(tmp_path, agent_name)


def _live_context(config: Config, paths: RuntimePaths) -> ToolRuntimeContext:
    """Bind the managed Matrix response owner a minimal child's CLI requires."""
    context = _delegate_runtime_context(config, paths, execution_identity=_identity())
    return replace(
        context,
        # The parent's reply target stays on the child's runtime for its Matrix tools.
        target=replace(context.target, reply_to_event_id="$parent-reply"),
        orchestrator=SimpleNamespace(
            agent_cli_registry=TurnToolRegistry(),
            budgets=BudgetMonitor(runtime_paths=paths, config_provider=lambda: config),
        ),
        membership_turn_id="$turn",
        correlation_id="correlation",
    )


def _leader_turn(identity: ToolExecutionIdentity, *, agent_mode: AgentMode = "standard") -> ResponseTurnContext:
    return ResponseTurnContext(
        agent_mode=agent_mode,
        entity_label="leader",
        session_id=identity.session_id,
        run_id=None,
        correlation_id="correlation",
        reply_to_event_id="$parent-reply",
        room_id=identity.room_id,
        thread_id=identity.resolved_thread_id,
        requester_id=identity.requester_id,
        matrix_run_metadata=None,
    )


async def _delegate(
    toolkit: DelegateTools,
    config: Config,
    paths: RuntimePaths,
    tool_name: str,
    *,
    leader: ScriptedModel | None,
    **arguments: object,
) -> str:
    """Call a delegate function directly, or, given the ``leader``'s model, through its reply and the native driver.

    The native call's result is what the leader's next request answers the call with.
    """
    if leader is None:
        return await toolkit.functions[tool_name](**arguments)
    call_id = f"{tool_name}-call-{len(leader.requests)}"
    leader.turns.extend([calls(tool_call(tool_name, call_id, **arguments)), answer("Parent done")])
    result = await ai.ai_response(
        replace(
            _leader_turn(_identity()),
            prompt="Delegate",
            execution_identity=_identity(),
            supports_native_tool_approval=True,
        ),
        paths,
        config,
    )
    assert "Parent done" in result.text
    return next(
        message.get_content_string()
        for message in leader.requests[-1].messages
        if message.role == "tool" and message.tool_call_id == call_id
    )


def _run_subagent(toolkit: DelegateTools) -> ToolFunction:
    """Return ``run_subagent`` as an agent offered ``toolkit`` is offered it."""
    function = toolkit_function(toolkit, "run_subagent")
    assert function is not None
    return function


def test_authoring_guidance_mentions_minimal_only_when_offered(tmp_path: Path) -> None:
    """A caller authoring its own copies is told minimal=true overrides a profile only when it can pass minimal."""
    config = _config(helper_tools=["shell"])
    config.agents["helper"].delegate_to = ["helper"]

    def guidance() -> str:
        toolkit = DelegateTools("helper", _paths(tmp_path, {}), config, _workspace(tmp_path, "helper"))
        return _run_subagent(toolkit).description or ""

    # A local shell calls back through the running API, which this test starts only below.
    assert "minimal=true" not in guidance()
    set_api_server_address("0.0.0.0", 8765)  # noqa: S104 - the default bind address
    try:
        assert "minimal=true" in guidance()
    finally:
        clear_api_server_address()


def test_minimal_option_is_advertised_only_for_capable_subagents(tmp_path: Path) -> None:
    """The description recommends minimal mode, and the schema offers it, only where a subagent can run it."""
    config = _config(helper_tools=["shell"])

    def offered(toolkit: DelegateTools) -> bool:
        function = _run_subagent(toolkit)
        has_option = "minimal" in function.parameters["properties"]
        assert has_option == ("minimal mode" in (function.description or ""))
        return has_option

    # A local shell calls back through the running API, which this test starts only below.
    unready = DelegateTools("leader", _paths(tmp_path, {}), config, _workspace(tmp_path, "leader"))
    assert not offered(unready)
    set_api_server_address("0.0.0.0", 8765)  # noqa: S104 - the default bind address
    try:
        ready = DelegateTools(
            "leader",
            _paths(tmp_path, _CLI_DEPLOYMENT_ENV),
            config,
            _workspace(tmp_path, "leader"),
        )
        description = _run_subagent(ready).description or ""
        assert "Subagents that support minimal mode: helper." in description
        assert "not important in its system prompt" in description
        # Hiding the option must not change the schema other callers receive.
        assert offered(ready)
        # A local shell needs only the running API.
        assert offered(
            DelegateTools("leader", _paths(tmp_path, {}), config, _workspace(tmp_path, "leader")),
        )

        openai_caller = replace(_identity(), channel="openai_compat")
        detached = DelegateTools(
            "leader",
            _paths(tmp_path, _CLI_DEPLOYMENT_ENV),
            config,
            _workspace(tmp_path, "leader"),
            execution_identity=openai_caller,
        )
        assert not offered(detached)

        gated = _config(
            helper_tools=["shell"],
            approval=ToolApprovalConfig(
                rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
            ),
        )
        gated_tools = DelegateTools(
            "leader",
            _paths(tmp_path, _CLI_DEPLOYMENT_ENV),
            gated,
            _workspace(tmp_path, "leader"),
        )
        assert not offered(gated_tools)
    finally:
        clear_api_server_address()


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
@pytest.mark.parametrize("native", [False, True], ids=["direct", "native"])
@pytest.mark.parametrize(
    ("target", "approval"),
    [
        ("plain", None),
        (
            "helper",
            ToolApprovalConfig(rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")]),
        ),
    ],
)
async def test_ineligible_minimal_child_fails_with_a_standard_subagent_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    approval: ToolApprovalConfig | None,
    native: bool,
) -> None:
    """A child without usable shell commands fails before any Bash and tells the caller how to recover."""
    config = _config(helper_tools=["shell"], approval=approval)
    paths = _paths(tmp_path, _CLI_DEPLOYMENT_ENV)
    entity_ids(config, paths)
    toolkit = DelegateTools(
        "leader",
        paths,
        config,
        _workspace(tmp_path, "leader"),
        execution_identity=_identity(),
    )
    leader = ScriptedModel([])
    ScriptedAgents({"leader": leader, "helper": ScriptedModel([]), "plain": ScriptedModel([])}).install(monkeypatch)

    with tool_runtime_context(_live_context(config, paths)):
        result = await _delegate(
            toolkit,
            config,
            paths,
            "run_subagent",
            leader=leader if native else None,
            task="Report",
            agent_name=target,
            minimal=True,
        )

    assert "`run_shell_command` shell permission" in result, result
    assert "Start a new subagent without minimal." in result


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
@pytest.mark.parametrize("native", [False, True], ids=["direct", "native"])
async def test_minimal_subagent_requests_only_bash_and_follow_ups_keep_its_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native: bool,
) -> None:
    """A minimal child sees one Bash tool and a short prompt, never gated tools; its follow-up stays minimal."""
    config = _config(
        helper_tools=["shell", "calculator"],
        approval=ToolApprovalConfig(rules=[ApprovalRuleConfig(match="factorial", action="require_approval")]),
    )
    paths = _paths(tmp_path, _CLI_DEPLOYMENT_ENV)
    entity_ids(config, paths)
    identity = _identity()
    live = _live_context(config, paths)
    listings: list[str] = []

    async def run_shell_command(args: str, timeout: int = 120, tail: int = 100) -> str:  # noqa: ARG001, ASYNC109
        assert args == "mindroom-agent tools list"
        listing = await shell_cli_owner(live.orchestrator.agent_cli_registry).operation(
            window=cli_window(),
            operation=ToolListOperation(operation="tools.list"),
        )
        listings.append(str(listing))
        return str(listing)

    child = ScriptedModel(
        [
            calls(tool_call("bash", "bash-0", command="mindroom-agent tools list")),
            answer("Child answer 0"),
            calls(tool_call("bash", "bash-1", command="mindroom-agent tools list")),
            answer("Child answer 1"),
        ],
    )
    leader = ScriptedModel([])
    install_scripted_shell(monkeypatch, run_shell_command)
    ScriptedAgents({"leader": leader, "helper": child}).install(monkeypatch)
    toolkit = DelegateTools(
        "leader",
        paths,
        config,
        _workspace(tmp_path, "leader"),
        execution_identity=identity,
    )

    with tool_runtime_context(live):
        result = await _delegate(
            toolkit,
            config,
            paths,
            "run_subagent",
            leader=leader if native else None,
            task="Report",
            agent_name="helper",
            minimal=True,
        )
        assert "Child answer 0" in result, result

        handles = list((paths.storage_root / "subagent_sessions").glob("*.json"))
        assert len(handles) == 1
        saved = json.loads(handles[0].read_text())["child"]
        assert saved["agent_mode"] == "minimal"
        follow_up = await _delegate(
            toolkit,
            config,
            paths,
            "continue_subagent",
            leader=leader if native else None,
            subagent_id=saved["subagent_id"],
            message="More detail",
        )
        assert "Child answer 1" in follow_up, follow_up

    assert len(child.requests) == 4
    for request in child.requests:
        assert offered_tools(request) == ["bash"]
        system = request.messages[0].get_content_string()
        assert request.messages[0].role == "system"
        assert "optional long role" not in system
        assert "mindroom-agent" in system
    assert len(listings) == 2
    # Native callers support approval, but a minimal child must never pause for it.
    assert all("is_prime" in listing and "factorial" not in listing for listing in listings), listings


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
@pytest.mark.parametrize(
    ("caller", "bash_runs"),
    [("direct", True), ("native", True), ("approval_overlay", True), ("stale_approval_overlay", False)],
)
async def test_a_minimal_subagent_started_after_a_reload_runs_its_bash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    bash_runs: bool,
) -> None:
    """A reload after the caller's turn began leaves a fresh minimal child its Bash, not the caller's stale snapshot.

    A Dynamic Workflow participant's pre-approval overlay is not part of the config a reload is checked against, but
    its base is: an overlay built before a reload leaves a child whose tools that reload revokes.
    """
    config = _config(helper_tools=["shell"])
    snapshot = config.model_copy(update={"timezone": "Europe/Amsterdam"})
    paths = _paths(tmp_path, _CLI_DEPLOYMENT_ENV)
    entity_ids(config, paths)
    identity = _identity()
    # The caller's turn holds the config it started with; the reloaded one is current.
    live = replace(_live_context(snapshot, paths), config_provider=lambda: config)
    listings: list[str] = []

    async def run_shell_command(args: str, timeout: int = 120, tail: int = 100) -> str:  # noqa: ARG001, ASYNC109
        assert args == "mindroom-agent tools list"
        listing = await shell_cli_owner(live.orchestrator.agent_cli_registry).operation(
            window=cli_window(),
            operation=ToolListOperation(operation="tools.list"),
        )
        listings.append(str(listing))
        return str(listing)

    child = ScriptedModel(
        [calls(tool_call("bash", "bash-0", command="mindroom-agent tools list")), answer("Child answer")],
    )
    leader = ScriptedModel([])
    install_scripted_shell(monkeypatch, run_shell_command)
    ScriptedAgents({"leader": leader, "helper": child}).install(monkeypatch)
    toolkit = DelegateTools("leader", paths, snapshot, _workspace(tmp_path, "leader"), execution_identity=identity)

    with tool_runtime_context(live):
        if caller in {"approval_overlay", "stale_approval_overlay"}:
            base = snapshot if caller == "stale_approval_overlay" else config
            child_turn = prepare_child_turn(
                "leader",
                "helper",
                "Report",
                owner=identity,
                config=config,
                runtime_paths=paths,
                depth=0,
                budget_monitor=None,
                model=None,
                agent_mode="minimal",
                previous=None,
                persona=None,
            )
            overlay = base.model_copy(
                update={
                    "tool_approval": ToolApprovalConfig(
                        rules=[ApprovalRuleConfig(match="factorial", action="require_approval")],
                    ),
                },
            )
            completed = await run_direct_child_turn(
                child_turn,
                owner=identity,
                parent_run_id=None,
                config=base,
                runtime_paths=paths,
                refresh_scheduler=None,
                approval_config=overlay,
            )
            result = completed.text
        else:
            result = await _delegate(
                toolkit,
                snapshot,
                paths,
                "run_subagent",
                leader=leader if caller == "native" else None,
                task="Report",
                agent_name="helper",
                minimal=True,
            )

    assert ("Child answer" in result) is bash_runs, result
    assert len(listings) == int(bash_runs)
    # A child whose tools the reload refuses stops at its first call, without another request.
    assert ("so the turn stopped" in result) is not bash_runs
    assert len(child.requests) == 1 + int(bash_runs)


def _add_reranker(config: Config) -> None:
    config.reranker = TypeAdapter(RerankerConfig).validate_python({"provider": "typesafe"})


def _exclude_memory_archive(config: Config) -> None:
    config.memory.search.exclude.append(f"archive-{len(config.memory.search.exclude)}/**")


def _remove_shell(config: Config) -> None:
    config.agents["helper"].tools = ["calculator"]


def _withdraw_delegation(config: Config) -> None:
    config.agents["leader"].delegate_to = ["plain"]
    config.agents["helper"].tools = ["shell"]


def _move_helper_storage(config: Config) -> None:
    config.agents["helper"].worker_scope = "user"


def _append_room_and_room_model(config: Config) -> None:
    config.agents["helper"].rooms.append("new-room")
    config.room_models["new-room"] = "default"


_RELOAD_STOPPED = "MindRoom's configuration was reloaded while this turn ran, so the turn stopped"
_RELOAD_FOLLOW_UP = "MindRoom's configuration was reloaded during your previous turn"
_CALL_REFUSED = "This MindRoom call did not run: the configuration was reloaded"
# The control call a completed `switch_thread_model` call for the current response reports.
_SWITCHED = {"tool": "thread_model", "action": "switch", "status": "ok", "when": "after-toolcall", "model": "default"}
_SWITCH_CALL = ReportedCall("switch", "switch_thread_model", None, json.dumps(_SWITCHED))


@dataclass
class _ReloadedRun:
    """A minimal `helper` turn whose config reloaded while it ran."""

    result: str
    # The config each shell command's catalog ran under, and each config a reload made current.
    ran_under: list[Config]
    reloaded: list[Config]
    child: ScriptedModel
    paths: RuntimePaths

    def receipt(self) -> dict[str, object]:
        """Return the child as its delegation settled it."""
        (handle,) = (self.paths.storage_root / "subagent_sessions").glob("*.json")
        return json.loads(handle.read_text())["child"]

    def prompts(self) -> list[str]:
        """Return what each of the child's requests was last sent by its caller."""
        return [
            next(str(message.content) for message in reversed(request.messages) if message.role == "user")
            for request in self.child.requests
        ]


async def _run_minimal_turn_through_a_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reload: Callable[[Config], object],
    *,
    via: str = "direct",
    reload_at: str = "between_bash_calls",
    bash_calls: int = 3,
    reload_every_command: bool = False,
    after_cli_call: Callable[[LiveTurnTools], Awaitable[object]] | None = None,
) -> _ReloadedRun:
    """Run a minimal `helper` turn of Bash calls whose config reloads while it runs.

    Each command calls the calculator through the CLI. The reload lands after the first command (``between_bash_calls``),
    while the first command runs before its CLI call (``before_cli_call``), or while that call's toolkit is bound
    (``during_bind``). ``via`` is how the turn runs: `leader`'s ``direct`` subagent call, a call in its own ``reply``,
    a subagent ``job``, a ``workflow`` participant, a direct call under an ``approval_overlay``, or ``top_level`` as
    `helper`'s own turn. With ``reload_every_command``, each command triggers another reload. Each command awaits
    ``after_cli_call`` with the turn's CLI owner once its CLI call settled. Alice administers the deployment, so real
    authorization admits her.
    """
    helper_tools = ["shell", "calculator", *(["dynamic_workflow"] if via == "workflow" else [])]
    config = _config(helper_tools=helper_tools)
    config.models["default"].pricing = ModelPricing(input=1, output=1)
    config.administrators = ["@alice:example.org"]
    paths = _paths(tmp_path, _CLI_DEPLOYMENT_ENV)
    entity_ids(config, paths)
    current = [config]
    reloaded: list[Config] = []
    live = replace(_live_context(config, paths), config_provider=lambda: current[0])
    ran_under: list[Config] = []

    def trigger_reload(at: str) -> None:
        if at == reload_at and (reload_every_command or not reloaded):
            reloaded.append(current[0].model_copy(deep=True))
            reload(reloaded[-1])
            current[0] = reloaded[-1]

    bind = PreparedAgentToolCatalog.bind

    async def bind_through_a_reload(catalog: PreparedAgentToolCatalog, key: ToolKey, **kwargs: object) -> object:
        binding = await bind(catalog, key, **kwargs)
        if key.toolkit == "calculator":
            trigger_reload("during_bind")
        return binding

    monkeypatch.setattr(PreparedAgentToolCatalog, "bind", bind_through_a_reload)

    async def run_shell_command(args: str, timeout: int = 120, tail: int = 100) -> str:  # noqa: ARG001, ASYNC109
        owner = shell_cli_owner(live.orchestrator.agent_cli_registry)
        ran_under.append(owner.catalog.runtime_context.config)
        trigger_reload("before_cli_call")
        call_id = uuid4()
        await owner.operation(
            window=cli_window(),
            operation=ToolCallOperation(
                operation="tools.call",
                toolkit="calculator",
                function="is_prime",
                arguments={"n": 7},
                call_id=call_id,
            ),
        )
        receipt = await settled_receipt(owner, str(call_id))
        if after_cli_call is not None:
            await after_cli_call(owner)
        trigger_reload("between_bash_calls")
        return f"{receipt['status']}: {receipt['outcome']}"

    command = "mindroom-agent calculator is_prime 7"
    per_request = usage(1_000_000, 0)
    child = ScriptedModel(
        [
            *(
                calls(tool_call("bash", f"bash-{turn}", command=command), metrics=per_request)
                for turn in range(bash_calls)
            ),
            answer("Child answer", metrics=per_request),
        ],
        identity=ModelIdentity(id="gpt-6-astra", provider="OpenAI"),
    )
    leader = ScriptedModel([])
    install_scripted_shell(monkeypatch, run_shell_command)
    ScriptedAgents({"leader": leader, "helper": child}).install(monkeypatch)
    run = _ReloadedRun("", ran_under, reloaded, child, paths)
    if via in ("top_level", "workflow", "approval_overlay"):
        run.result = await _run_direct_reload_child(via, config, paths, live)
        return run
    toolkit = DelegateTools("leader", paths, config, _workspace(tmp_path, "leader"), execution_identity=_identity())
    with tool_runtime_context(live):
        if via == "job":
            run.result = await _run_subagent_job(tmp_path, config, paths, toolkit._caller_identity())
        else:
            run.result = await _delegate(
                toolkit,
                config,
                paths,
                "run_subagent",
                leader=leader if via == "reply" else None,
                task="Report",
                agent_name="helper",
                minimal=True,
            )
    return run


async def _run_direct_reload_child(
    via: str,
    config: Config,
    paths: RuntimePaths,
    live: ToolRuntimeContext,
) -> str:
    """Run a minimal `helper` turn directly: top-level, workflow participant, or under a pre-approval overlay."""
    if via == "top_level":
        identity = replace(_identity(), agent_name="helper")
        turn = replace(
            _leader_turn(identity, agent_mode="minimal"),
            entity_label="helper",
            prompt="Report",
            execution_identity=identity,
        )
        with tool_runtime_context(replace(live, agent_name="helper")):
            return (await ai.ai_response(turn, paths, config)).text
    caller = "helper" if via == "workflow" else "leader"
    owner = replace(_identity(), agent_name=caller, transport_agent_name=caller)
    persona = (
        SubagentPersona(source_kind="workflow", source_name="step", system_prompt="Report.")
        if via == "workflow"
        else None
    )
    child_turn = prepare_child_turn(
        caller,
        "helper",
        "Report",
        owner=owner,
        config=config,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
        model=None,
        agent_mode="minimal",
        previous=None,
        persona=persona,
    )
    with tool_runtime_context(replace(live, agent_name=caller)):
        direct = await run_direct_child_turn(
            child_turn,
            owner=owner,
            parent_run_id=None,
            config=config,
            runtime_paths=paths,
            refresh_scheduler=None,
            approval_config=config.model_copy(deep=True) if via == "approval_overlay" else None,
        )
    return direct.text


async def _run_subagent_job(tmp_path: Path, config: Config, paths: RuntimePaths, owner: ToolExecutionIdentity) -> str:
    """Run a minimal `helper` child of ``owner`` as the subagent job a managed call starts, and return its outcome."""
    child_turn = prepare_child_turn(
        "leader",
        "helper",
        "Report",
        owner=owner,
        config=config,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
        model=None,
        agent_mode="minimal",
        previous=None,
        persona=None,
    )
    runtime = await tool_job_runtime(tmp_path)
    try:
        spec = delegation_call_job(
            child_turn,
            job_id=child_turn.delegation_id,
            runtime=runtime,
            owner=owner,
            parent_run_id=None,
            tool_name="run_subagent",
            arguments={"agent_name": "helper", "task": "Report"},
            run_child=ai.run_delegated_child_response,
            config=config,
            runtime_paths=paths,
            refresh_scheduler=None,
        )
        assert isinstance(spec, JobSpec), spec
        _job, claim = await admit_delegation_spec(runtime, child_turn, spec, owner=owner)
        await runtime.release_wait(child_turn.delegation_id, claim)
        waited = await asyncio.wait_for(runtime.wait(child_turn.delegation_id, owner=owner, depth=0), JOB_TEST_TIMEOUT)
        await runtime.release_wait(child_turn.delegation_id, waited.claim)
    finally:
        await runtime.shutdown()
    return f"{waited.job.status}: {waited.job.result}"


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api", "enforce_turn_authorization")
@pytest.mark.parametrize("reload", [_add_reranker, _exclude_memory_archive], ids=["reranker", "memory_search_exclude"])
@pytest.mark.parametrize("via", ["direct", "reply", "job"])
async def test_a_reload_ends_a_minimal_subagents_turn_and_its_follow_up_turn_finishes_the_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reload: Callable[[Config], object],
    via: str,
) -> None:
    """A reload between two Bash calls stops the child's turn and its delegation finishes in a follow-up turn."""
    run = await _run_minimal_turn_through_a_reload(tmp_path, monkeypatch, reload, via=via)

    assert "Child answer" in run.result, run.result
    # The refused call never ran; the follow-up turn's command ran under the reloaded config.
    assert len(run.ran_under) == 2
    assert run.ran_under[0] is not run.reloaded[0]
    assert run.ran_under[1] is run.reloaded[0]
    # The stopped attempt asked the model nothing more: its next request is the follow-up turn's.
    prompts = run.prompts()
    assert len(prompts) == 4
    assert [_RELOAD_FOLLOW_UP in prompt for prompt in prompts] == [False, False, True, True]
    # The follow-up turn replays the child's session: its task, and the refused call with why it did not run.
    follow_up = str(run.child.requests[2].messages)
    assert "Report" in follow_up
    assert _CALL_REFUSED in follow_up
    assert "turn stopped" not in follow_up
    assert "completed: " in str(run.child.requests[3].messages[-1])
    receipt = run.receipt()
    assert receipt["status"] == "completed", receipt
    assert "Child answer" in str(receipt["result"]), receipt
    # Both the stopped run's two requests and the follow-up run's two requests count toward the child's cost.
    assert receipt["cost_usd"] == pytest.approx(4.0)


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api", "enforce_turn_authorization")
@pytest.mark.parametrize("reload_at", ["before_cli_call", "during_bind"])
async def test_a_cli_call_a_reload_refuses_inside_a_running_bash_ends_the_turn_for_its_follow_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reload_at: str,
) -> None:
    """A CLI call refused at dispatch, before or after its toolkit is bound, stops the run for a follow-up turn."""
    run = await _run_minimal_turn_through_a_reload(
        tmp_path,
        monkeypatch,
        _add_reranker,
        reload_at=reload_at,
        bash_calls=2,
    )

    assert "Child answer" in run.result, run.result
    assert run.ran_under[0] is not run.reloaded[0]
    assert run.ran_under[1] is run.reloaded[0]
    prompts = run.prompts()
    assert [_RELOAD_FOLLOW_UP in prompt for prompt in prompts] == [False, True, True]
    assert f"failed: {_CALL_REFUSED}" in str(run.child.requests[1].messages)
    assert "completed: " in str(run.child.requests[2].messages[-1])
    receipt = run.receipt()
    assert receipt["status"] == "completed", receipt
    assert receipt["cost_usd"] == pytest.approx(3.0)


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api", "enforce_turn_authorization")
@pytest.mark.parametrize("via", ["direct", "job"])
async def test_repeated_reloads_exhaust_follow_ups_and_settle_failed_with_resume_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    via: str,
) -> None:
    """Repeated reloads stop after the continuation limit and settle failed with the existing resume hint."""
    run = await _run_minimal_turn_through_a_reload(
        tmp_path,
        monkeypatch,
        _exclude_memory_archive,
        via=via,
        bash_calls=2 * (TURN_CONTINUATION_LIMIT + 2),
        reload_every_command=True,
    )

    assert "Child answer" not in run.result, run.result
    assert _RELOAD_STOPPED in run.result, run.result
    # Each turn ran one command under the config it started on, and its next call was refused.
    assert run.ran_under[1:] == run.reloaded[:TURN_CONTINUATION_LIMIT]
    assert len(run.child.requests) == 2 * (TURN_CONTINUATION_LIMIT + 1)
    receipt = run.receipt()
    assert receipt["status"] == "failed", receipt
    assert _RELOAD_STOPPED in str(receipt["result"]), receipt
    resume = f'Resume this subagent with continue_subagent(subagent_id="{receipt["subagent_id"]}", message="continue").'
    assert resume in str(receipt["result"]), receipt
    assert resume in run.result, run.result
    assert receipt["cost_usd"] == pytest.approx(2 * (TURN_CONTINUATION_LIMIT + 1))
    if via == "job":
        assert run.result.startswith("failed: "), run.result


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api", "enforce_turn_authorization")
@pytest.mark.parametrize(
    ("reload", "error"),
    [
        (_remove_shell, "Minimal mode requires the agent's `run_shell_command` shell permission"),
        (_withdraw_delegation, "Cannot delegate to 'helper'. Allowed subagents: plain."),
        (_move_helper_storage, "Subagent storage scope changed; start a new subagent."),
        (lambda config: config.agents.pop("helper"), "Subagent target or caller is no longer configured."),
        (lambda config: setattr(config, "administrators", []), "Cannot delegate to 'helper'"),
    ],
    ids=["shell_removed", "delegation_withdrawn", "storage_moved", "agent_removed", "requester_revoked"],
)
@pytest.mark.parametrize("via", ["direct", "job"])
async def test_a_reload_whose_config_refuses_the_follow_up_turn_fails_the_minimal_subagent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reload: Callable[[Config], object],
    error: str,
    via: str,
) -> None:
    """A follow-up turn the reloaded config no longer allows fails the child with the reason a new turn would get."""
    run = await _run_minimal_turn_through_a_reload(tmp_path, monkeypatch, reload, via=via)

    assert "Child answer" not in run.result, run.result
    assert error in run.result, run.result
    assert len(run.ran_under) == 1
    assert len(run.child.requests) == 2
    receipt = run.receipt()
    assert receipt["status"] == "failed", receipt
    assert error in str(receipt["result"]), receipt


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api", "enforce_turn_authorization")
@pytest.mark.parametrize("via", ["workflow", "approval_overlay"])
async def test_workflow_participant_and_approval_overlay_settle_failed_without_a_follow_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    via: str,
) -> None:
    """Workflow participants and pre-approval overlays do not run an automatic follow-up on reload."""
    run = await _run_minimal_turn_through_a_reload(tmp_path, monkeypatch, _add_reranker, via=via)

    assert "Child answer" not in run.result, run.result
    assert len(run.ran_under) == 1
    assert len(run.child.requests) == 2
    receipt = run.receipt()
    assert receipt["status"] == "failed", receipt
    assert _RELOAD_STOPPED in str(receipt["result"]), receipt
    if via == "workflow":
        assert "continue_subagent" not in str(receipt["result"]), receipt
    else:
        assert f'continue_subagent(subagent_id="{receipt["subagent_id"]}"' in str(receipt["result"]), receipt


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api", "enforce_turn_authorization")
@pytest.mark.parametrize("via", ["direct", "job"])
async def test_appending_own_room_and_room_model_does_not_stop_a_minimal_subagent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    via: str,
) -> None:
    """Appending a room to the child's own config and adding a room_models entry keeps the turn running."""
    run = await _run_minimal_turn_through_a_reload(tmp_path, monkeypatch, _append_room_and_room_model, via=via)

    assert "Child answer" in run.result, run.result
    assert len(run.ran_under) == 3
    assert all(cfg is not run.reloaded[0] for cfg in run.ran_under)
    assert len(run.child.requests) == 4
    assert all(_RELOAD_FOLLOW_UP not in prompt for prompt in run.prompts())
    assert all(_CALL_REFUSED not in str(request.messages) for request in run.child.requests)
    receipt = run.receipt()
    assert receipt["status"] == "completed", receipt


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api", "enforce_turn_authorization")
async def test_a_reload_stop_wins_over_a_model_switch_in_the_same_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A switch that asks for another attempt does not continue a turn whose reload stop said it ended."""

    async def complete_a_model_switch(owner: LiveTurnTools) -> None:
        owner.control_calls.append(_SWITCH_CALL)

    run = await _run_minimal_turn_through_a_reload(
        tmp_path,
        monkeypatch,
        _add_reranker,
        via="top_level",
        reload_at="before_cli_call",
        after_cli_call=complete_a_model_switch,
    )

    assert len(run.ran_under) == 1
    assert len(run.child.requests) == 1
    assert _RELOAD_STOPPED in run.result, run.result


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api", "enforce_turn_authorization")
async def test_a_minimal_subagent_cancelled_after_a_reload_refused_its_call_settles_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command that outlives its refused call is cancelled as any child is: the reload stop names completed runs."""
    refused = asyncio.Event()

    async def keep_running(_owner: LiveTurnTools) -> None:
        refused.set()
        await asyncio.Event().wait()

    turn = asyncio.create_task(
        _run_minimal_turn_through_a_reload(
            tmp_path,
            monkeypatch,
            _add_reranker,
            reload_at="before_cli_call",
            after_cli_call=keep_running,
        ),
    )
    await asyncio.wait_for(refused.wait(), JOB_TEST_TIMEOUT)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, JOB_TEST_TIMEOUT)

    (handle,) = (_paths(tmp_path, _CLI_DEPLOYMENT_ENV).storage_root / "subagent_sessions").glob("*.json")
    receipt = json.loads(handle.read_text())["child"]
    assert receipt["status"] == "cancelled", receipt
    assert "continue_subagent" not in str(receipt["result"]), receipt


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api", "enforce_turn_authorization")
async def test_a_reload_stopped_minimal_subagent_recovers_as_failed_after_a_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart between the stopped run's save and the child's settlement settles the child from the stored run."""
    run = await _run_minimal_turn_through_a_reload(
        tmp_path,
        monkeypatch,
        _add_reranker,
        via="approval_overlay",
    )
    settled = run.receipt()
    (handle,) = (run.paths.storage_root / "subagent_sessions").glob("*.json")
    saved = json.loads(handle.read_text())
    saved["child"] |= {"status": "running", "result": None}
    handle.write_text(json.dumps(saved))

    recovered = await settled_subagent(
        str(settled["subagent_id"]),
        owner=replace(_identity(), transport_agent_name="leader"),
        config=run.reloaded[0],
        runtime_paths=run.paths,
        depth=0,
    )

    assert (recovered.status, recovered.result) == ("failed", settled["result"])
    assert f'continue_subagent(subagent_id="{recovered.subagent_id}"' in str(recovered.result)


def test_only_a_completed_run_is_reload_stopped_and_a_workflow_participant_is_not_told_to_continue(
    tmp_path: Path,
) -> None:
    """A workflow participant cannot be continued outside its workflow; a run that ended another way settles as that."""
    config = _config(helper_tools=["shell"])
    paths = _paths(tmp_path, _CLI_DEPLOYMENT_ENV)

    def stopped_run(status: RunStatus) -> RunRecord:
        marker = {CONFIG_RELOAD_CALL: RELOAD_STOPPED}
        return RunRecord(run_id="run", session_id="session", status=status, metadata=marker)

    stopped = stopped_run(RunStatus.completed)

    def own_copy(source_kind: PersonaSourceKind) -> DelegationChild:
        return prepare_child_turn(
            "leader",
            "leader",
            "Review",
            owner=_identity(),
            config=config,
            runtime_paths=paths,
            depth=0,
            budget_monitor=None,
            model=None,
            agent_mode="minimal",
            previous=None,
            persona=SubagentPersona(source_kind=source_kind, source_name="review", system_prompt="Review."),
        )

    inline = own_copy("inline")
    resume = f'Resume this subagent with continue_subagent(subagent_id="{inline.subagent_id}", message="continue").'
    assert reload_stopped_child_result(inline, stopped) == f"{RELOAD_STOPPED} {resume}"
    assert reload_stopped_child_result(own_copy("workflow"), stopped) == RELOAD_STOPPED
    assert reload_stopped_child_result(inline, RunRecord(run_id="run", session_id="session")) is None
    # A refused call marks the run, which may still be cancelled, fail, or pause before its batch ends.
    for status in (RunStatus.cancelled, RunStatus.error, RunStatus.paused):
        assert reload_stopped_child_result(inline, stopped_run(status)) is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api", "enforce_turn_authorization")
async def test_a_reload_ends_a_minimal_turn_in_a_room_with_the_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A minimal turn that is no subagent's stops at the refused call too, and its reply ends with why."""
    run = await _run_minimal_turn_through_a_reload(tmp_path, monkeypatch, _add_reranker, via="top_level")

    assert len(run.ran_under) == 1
    assert len(run.child.requests) == 2
    assert run.result.rstrip().endswith("A new turn runs under the reloaded configuration."), run.result
    assert _RELOAD_STOPPED in run.result
    assert "continue_subagent" not in run.result


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
async def test_minimal_parent_runs_a_minimal_child_through_its_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child started inside a parent's CLI call captures its own provider batch and runs its own Bash."""
    config = _config(helper_tools=["shell"])
    config.agents["leader"] = AgentConfig(
        display_name="Leader",
        model="lead",
        tools=["shell"],
        memory_backend="file",
        delegate_to=["helper"],
    )
    config.models["lead"] = ModelConfig(provider="openai", id="gpt-6.1-sol")
    paths = _paths(tmp_path, _CLI_DEPLOYMENT_ENV)
    entity_ids(config, paths)
    identity = _identity()
    live = _live_context(config, paths)
    child_listings: list[str] = []

    async def run_shell_command(args: str, timeout: int = 120, tail: int = 100) -> str:  # noqa: ARG001, ASYNC109
        # Each Bash call reaches only its own response, the parent's or its minimal child's.
        owner = shell_cli_owner(live.orchestrator.agent_cli_registry)
        if owner.owner.execution_identity.agent_name == "helper":
            assert args == "mindroom-agent tools list"
            listing = str(
                await owner.operation(window=cli_window(), operation=ToolListOperation(operation="tools.list")),
            )
            child_listings.append(listing)
            return listing
        call_id = uuid4()
        await owner.operation(
            window=cli_window(),
            operation=ToolCallOperation(
                operation="tools.call",
                toolkit="delegate",
                function="run_subagent",
                arguments={"agent_name": "helper", "task": "List your tools", "title": "List tools", "minimal": True},
                call_id=call_id,
            ),
        )
        receipt = await settled_receipt(owner, str(call_id))
        assert receipt["status"] == "completed", json.dumps(receipt, default=str)
        return str(receipt["outcome"])

    lead = ScriptedModel(
        [calls(tool_call("bash", "lead-bash", command="mindroom-agent tools call ...")), answer("Leader done")],
    )
    child = ScriptedModel(
        [calls(tool_call("bash", "child-bash", command="mindroom-agent tools list")), answer("Child answer")],
    )
    install_scripted_shell(monkeypatch, run_shell_command)
    ScriptedAgents({"leader": lead, "helper": child}).install(monkeypatch)
    turn = _leader_turn(identity, agent_mode="minimal")
    continued: list[str] = []

    def continue_turn_run(*_args: object, **_kwargs: object) -> None:
        continued.append("parent")

    monkeypatch.setattr(ai._TurnRunContinuation, "events", continue_turn_run)

    with tool_runtime_context(live):
        result = await ai.ai_response(replace(turn, prompt="Delegate", execution_identity=identity), paths, config)

    assert "Leader done" in result.text, result
    assert len(child_listings) == 1
    assert [offered_tools(request) for request in child.requests] == [["bash"], ["bash"]]
    # The parent's delegation ran inside its Bash call, so its run never paused and kept its Bash-only surface.
    assert continued == []
    assert [offered_tools(request) for request in lead.requests] == [["bash"], ["bash"]]
    assert "Child answer" in str(lead.requests[-1].messages)


@pytest.mark.asyncio
async def test_child_snapshot_without_mode_continues_in_standard_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subagent saved before modes existed continues in standard mode with its full tool surface once rewritten."""
    config = _config(helper_tools=["shell"])
    paths = _paths(tmp_path, {})
    entity_ids(config, paths)
    child = ScriptedModel([answer("Answer 0"), answer("Answer 1")])
    ScriptedAgents({"helper": child}).install(monkeypatch)
    toolkit = DelegateTools(
        "leader",
        paths,
        config,
        _workspace(tmp_path, "leader"),
        execution_identity=_identity(),
    )

    with tool_runtime_context(_live_context(config, paths)):
        assert "Answer 0" in await toolkit.run_subagent(task="Report", agent_name="helper")
        handle = next((paths.storage_root / "subagent_sessions").glob("*.json"))
        payload = json.loads(handle.read_text())
        del payload["child"]["agent_mode"]
        handle.write_text(json.dumps(payload))
        await migrate_subagent_sessions(paths)
        assert json.loads(handle.read_text())["child"]["agent_mode"] == "standard"
        assert "Answer 1" in await toolkit.continue_subagent(payload["child"]["subagent_id"], "More")

    assert "bash" not in offered_tools(child.requests[1])
    assert "run_shell_command" in offered_tools(child.requests[1])
