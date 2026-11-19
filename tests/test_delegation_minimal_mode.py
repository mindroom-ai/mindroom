"""Callers can run a subagent in token-efficient minimal mode."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

import mindroom.memory._file_backend as file_backend_module
import mindroom.memory._semantic_file_search as semantic_file_search
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
from mindroom.hooks import EVENT_TOOL_BEFORE_CALL, HookRegistry, ToolBeforeCallContext, hook
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.read_protocol import DISTANCE_KEY, SearchHit
from mindroom.response_turn import ResponseTurnContext
from mindroom.runtime_state import clear_api_server_address, set_api_server_address
from mindroom.tool_system.agno_toolkits import toolkit_function
from mindroom.tool_system.plugins import ConfigPlugins
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
from tests.test_tool_hooks import _plugin

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.agent_cli.turn import LiveTurnTools
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


@dataclass
class _ReloadedRun:
    """A minimal `helper` turn whose config reloaded while it ran."""

    result: str
    # The config each shell command's catalog ran under, and each config a reload made current.
    ran_under: list[Config]
    reloaded: list[Config]
    child: ScriptedModel
    paths: RuntimePaths
    # What a later `continue_subagent` turn answered, when one was asked for.
    follow_up: str | None = None

    def receipt(self) -> dict[str, object]:
        """Return the child as its delegation settled it."""
        (handle,) = (self.paths.storage_root / "subagent_sessions").glob("*.json")
        return json.loads(handle.read_text())["child"]


async def _run_minimal_turn_through_reloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reload: Callable[[Config], object],
    *,
    bash_calls: int,
    reload_every_command: bool = False,
    cli_call: Callable[[LiveTurnTools], Awaitable[object]] | None = None,
    reload_before_cli_call: bool = False,
    prepare: Callable[[Config], object] | None = None,
    native: bool = False,
    narrowed_to: tuple[str, ...] | None = None,
    follow_up: bool = False,
) -> _ReloadedRun:
    """Run a minimal `helper` turn whose config reloads after its first shell command, or after every one.

    Each command lists the turn's tools, or makes ``cli_call``, which the first reload precedes with
    ``reload_before_cli_call``. The turn is a child `leader` delegates, directly or with ``native`` through its own
    reply, or with ``narrowed_to`` a turn of `helper`'s own that offers only those toolkits, as a contact's does.
    ``prepare`` edits the config the turn starts under. Alice administers the deployment, so real authorization
    admits her until a reload says otherwise.
    """
    config = _config(helper_tools=["shell", "calculator"])
    config.administrators = ["@alice:example.org"]
    if prepare is not None:
        prepare(config)
    paths = _paths(tmp_path, _CLI_DEPLOYMENT_ENV)
    entity_ids(config, paths)
    current = [config]
    reloaded: list[Config] = []
    live = replace(_live_context(config, paths), config_provider=lambda: current[0])
    ran_under: list[Config] = []

    def reload_config() -> None:
        reloaded.append(current[0].model_copy(deep=True))
        reload(reloaded[-1])
        current[0] = reloaded[-1]

    async def run_shell_command(args: str, timeout: int = 120, tail: int = 100) -> str:  # noqa: ARG001, ASYNC109
        owner = shell_cli_owner(live.orchestrator.agent_cli_registry)
        ran_under.append(owner.catalog.runtime_context.config)
        if reload_before_cli_call and not reloaded:
            # The reload lands while this command runs, before its CLI call.
            reload_config()
        if cli_call is not None:
            output = await cli_call(owner)
        else:
            output = await owner.operation(window=cli_window(), operation=ToolListOperation(operation="tools.list"))
        if reload_every_command or not reloaded:
            # The reload lands while the turn runs, between its Bash calls.
            reload_config()
        return str(output)

    bash = [calls(tool_call("bash", f"bash-{turn}", command="mindroom-agent tools list")) for turn in range(bash_calls)]
    child = ScriptedModel([*bash, answer("Child answer")])
    leader = ScriptedModel([])
    install_scripted_shell(monkeypatch, run_shell_command)
    ScriptedAgents({"leader": leader, "helper": child}).install(monkeypatch)
    run = _ReloadedRun("", ran_under, reloaded, child, paths)
    if narrowed_to is not None:
        identity = replace(_identity(), agent_name="helper")
        turn = replace(
            _leader_turn(identity, agent_mode="minimal"),
            entity_label="helper",
            prompt="Report",
            execution_identity=identity,
            tool_names=narrowed_to,
        )
        with tool_runtime_context(replace(live, agent_name="helper")):
            run.result = (await ai.ai_response(turn, paths, config)).text
        return run
    toolkit = DelegateTools("leader", paths, config, _workspace(tmp_path, "leader"), execution_identity=_identity())
    with tool_runtime_context(live):
        run.result = await _delegate(
            toolkit,
            config,
            paths,
            "run_subagent",
            leader=leader if native else None,
            task="Report",
            agent_name="helper",
            minimal=True,
        )
        if follow_up:
            child.turns.append(answer("Follow-up answer"))
            run.follow_up = await _delegate(
                toolkit,
                current[0],
                paths,
                "continue_subagent",
                leader=None,
                subagent_id=str(run.receipt()["subagent_id"]),
                message="More detail",
            )
    return run


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api", "enforce_turn_authorization")
@pytest.mark.parametrize("reload", [_add_reranker, _exclude_memory_archive], ids=["reranker", "memory_search_exclude"])
async def test_a_running_minimal_subagent_continues_on_rebuilt_tools_after_a_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reload: Callable[[Config], object],
) -> None:
    """A reload between two Bash calls stops the run at the refused call, and the turn goes on under the new config."""
    run = await _run_minimal_turn_through_reloads(tmp_path, monkeypatch, reload, bash_calls=3, follow_up=True)

    assert "Child answer" in run.result, run.result
    # The refused call never ran; the one after the rebuild ran under the reloaded config.
    assert len(run.ran_under) == 2
    assert run.ran_under[1] is run.reloaded[0]
    assert run.ran_under[0] is not run.reloaded[0]
    assert len(run.child.requests) == 5
    # The continued attempt reads the refused call's own result, and no later call is refused.
    continued, final = str(run.child.requests[2].messages), str(run.child.requests[3].messages)
    assert "This call did not run: MindRoom's configuration was reloaded" in continued
    assert "no longer permits" not in final
    assert final.count("This call did not run") == 1
    # No instruction about the reload joins the turn's prompt, where a later turn would replay it.
    assert "Follow-up answer" in str(run.follow_up)
    assert all("SYSTEM NOTICE" not in str(request.messages) for request in run.child.requests)


def _cli_call(
    receipts: list[dict[str, object]],
    toolkit: str,
    function: str,
    **arguments: object,
) -> Callable[[LiveTurnTools], Awaitable[object]]:
    """Return a shell command's call of ``toolkit.function`` through the CLI, which keeps its settled receipt."""

    async def call(owner: LiveTurnTools) -> object:
        call_id = uuid4()
        await owner.operation(
            window=cli_window(),
            operation=ToolCallOperation(
                operation="tools.call",
                toolkit=toolkit,
                function=function,
                arguments=arguments,
                call_id=call_id,
            ),
        )
        receipts.append(await settled_receipt(owner, str(call_id)))
        return receipts[-1]["outcome"]

    return call


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
async def test_a_rebuilt_minimal_turns_before_call_hook_reads_the_reloaded_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin gate that allowed a call under the old config declines it once the reload it reads says so."""

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def audited_calls_need_request_logs(ctx: ToolBeforeCallContext) -> None:
        if ctx.tool_name == "is_prime" and not ctx.config.debug.log_llm_requests:
            ctx.decline("audited calls need request logs")

    registry = HookRegistry.from_plugins([_plugin("policy", [audited_calls_need_request_logs])])
    monkeypatch.setattr(ConfigPlugins, "hooks", property(lambda _plugins: registry))
    receipts: list[dict[str, object]] = []
    run = await _run_minimal_turn_through_reloads(
        tmp_path,
        monkeypatch,
        lambda config: setattr(config.debug, "log_llm_requests", False),
        bash_calls=3,
        cli_call=_cli_call(receipts, "calculator", "is_prime", n=7),
        prepare=lambda config: setattr(config.debug, "log_llm_requests", True),
    )

    assert "Child answer" in run.result, run.result
    assert [receipt["status"] for receipt in receipts] == ["completed", "failed"]
    assert "[TOOL CALL DECLINED]" in json.dumps(receipts[1]["outcome"])
    assert run.ran_under == [run.ran_under[0], run.reloaded[0]]


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
async def test_a_rebuilt_minimal_turn_sends_nothing_to_a_reranker_the_reload_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A memory search after the rebuild reads the reloaded config, so the removed reranker gets no more documents."""
    reranked: list[list[str]] = []
    sent_before_reload: list[int] = []

    class Reranker:
        async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
            del query
            reranked.append(list(documents))
            return [1.0] * len(documents)

    class Index:
        async def asearch(self, _query: str, limit: int) -> list[SearchHit]:
            hit = SearchHit(
                content="The shed is green.",
                meta_data={"source_path": "memory/notes.md", DISTANCE_KEY: 0.1},
            )
            return [hit][:limit]

    async def resolve_access(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(knowledge=Index(), availability=KnowledgeAvailability.READY)

    monkeypatch.setattr(semantic_file_search, "list_knowledge_files", lambda *_args: [Path("memory/notes.md")])
    monkeypatch.setattr(semantic_file_search, "resolve_knowledge_base_access_async", resolve_access)
    # The recording reranker serves any config that names one, as the config a search is handed decides.
    monkeypatch.setattr(
        file_backend_module,
        "configured_reranker",
        lambda config, _paths: Reranker() if config.reranker is not None else None,
    )

    def configure(config: Config) -> None:
        config.agents["helper"].tools = ["shell", "memory"]
        config.memory.search.mode = "semantic"
        config.memory.search.rerank = True
        config.reranker = TypeAdapter(RerankerConfig).validate_python({"provider": "cohere", "model": "rerank-v3.5"})

    def remove_reranker(config: Config) -> None:
        config.reranker = None
        sent_before_reload.append(len(reranked))

    receipts: list[dict[str, object]] = []
    run = await _run_minimal_turn_through_reloads(
        tmp_path,
        monkeypatch,
        remove_reranker,
        bash_calls=3,
        cli_call=_cli_call(receipts, "memory", "search_memories", query="shed"),
        prepare=configure,
    )

    assert "Child answer" in run.result, run.result
    assert [receipt["status"] for receipt in receipts] == ["completed", "completed"]
    assert all("The shed is green." in str(receipt["outcome"]) for receipt in receipts)
    # The search before the reload was reranked; the one after it, on the rebuilt tools, was not.
    assert sent_before_reload[0] >= 1
    assert len(reranked) == sent_before_reload[0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
async def test_a_cli_call_a_reload_refuses_inside_a_running_bash_runs_after_the_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reload during a Bash call refuses the CLI calls its command still makes, and the next attempt runs them."""
    receipts: list[dict[str, object]] = []
    run = await _run_minimal_turn_through_reloads(
        tmp_path,
        monkeypatch,
        _add_reranker,
        bash_calls=2,
        cli_call=_cli_call(receipts, "calculator", "is_prime", n=7),
        reload_before_cli_call=True,
    )

    assert "Child answer" in run.result, run.result
    assert [receipt["status"] for receipt in receipts] == ["cancelled", "completed"]
    # The refused call's receipt says why, so the command's output tells the continued attempt.
    assert "This call did not run: MindRoom's configuration was reloaded" in str(receipts[0]["outcome"])
    assert "This call did not run" in str(run.child.requests[1].messages)
    assert run.ran_under[1] is run.reloaded[0]
    assert run.ran_under[0] is not run.reloaded[0]
    assert len(run.child.requests) == 3


_REFUSED = "Current configuration no longer permits this prepared CLI call. Start a new subagent without minimal."
_NEW_PROMPT = "PROMPT-OF-THE-RELOADED-CONFIG"


def _reload_prompt(config: Config) -> None:
    config.agents["helper"].minimal_instructions = [_NEW_PROMPT]


def _revoke_requester(config: Config) -> None:
    config.administrators = []
    _reload_prompt(config)


def _withdraw_delegation(config: Config) -> None:
    config.agents["leader"].delegate_to = ["plain"]
    config.agents["helper"].tools = ["shell"]
    _reload_prompt(config)


def _move_shell_out_of_its_worker(config: Config) -> None:
    # Where the agent's tools run is not part of its worker target, which stays as it was.
    config.agents["helper"].worker_tools = []
    _reload_prompt(config)


def _remove_shell(config: Config) -> None:
    config.agents["helper"].tools = []
    _reload_prompt(config)


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api", "enforce_turn_authorization")
@pytest.mark.parametrize(
    ("reload", "error"),
    [
        (_remove_shell, "Minimal mode requires the agent's `run_shell_command` shell permission"),
        (lambda config: config.agents.pop("helper"), _REFUSED),
        (_revoke_requester, _REFUSED),
        (_withdraw_delegation, _REFUSED),
        (_move_shell_out_of_its_worker, _REFUSED),
    ],
    ids=[
        "shell_removed",
        "agent_removed",
        "requester_revoked",
        "delegation_withdrawn",
        "shell_moved_out_of_its_worker",
    ],
)
@pytest.mark.parametrize("native", [False, True], ids=["direct", "native"])
async def test_a_reload_a_running_minimal_subagent_cannot_rebuild_under_fails_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reload: Callable[[Config], object],
    error: str,
    native: bool,
) -> None:
    """A turn whose grant the reloaded config would not give again ends failed, before anything of that config reaches it.

    Its requester, its caller's permission to delegate to it, and where its shell runs are checked before the rebuild,
    so the model is sent no prompt of the reloaded config; a rebuilt agent without a shell fails as a new turn would.
    """
    run = await _run_minimal_turn_through_reloads(tmp_path, monkeypatch, reload, bash_calls=3, native=native)

    assert "Child answer" not in run.result, run.result
    assert error in run.result, run.result
    # No call ran on the old tools, and the retired attempt and the refused rebuild sent the model nothing more.
    assert len(run.ran_under) == 1
    assert len(run.child.requests) == 2
    assert _NEW_PROMPT not in str([request.messages for request in run.child.requests])
    receipt = run.receipt()
    assert receipt["status"] == "failed", receipt
    assert error in str(receipt["result"]), receipt


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
async def test_a_reload_refuses_a_narrowed_minimal_turns_calls_without_rebuilding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn offered only some toolkits, as a contact's is, keeps refusing: its narrowing came from the old config."""
    run = await _run_minimal_turn_through_reloads(
        tmp_path,
        monkeypatch,
        lambda config: setattr(config.agents["helper"], "tools", ["shell"]),
        bash_calls=2,
        narrowed_to=("shell",),
    )

    assert "Child answer" in run.result, run.result
    assert len(run.ran_under) == 1
    assert len(run.child.requests) == 3
    assert "no longer permits this prepared CLI call" in str(run.child.requests[-1].messages)


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
async def test_repeated_reloads_end_a_minimal_subagents_turn_at_the_continuation_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config that reloads after every command cannot keep a turn rebuilding past the continuation limit."""
    run = await _run_minimal_turn_through_reloads(
        tmp_path,
        monkeypatch,
        _exclude_memory_archive,
        bash_calls=2 * (TURN_CONTINUATION_LIMIT + 2),
        reload_every_command=True,
    )

    assert "Child answer" not in run.result, run.result
    assert f"{TURN_CONTINUATION_LIMIT} continuation attempts" in run.result, run.result
    # Each attempt ran one command under the config of its own rebuild, and its next was refused.
    assert run.ran_under[1:] == run.reloaded[:TURN_CONTINUATION_LIMIT]
    assert len(run.child.requests) == 2 * (TURN_CONTINUATION_LIMIT + 1)


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
