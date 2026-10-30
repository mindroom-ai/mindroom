"""Building a team: its exact members, the leader's and members' models, and the team spec.

``build_team_spec`` turns the resolved members into the team the leader loop runs: the leader's model, prompt, job
controls, and call budget, and each member's roster entry from config with the build its first delegation makes, the
member's system messages and loop hooks.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING

from mindroom.agent_loop.build import appended_context, prompt_for_turn, tool_owners_by_agent
from mindroom.agent_loop.spec import AgentSpec
from mindroom.agent_loop.tools import toolkit_instructions
from mindroom.agents import create_agent, team_background_jobs
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.knowledge.utils import resolve_agent_knowledge_access
from mindroom.logging_config import get_logger
from mindroom.loop_hooks import HookTurn, agent_loop_hooks
from mindroom.prompt_templates import openai_compat_identity, render_prompt_template
from mindroom.providers import registry as provider_registry
from mindroom.system_prompt import render_session_context
from mindroom.teams.formation import (
    not_materializable_team_agents_message,
    resolve_live_shared_agent_names,
    resolve_team_materializable_agent_names,
)
from mindroom.teams.prompt import render_leader_system_messages
from mindroom.teams.spec import MemberBuild, TeamMember, TeamSpec
from mindroom.tool_system.runtime_context import current_plugins, member_tool_scope
from mindroom.turn_model import ModelScope, resolve_turn_model

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.model_loop import LoopHooks
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.types import ResolvedHistorySettings
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.search import AgentKnowledge
    from mindroom.knowledge.utils import KnowledgeAvailabilityDetail
    from mindroom.runtime_protocols import OrchestratorRuntime
    from mindroom.teams.spec import TeamMode
    from mindroom.tool_jobs.execution import JobActor
    from mindroom.tool_system.invocation import ToolCall, ToolMiddleware, ToolNext
    from mindroom.tool_system.plugins import ConfigPlugins
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


@dataclass(frozen=True)
class ResolvedExactTeamMembers:
    """One exact requested member set: each member's config name, display name, and model for this turn, and its build.

    ``build(agent_name, model_name)`` builds one member on ``model_name`` under this set's settings and knowledge,
    blocking; a team builds each member on its first delegation (``TeamMember.ensure_built``). ``plugins`` is the view
    the turn captured, which the members' tools and the leader's job hooks both come from.
    """

    requested_agent_names: list[str]
    display_names: list[str]
    model_names: dict[str, str]
    plugins: ConfigPlugins = field(repr=False, compare=False)
    build: Callable[[str, str], AgentBuild] = field(repr=False, compare=False)


def resolve_exact_team_members(
    requested_agent_names: list[str],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    active_model_names: Mapping[str, str],
    include_openai_compat_guidance: bool = False,
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] | None = None,
    supports_native_tool_approval: bool = False,
    required_tool_names: Mapping[str, tuple[str, ...]] | None = None,
) -> ResolvedExactTeamMembers:
    """Resolve the exact team-member set: each member's model and knowledge, and its build.

    Each member's knowledge resolves now, so ``unavailable_bases`` collects what the leader's turn is told is
    unavailable; the members themselves build on their first delegation.
    """
    knowledge: dict[str, AgentKnowledge | None] = {}
    for agent_name in requested_agent_names:
        knowledge_resolution = resolve_agent_knowledge_access(
            agent_name,
            config,
            runtime_paths,
            refresh_scheduler=refresh_scheduler,
            execution_identity=execution_identity,
        )
        if unavailable_bases is not None:
            unavailable_bases.update(knowledge_resolution.unavailable)
        knowledge[agent_name] = knowledge_resolution.knowledge
    model_names = {agent_name: active_model_names[agent_name] for agent_name in requested_agent_names}
    plugins = current_plugins(config, runtime_paths)

    def build(agent_name: str, model_name: str) -> AgentBuild:
        # The team turn froze each member's model; the build runs it as chosen.
        model = resolve_turn_model(config, runtime_paths, ModelScope(entity=agent_name, explicit=model_name), None)
        return create_agent(
            agent_name,
            config,
            runtime_paths,
            # The member's toolkits act for the member, still answering through the turn's account.
            execution_identity=(
                replace(execution_identity, agent_name=agent_name) if execution_identity is not None else None
            ),
            model=model,
            knowledge=knowledge[agent_name],
            plugins=plugins,
            include_interactive_questions=False,
            include_openai_compat_guidance=include_openai_compat_guidance,
            refresh_scheduler=refresh_scheduler,
            supports_native_tool_approval=supports_native_tool_approval,
            required_tool_names=(required_tool_names or {}).get(agent_name, ()),
        )

    return ResolvedExactTeamMembers(
        requested_agent_names=requested_agent_names,
        display_names=[config.get_agent(agent_name).display_name for agent_name in requested_agent_names],
        model_names=model_names,
        plugins=plugins,
        build=build,
    )


def requested_team_agent_names(agent_names: Sequence[str]) -> list[str]:
    """Return the requested team members, excluding router placeholders."""
    return [name for name in agent_names if name != ROUTER_AGENT_NAME]


def resolve_team_members(
    agent_names: list[str],
    orchestrator: OrchestratorRuntime,
    execution_identity: ToolExecutionIdentity | None,
    *,
    config: Config,
    configured_team_name: str | None,
    reason_prefix: str,
    active_model_names: Mapping[str, str],
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] | None = None,
) -> ResolvedExactTeamMembers:
    """Resolve the exact requested team-member set under the turn's ``config`` without silent fallback.

    The orchestrator says which shared agents are running now; a requested member that is not, or that this request
    may not reach, rejects the set with a ``ValueError`` naming it under ``reason_prefix``, before anything resolves.
    """
    requested_agent_names = requested_team_agent_names(agent_names)
    materializable_agent_names = resolve_team_materializable_agent_names(
        config,
        resolve_live_shared_agent_names(orchestrator),
        allow_direct_private_agents=allow_direct_private_team_agents(
            execution_identity,
            configured_team_name=configured_team_name,
        ),
    )
    if materializable_agent_names is not None:
        missing_agent_names = [name for name in requested_agent_names if name not in materializable_agent_names]
        if missing_agent_names:
            raise ValueError(not_materializable_team_agents_message(missing_agent_names, prefix=reason_prefix))
    return resolve_exact_team_members(
        requested_agent_names,
        config=config,
        runtime_paths=orchestrator.runtime_paths,
        execution_identity=execution_identity,
        unavailable_bases=unavailable_bases,
        refresh_scheduler=orchestrator.knowledge_refresh_scheduler,
        supports_native_tool_approval=True,
        active_model_names=active_model_names,
    )


def allow_direct_private_team_agents(
    execution_identity: ToolExecutionIdentity | None,
    *,
    configured_team_name: str | None,
) -> bool:
    """Return whether direct private members may join this team request."""
    return (
        configured_team_name is None
        and execution_identity is not None
        and execution_identity.channel == "matrix"
        and bool(execution_identity.requester_id)
    )


def materialized_team_label(display_names: Sequence[str]) -> str:
    """Return the label of a team of these members, its leader's name.

    An ad hoc team's turn and resume carry it as their entity label in logs and notices.
    """
    return f"Team ({', '.join(display_names)})"


def _team_member(
    agent_name: str,
    members: ResolvedExactTeamMembers,
    system_enrichment: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> TeamMember:
    """Return one member as the leader reads it from config, built on its first delegation (``_member_build``)."""
    agent_config = config.get_agent(agent_name)
    return TeamMember(
        id=agent_name,
        name=agent_config.display_name,
        role=agent_config.role,
        build=partial(
            _member_build,
            agent_name,
            members,
            system_enrichment,
            config=config,
            runtime_paths=runtime_paths,
        ),
    )


def _member_build(
    agent_name: str,
    members: ResolvedExactTeamMembers,
    system_enrichment: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> MemberBuild:
    """Build one member as the team runs it: this turn's system messages, its loop hooks, and its toolkit owners.

    A member runs under the agent hook policy: its own tool-call policy, output-limit policy, portable tool search,
    mid-turn compaction, and the approval receipt hook; its tool-call policy and tools run under its own identity. It
    replays no history, so its compaction stays within the run, and the leader owns the queued-message notices of the
    turn. The member runs on its configured model for this turn (``members.model_names``), whose window its compaction
    sizes against.
    """
    model_name = members.model_names[agent_name]
    build = members.build(agent_name, model_name)
    as_member = _as_member(build.spec.id, model_name)
    turn = HookTurn(
        config=config,
        runtime_paths=runtime_paths,
        model_name=model_name,
        storage=None,
        replay_plan=None,
        prompt_id=None,
    )

    def new_hooks() -> LoopHooks:
        hooks = agent_loop_hooks(build, turn, queued_notices=False)
        # Outermost, so job admission and the managed job a call becomes, which inherits its context, act as the member.
        return replace(hooks, tool_middleware=(as_member, *hooks.tool_middleware))

    return MemberBuild(
        spec=replace(build.spec, system_messages=prompt_for_turn(build, system_enrichment)),
        new_hooks=new_hooks,
        request_template=build.request_template,
        toolkit_owners=tool_owners_by_agent(build),
    )


def _as_member(member_id: str, model_name: str) -> ToolMiddleware:
    """Return the outermost step around a member's calls, which runs the rest under the member's own identity.

    Tools that act for the running agent, such as ``todo``'s default assignee and workspace templates or
    ``claude_agent``'s sessions and model, and the policy's plugin tool hooks read it from the tool runtime context,
    so each call runs its policy and reaches its entrypoint with the member's name and the model it runs on.
    """

    async def run_as_member(call: ToolCall, call_next: ToolNext) -> object:
        with member_tool_scope(member_id, model_name):
            return await call_next(call.arguments)

    return run_as_member


@dataclass(frozen=True)
class BuiltTeam:
    """One team ready to run: its spec, its history settings, the tool-call policy the leader's own tools run under, and the team's jobs.

    ``leader_jobs`` is the team as tool jobs see it while the instance runs them for a Matrix owner. ``timezone``
    dates the turn: the leader's date line and the current message's time.
    """

    spec: TeamSpec
    history_settings: ResolvedHistorySettings
    timezone: str
    leader_middleware: tuple[ToolMiddleware, ...]
    leader_jobs: JobActor | None


def _leader_identity(
    config: Config,
    *,
    display_name: str,
    team_purpose: str,
    include_openai_compat_guidance: bool,
) -> str:
    """Render the leader's identity for its channel, as an agent's is rendered: Matrix, or the OpenAI-compatible API."""
    matrix_identity = render_prompt_template(
        config.get_prompt("TEAM_LEADER_IDENTITY_CONTEXT_TEMPLATE"),
        display_name=display_name,
        team_purpose=team_purpose,
    )
    if not include_openai_compat_guidance:
        return matrix_identity
    return openai_compat_identity(
        config.prompts,
        openai_identity=render_prompt_template(
            config.get_prompt("OPENAI_COMPAT_TEAM_LEADER_IDENTITY_CONTEXT_TEMPLATE"),
            display_name=display_name,
            team_purpose=team_purpose,
            openai_compat_history_guidance=config.get_prompt("OPENAI_COMPAT_HISTORY_GUIDANCE"),
        ),
        matrix_identity=matrix_identity,
        openai_prompt_name="OPENAI_COMPAT_TEAM_LEADER_IDENTITY_CONTEXT_TEMPLATE",
        matrix_prompt_name="TEAM_LEADER_IDENTITY_CONTEXT_TEMPLATE",
    )


def build_team_spec(
    *,
    members: ResolvedExactTeamMembers,
    mode: TeamMode,
    config: Config,
    runtime_paths: RuntimePaths,
    scope_context: ScopeSessionContext,
    execution_identity: ToolExecutionIdentity | None,
    model_name: str,
    configured_team_name: str | None,
    timezone: str,
    system_enrichment: str = "",
    include_openai_compat_guidance: bool = False,
) -> BuiltTeam:
    """Build the team the leader loop runs: its model, prompt, job controls, call budget, and members.

    The leader is named by the team's label (``materialized_team_label``) and identified by the history scope of
    ``scope_context``. The leader prompt opens with the team's identity, the configured team's name and purpose or the
    ad hoc team's label, in the OpenAI-compatible API's form with ``include_openai_compat_guidance``, then holds each
    member's authored role and the leader's tool instructions in offer order, then the date and session context in
    ``timezone`` with ``system_enrichment`` appended; the team's own history settings and call budget come from the
    configured team ``configured_team_name``, or the defaults when it is None for an ad hoc one.
    """
    model = provider_registry.build_model_provider(
        config,
        runtime_paths,
        model_name,
        execution_identity=execution_identity,
    )
    label = materialized_team_label(members.display_names)
    team_scope = config.resolve_scope(configured_team_name)
    jobs = team_background_jobs(config, runtime_paths, execution_identity, members.plugins)
    team_members = tuple(
        _team_member(agent_name, members, system_enrichment, config=config, runtime_paths=runtime_paths)
        for agent_name in members.requested_agent_names
    )
    leader = AgentSpec(
        id=scope_context.scope.scope_id,
        name=label,
        model=model,
        system_messages=(),
        toolkits=() if jobs is None else (jobs.controls,),
        max_tool_calls_per_turn=team_scope.max_tool_calls_per_turn,
    )
    turn_context = appended_context(render_session_context(config, timezone), system_enrichment)
    team_config = config.teams.get(configured_team_name) if configured_team_name is not None else None
    system_messages = render_leader_system_messages(
        team_members,
        mode,
        identity=_leader_identity(
            config,
            display_name=label if team_config is None else team_config.display_name,
            team_purpose="" if team_config is None else f"Team purpose: {team_config.role}\n",
            include_openai_compat_guidance=include_openai_compat_guidance,
        ),
        markdown=config.defaults.markdown,
        tool_instructions=toolkit_instructions(leader.toolkits),
        turn_context=turn_context,
    )
    return BuiltTeam(
        spec=TeamSpec(leader=replace(leader, system_messages=system_messages), mode=mode, members=team_members),
        history_settings=team_scope.history_settings,
        timezone=timezone,
        leader_middleware=() if jobs is None else jobs.middleware,
        leader_jobs=None if jobs is None else jobs.actor,
    )


__all__ = [
    "BuiltTeam",
    "ResolvedExactTeamMembers",
    "allow_direct_private_team_agents",
    "build_team_spec",
    "materialized_team_label",
    "requested_team_agent_names",
    "resolve_exact_team_members",
    "resolve_team_members",
]
