"""Building a team: its exact members as agent builds, the leader's and members' models, and the team spec.

``build_team_spec`` turns the materialized members into the team the leader loop runs: the leader's model, prompt,
job controls, and call budget, and each member's system message, roster role, and loop hooks.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.agent_loop.build import appended_context, prompt_for_turn
from mindroom.agent_loop.prompt import model_note_for
from mindroom.agent_loop.spec import AgentSpec
from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.agents import create_agent, team_background_jobs
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.history.session_context import resolve_bound_team_scope
from mindroom.knowledge.utils import resolve_agent_knowledge_access
from mindroom.logging_config import get_logger
from mindroom.loop_hooks import HookTurn, agent_loop_hooks
from mindroom.providers import registry as provider_registry
from mindroom.system_prompt import render_date_context
from mindroom.teams.formation import (
    not_materializable_team_agents_message,
    resolve_live_shared_agent_names,
    resolve_team_materializable_agent_names,
)
from mindroom.teams.prompt import render_leader_system_message
from mindroom.teams.spec import TeamMember, TeamSpec
from mindroom.thread_models import resolve_thread_model_override
from mindroom.tool_system.runtime_context import get_tool_runtime_context, tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.model_loop import LoopHooks
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.types import ResolvedHistorySettings
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.utils import KnowledgeAvailabilityDetail
    from mindroom.runtime_protocols import OrchestratorRuntime
    from mindroom.teams.spec import TeamMode
    from mindroom.tool_jobs.execution import JobActor
    from mindroom.tool_system.invocation import ToolCall, ToolMiddleware, ToolNext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


@dataclass(frozen=True)
class ResolvedExactTeamMembers:
    """One exact requested member set after runtime materialization attempts."""

    requested_agent_names: list[str]
    builds: list[AgentBuild]
    display_names: list[str]
    failed_agent_names: list[str]
    model_names: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TeamTurnModelSelection:
    """Model aliases frozen together for one team turn."""

    team_model_name: str
    member_model_names: dict[str, str]


def materialize_exact_requested_team_members(
    requested_agent_names: list[str],
    *,
    materializable_agent_names: set[str] | None,
    build_member: Callable[[str], AgentBuild],
) -> ResolvedExactTeamMembers:
    """Materialize the exact requested team members without silent shrinkage."""
    if materializable_agent_names is not None:
        missing_agent_names = [name for name in requested_agent_names if name not in materializable_agent_names]
        if missing_agent_names:
            return ResolvedExactTeamMembers(
                requested_agent_names=requested_agent_names,
                builds=[],
                display_names=[],
                failed_agent_names=missing_agent_names,
            )

    builds: list[AgentBuild] = []
    failed_agent_names: list[str] = []
    for name in requested_agent_names:
        try:
            build = build_member(name)
        except Exception:
            logger.warning(
                "Failed to materialize exact team member",
                agent_name=name,
                exc_info=True,
            )
            failed_agent_names.append(name)
            continue
        builds.append(build)

    return ResolvedExactTeamMembers(
        requested_agent_names=requested_agent_names,
        builds=builds,
        display_names=[build.spec.name for build in builds],
        failed_agent_names=failed_agent_names,
    )


def materialize_exact_team_members(
    requested_agent_names: list[str],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    include_openai_compat_guidance: bool = False,
    materializable_agent_names: set[str] | None = None,
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    reason_prefix: str = "Team request",
    supports_native_tool_approval: bool = False,
    active_model_names: Mapping[str, str] | None = None,
    required_tool_names: Mapping[str, tuple[str, ...]] | None = None,
) -> ResolvedExactTeamMembers:
    """Materialize the exact team-member set without silent fallback."""
    selected_model_names: dict[str, str] = {}

    def _build_member(agent_name: str) -> AgentBuild:
        knowledge_resolution = resolve_agent_knowledge_access(
            agent_name,
            config,
            runtime_paths,
            refresh_scheduler=refresh_scheduler,
            execution_identity=execution_identity,
        )
        if unavailable_bases is not None:
            unavailable_bases.update(knowledge_resolution.unavailable)
        model_name = (
            active_model_names[agent_name]
            if active_model_names is not None
            else config.resolve_runtime_model(
                entity_name=agent_name,
                room_id=execution_identity.room_id if execution_identity is not None else None,
                thread_id=execution_identity.resolved_thread_id if execution_identity is not None else None,
                runtime_paths=runtime_paths,
            ).model_name
        )
        selected_model_names[agent_name] = model_name
        return create_agent(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
            active_model_name=model_name,
            knowledge=knowledge_resolution.knowledge,
            include_interactive_questions=False,
            include_openai_compat_guidance=include_openai_compat_guidance,
            refresh_scheduler=refresh_scheduler,
            supports_native_tool_approval=supports_native_tool_approval,
            required_tool_names=(required_tool_names or {}).get(agent_name, ()),
        )

    team_members = materialize_exact_requested_team_members(
        requested_agent_names,
        materializable_agent_names=materializable_agent_names,
        build_member=_build_member,
    )
    if team_members.failed_agent_names:
        raise ValueError(
            not_materializable_team_agents_message(team_members.failed_agent_names, prefix=reason_prefix),
        )
    return replace(team_members, model_names=selected_model_names)


def requested_team_agent_names(agent_names: Sequence[str]) -> list[str]:
    """Return the requested team members, excluding router placeholders."""
    return [name for name in agent_names if name != ROUTER_AGENT_NAME]


def resolve_team_member_model_names(
    agent_names: Sequence[str],
    room_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    thread_id: str | None = None,
) -> dict[str, str]:
    """Resolve every exact team member's model alias without yielding control."""
    return {
        agent_name: config.resolve_runtime_model(
            entity_name=agent_name,
            room_id=room_id,
            thread_id=thread_id,
            runtime_paths=runtime_paths,
        ).model_name
        for agent_name in requested_team_agent_names(agent_names)
    }


