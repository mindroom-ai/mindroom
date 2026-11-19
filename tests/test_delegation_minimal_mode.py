"""Callers can run a subagent in token-efficient minimal mode."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from mindroom import ai
from mindroom.agent_cli.protocol import ToolCallOperation, ToolListOperation
from mindroom.agent_cli.session import TurnToolRegistry
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.knowledge import RerankerConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation.direct import run_direct_child_turn
from mindroom.delegation.legacy_snapshots import migrate_subagent_sessions
from mindroom.delegation.lifecycle import prepare_child_turn
from mindroom.response_turn import ResponseTurnContext
from mindroom.runtime_state import clear_api_server_address, set_api_server_address
from mindroom.tool_system.agno_toolkits import toolkit_function
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from mindroom.turn_continuation import TURN_CONTINUATION_LIMIT
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
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

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.agent_modes import AgentMode
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
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

    assert "Child answer" in result, result
    assert len(listings) == int(bash_runs)
    assert ("no longer permits" in str(child.requests[-1].messages)) is not bash_runs


def _add_reranker(config: Config) -> None:
    config.reranker = TypeAdapter(RerankerConfig).validate_python({"provider": "typesafe"})


def _exclude_memory_archive(config: Config) -> None:
    config.memory.search.exclude.append(f"archive-{len(config.memory.search.exclude)}/**")


async def _run_minimal_child_through_reloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reload: Callable[[Config], object],
    *,
    bash_calls: int,
    reload_every_command: bool = False,
) -> tuple[str, list[Config], list[Config], ScriptedModel]:
    """Run a minimal child whose config reloads after its first shell command, or after every one.

    Returns the child's result, the config each command's catalog ran under, the reloaded configs, and its model.
    """
    config = _config(helper_tools=["shell"])
    paths = _paths(tmp_path, _CLI_DEPLOYMENT_ENV)
    entity_ids(config, paths)
    current = [config]
    reloaded: list[Config] = []
    live = replace(_live_context(config, paths), config_provider=lambda: current[0])
    ran_under: list[Config] = []

    async def run_shell_command(args: str, timeout: int = 120, tail: int = 100) -> str:  # noqa: ARG001, ASYNC109
        owner = shell_cli_owner(live.orchestrator.agent_cli_registry)
        listing = await owner.operation(window=cli_window(), operation=ToolListOperation(operation="tools.list"))
        ran_under.append(owner.catalog.runtime_context.config)
        if reload_every_command or not reloaded:
            # The reload lands while the child runs, between its Bash calls.
            reloaded.append(current[0].model_copy(deep=True))
            reload(reloaded[-1])
            current[0] = reloaded[-1]
        return str(listing)

    bash = [calls(tool_call("bash", f"bash-{turn}", command="mindroom-agent tools list")) for turn in range(bash_calls)]
    child = ScriptedModel([*bash, answer("Child answer")])
    install_scripted_shell(monkeypatch, run_shell_command)
    ScriptedAgents({"leader": ScriptedModel([]), "helper": child}).install(monkeypatch)
    toolkit = DelegateTools("leader", paths, config, _workspace(tmp_path, "leader"), execution_identity=_identity())
    with tool_runtime_context(live):
        result = await _delegate(
            toolkit,
            config,
            paths,
            "run_subagent",
            leader=None,
            task="Report",
            agent_name="helper",
            minimal=True,
        )
    return result, ran_under, reloaded, child


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
@pytest.mark.parametrize("reload", [_add_reranker, _exclude_memory_archive], ids=["reranker", "memory_search_exclude"])
async def test_a_running_minimal_subagent_continues_on_rebuilt_tools_after_a_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reload: Callable[[Config], object],
) -> None:
    """A reload between two Bash calls stops the run at the refused call, and the turn goes on under the new config."""
    result, ran_under, reloaded, child = await _run_minimal_child_through_reloads(
        tmp_path,
        monkeypatch,
        reload,
        bash_calls=3,
    )

    assert "Child answer" in result, result
    # The refused call never ran; the one after the rebuild ran under the reloaded config.
    assert len(ran_under) == 2
    assert ran_under[1] is reloaded[0]
    assert ran_under[0] is not reloaded[0]
    assert len(child.requests) == 4
    refused, rebuilt = str(child.requests[2].messages), str(child.requests[3].messages)
    assert "CONFIGURATION RELOADED" in refused
    assert "did not run" in refused
    assert "no longer permits" not in rebuilt


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
async def test_a_reload_that_removes_a_running_minimal_subagents_shell_ends_its_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rebuilt agent has no shell, so the turn ends with the minimal-mode error and no call ran on the old tools."""
    result, ran_under, _reloaded, child = await _run_minimal_child_through_reloads(
        tmp_path,
        monkeypatch,
        lambda config: setattr(config.agents["helper"], "tools", []),
        bash_calls=3,
    )

    assert "Child answer" not in result, result
    assert "minimal" in result.lower(), result
    assert len(ran_under) == 1
    assert len(child.requests) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
async def test_repeated_reloads_end_a_minimal_subagents_turn_at_the_continuation_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config that reloads after every command cannot keep a turn rebuilding past the continuation limit."""
    result, ran_under, reloaded, child = await _run_minimal_child_through_reloads(
        tmp_path,
        monkeypatch,
        _exclude_memory_archive,
        bash_calls=2 * (TURN_CONTINUATION_LIMIT + 2),
        reload_every_command=True,
    )

    assert "Child answer" not in result, result
    assert f"{TURN_CONTINUATION_LIMIT} continuation attempts" in result, result
    # Each attempt ran one command under the config of its own rebuild, and its next was refused.
    assert ran_under[1:] == reloaded[:TURN_CONTINUATION_LIMIT]
    assert len(child.requests) == 2 * (TURN_CONTINUATION_LIMIT + 1)


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
                arguments={"agent_name": "helper", "task": "List your tools", "minimal": True},
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