def materialize_team_members(
    agent_names: list[str],
    orchestrator: OrchestratorRuntime,
    execution_identity: ToolExecutionIdentity | None,
    *,
    configured_team_name: str | None = None,
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] | None = None,
    reason_prefix: str = "Team request",
    active_model_names: Mapping[str, str] | None = None,
) -> ResolvedExactTeamMembers:
    """Materialize the exact requested team-member set from the live orchestrator without silent fallback."""
    assert orchestrator.config is not None
    materializable_agent_names = resolve_team_materializable_agent_names(
        orchestrator.config,
        resolve_live_shared_agent_names(orchestrator),
        allow_direct_private_agents=allow_direct_private_team_agents(
            execution_identity,
            configured_team_name=configured_team_name,
        ),
    )
    return materialize_exact_team_members(
        requested_team_agent_names(agent_names),
        config=orchestrator.config,
        runtime_paths=orchestrator.runtime_paths,
        execution_identity=execution_identity,
        materializable_agent_names=materializable_agent_names,
        unavailable_bases=unavailable_bases,
        refresh_scheduler=orchestrator.knowledge_refresh_scheduler,
        supports_native_tool_approval=True,
        active_model_names=active_model_names,
        reason_prefix=reason_prefix,
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


def select_model_for_team(
    team_name: str,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    thread_id: str | None = None,
) -> str:
    """Return the model for a team in a room: the thread override, the persisted or configured room model, else the team's."""
    model_name = config.resolve_runtime_model(
        entity_name=team_name,
        room_id=room_id,
        thread_id=thread_id,
        runtime_paths=runtime_paths,
    ).model_name
    logger.info("selected_team_model", team_name=team_name, model_name=model_name)
    return model_name


def resolve_team_turn_models(
    team_name: str,
    member_names: Sequence[str],
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    thread_id: str | None = None,
    active_model_name: str | None = None,
) -> TeamTurnModelSelection:
    """Freeze the coordinator and member model aliases in one synchronous snapshot."""
    # A configured team's access reaches its members, so its thread override governs them during its turns.
    if active_model_name is None and thread_id is not None and team_name in config.teams:
        active_model_name = resolve_thread_model_override(runtime_paths, thread_id, config=config).active.get(team_name)
    if active_model_name is not None:
        return TeamTurnModelSelection(
            team_model_name=active_model_name,
            member_model_names=dict.fromkeys(member_names, active_model_name),
        )
    return TeamTurnModelSelection(
        team_model_name=select_model_for_team(
            team_name,
            room_id,
            config,
            runtime_paths,
            thread_id=thread_id,
        ),
        member_model_names=resolve_team_member_model_names(
            member_names,
            room_id,
            config,
            runtime_paths,
            thread_id=thread_id,
        ),
    )


def team_label(member_names: Sequence[str]) -> str:
    """Return the stored ``team_name`` of a team of these members, kept as main stores it."""
    return f"Team-{'-'.join(member_names)}"


def materialized_team_label(display_names: Sequence[str]) -> str:
    """Return the entity label an ad hoc team's turn and resume carry in their logs and notices."""
    return f"Team ({', '.join(display_names)})"


def configured_team(config: Config, team_name: str | None) -> str | None:
    """Return the configured team ``team_name`` names, or None for an ad hoc team.

    A saved approval names the team as the entity whose account handles it, which for an ad hoc team is a member;
    an ad hoc team compacts and stores as no configured entity, under the defaults.
    """
    return team_name if team_name in config.teams else None


def _team_member(
    build: AgentBuild,
    system_enrichment: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    model_name: str,
) -> TeamMember:
    """Return one member as the team runs it: this turn's system message, the roster role, and its loop hooks.

    A member runs under the agent hook policy: its own tool-call policy, output-limit policy, portable tool search,
    mid-turn compaction, and the approval receipt hook; its tools run under its own identity. It replays no history,
    so its compaction stays within the run, and the leader owns the queued-message notices of the turn.
    ``model_name`` is the member's configured model for this turn, whose window its compaction sizes against.
    """
    # The binding joins the policy a managed job runs again, so a member's call that becomes a job runs as the member.
    as_member = replace(build, tool_policy=(*build.tool_policy, _as_member(build.spec.id, model_name)))
    turn = HookTurn(
        config=config,
        runtime_paths=runtime_paths,
        model_name=model_name,
        storage=None,
        replay_plan=None,
        prompt_id=None,
    )

    def new_hooks() -> LoopHooks:
        return agent_loop_hooks(as_member, turn, queued_notices=False)

    return TeamMember(
        spec=replace(build.spec, system_message=prompt_for_turn(build, system_enrichment)),
        role=build.prompt.role or "",
        new_hooks=new_hooks,
        request_template=build.request_template,
    )


def _as_member(member_id: str, model_name: str) -> ToolMiddleware:
    """Return the innermost step of a member's tool calls, which runs the tool under the member's own identity.

    The turn's tool runtime context names the team. Tools that act for the running agent, such as ``todo``'s default
    assignee and workspace templates or ``claude_agent``'s sessions and model, read it from that context, so each
    call reaches its entrypoint with the member's name and the model it runs on. The tool-call policy around the
    step still sees the turn's context, kept as main behaves.
    """

    async def run_as_member(call: ToolCall, call_next: ToolNext) -> object:
        context = get_tool_runtime_context()
        if context is None:
            return await call_next(call.arguments)
        with tool_runtime_context(replace(context, agent_name=member_id, active_model_name=model_name)):
            return await call_next(call.arguments)

    return run_as_member


@dataclass(frozen=True)
class BuiltTeam:
    """One team ready to run: its spec, its history settings, the tool-call policy the leader's own tools run under, and the team's jobs.

    ``leader_jobs`` is the team as tool jobs see it while the instance runs them for a Matrix owner.
    """

    spec: TeamSpec
    history_settings: ResolvedHistorySettings
    leader_middleware: tuple[ToolMiddleware, ...]
    leader_jobs: JobActor | None = None


def build_team_spec(
    *,
    members: ResolvedExactTeamMembers,
    mode: TeamMode,
    config: Config,
    runtime_paths: RuntimePaths,
    team_display_name: str,
    scope_context: ScopeSessionContext | None,
    execution_identity: ToolExecutionIdentity | None,
    model_name: str,
    configured_team_name: str | None,
    system_enrichment: str = "",
) -> BuiltTeam:
    """Build the team the leader loop runs: its model, prompt, job controls, call budget, and members.

    The leader prompt holds each member's roster role, the leader's tool instructions in offer order, the provider
    family's note, and the date context with ``system_enrichment`` appended; the team's own history settings and
    call budget come from the configured team ``configured_team_name``, or the defaults when it is None for an ad
    hoc one.
    """
    model = provider_registry.build_model_provider(
        config,
        runtime_paths,
        model_name,
        execution_identity=execution_identity,
    )
    team_view = config.resolve_entity(configured_team_name)
    if scope_context is not None:
        team_id = scope_context.scope.scope_id
    else:
        scope = resolve_bound_team_scope(
            member_names=members.requested_agent_names,
            config=config,
            team_name=configured_team_name,
            execution_identity=execution_identity,
        )
        team_id = scope.scope_id if scope is not None else team_display_name
    jobs = team_background_jobs(config, runtime_paths, execution_identity)
    leader_toolkits = () if jobs is None else (jobs.controls,)
    team_members = tuple(
        _team_member(
            build,
            system_enrichment,
            config=config,
            runtime_paths=runtime_paths,
            model_name=members.model_names[build.spec.id],
        )
        for build in members.builds
    )
    prepared = prepare_run_tools(
        AgentSpec(id=team_id, name=team_display_name, model=model, system_message=None, toolkits=leader_toolkits),
    )
    additional_context = appended_context(
        render_date_context(config.timezone, datetime_context_template=config.get_prompt("DATETIME_CONTEXT_TEMPLATE")),
        system_enrichment,
    )
    system_message = render_leader_system_message(
        team_members,
        mode,
        tool_instructions=prepared.instructions,
        # The delegation tool is always offered, so every leader request offers tools.
        model_note=model_note_for(model.capabilities, offers_tools=True),
        additional_context=additional_context,
    )
    spec = TeamSpec(
        id=team_id,
        name=team_display_name,
        mode=mode,
        model=model,
        system_message=system_message,
        toolkits=leader_toolkits,
        max_tool_calls_per_turn=team_view.max_tool_calls_per_turn,
        members=team_members,
    )
    return BuiltTeam(
        spec=spec,
        history_settings=team_view.history_settings,
        leader_middleware=() if jobs is None else jobs.middleware,
        leader_jobs=None if jobs is None else jobs.actor,
    )


__all__ = [
    "BuiltTeam",
    "ResolvedExactTeamMembers",
    "TeamTurnModelSelection",
    "allow_direct_private_team_agents",
    "build_team_spec",
    "configured_team",
    "materialize_exact_requested_team_members",
    "materialize_exact_team_members",
    "materialize_team_members",
    "materialized_team_label",
    "requested_team_agent_names",
    "resolve_team_member_model_names",
    "resolve_team_turn_models",
    "select_model_for_team",
    "team_label",
]
