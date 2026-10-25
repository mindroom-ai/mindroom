"""Team history of configured teams and ad-hoc team scopes, written and read through MindRoom's history code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom import constants
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.history import summary_rows
from mindroom.history.replay import current_summary_text, scope_visible_runs
from mindroom.history.session_context import load_bound_scope_session
from mindroom.history.storage import (
    add_pending_force_compaction_scope,
    has_queued_force_compaction,
    read_scope_seen_event_ids,
    set_force_compaction,
    update_scope_seen_event_ids,
)
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, TeamRunRecord, ToolExecution
from mindroom.llm.usage import RunMetrics, TokenCounts
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.agent_rename_fixtures.entity_state import add_agno_era_fields
from tests.conftest import persist_session

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.history.session_context import ScopeSessionContext


OLD = "code"
NEW = "coder"

# Its name extends the renamed agent's, so a substring match would wrongly touch its scopes.
PREFIX = "code_team"
HELPER = "helper"

# A private agent partitions every ad-hoc team it joins by requester.
DIARY = "diary"
ALICE = "@alice:x"
SESSION = "s1"

# A configured team with the renamed agent, and one without it.
CREW = "crew"
DOCS = "docs"


@dataclass(frozen=True)
class TeamScope:
    """One team history scope of the fixture: its members, and its configured name if it has one."""

    members: tuple[str, ...]
    team_name: str | None = None

    def renamed(self) -> TeamScope:
        """Return the scope with the renamed agent as a member in place of the old name."""
        return TeamScope(tuple(NEW if member == OLD else member for member in self.members), self.team_name)


AD_HOC = TeamScope((OLD, HELPER))
PRIVATE_AD_HOC = TeamScope((OLD, DIARY))
CONFIGURED = TeamScope((OLD, HELPER), CREW)
PREFIX_AD_HOC = TeamScope((PREFIX, HELPER))
OTHER_CONFIGURED = TeamScope((PREFIX, HELPER), DOCS)
TEAMS = (AD_HOC, PRIVATE_AD_HOC, CONFIGURED, PREFIX_AD_HOC, OTHER_CONFIGURED)


def team_config(agent: str) -> Config:
    """Return the config naming the renamed agent ``agent``, as it is before (``OLD``) or after (``NEW``)."""
    return Config(
        agents={
            agent: AgentConfig(display_name="Code"),
            PREFIX: AgentConfig(display_name="Code Team"),
            HELPER: AgentConfig(display_name="Helper"),
            DIARY: AgentConfig(display_name="Diary", private=AgentPrivateConfig(per="user")),
        },
        teams={
            CREW: TeamConfig(display_name="Crew", role="Ships releases", agents=[agent, HELPER]),
            DOCS: TeamConfig(display_name="Docs", role="Writes docs", agents=[PREFIX, HELPER]),
        },
    )


def team_context(runtime_paths: RuntimePaths, config: Config, team: TeamScope) -> ScopeSessionContext:
    """Open a team's history the way a live team run of its members does, scope resolution included."""
    context = load_bound_scope_session(
        member_names=team.members,
        session_id=SESSION,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name=team.members[0],
            requester_id=ALICE,
            room_id="!room:x",
            thread_id=None,
            resolved_thread_id=None,
            session_id=SESSION,
        ),
        team_name=team.team_name,
    )
    assert context is not None
    return context


def member_run(member: str, team_run_id: str) -> RunRecord:
    """Return a member's completed run inside a team run."""
    return RunRecord(
        run_id=f"{team_run_id}-{member}",
        agent_id=member,
        agent_display_name=member.title(),
        parent_run_id=team_run_id,
        session_id=SESSION,
        user_id=ALICE,
        status=RunStatus.completed,
        content=f"{member} did {team_run_id}",
        messages=[Message(role="user", content=f"task {team_run_id}"), Message(role="assistant", content="done")],
    )


def team_run(scope_id: str, team: TeamScope, run_id: str) -> TeamRunRecord:
    """Return a completed team run with its member runs and a requirement a member raised."""
    requirement = RunRequirement(
        tool_execution=ToolExecution(tool_name="approve", tool_call_id=f"{run_id}-call"),
        member_agent_id=team.members[0],
    )
    return TeamRunRecord(
        run_id=run_id,
        team_id=scope_id,
        session_id=SESSION,
        user_id=ALICE,
        status=RunStatus.completed,
        model="test-model",
        model_provider="test-provider",
        content=f"answer {run_id}",
        messages=[Message(role="user", content=f"question {run_id}"), Message(role="assistant", content="answer")],
        metrics=RunMetrics(tokens=TokenCounts(input_tokens=2, output_tokens=3, total_tokens=5)),
        member_responses=[member_run(member, run_id) for member in team.members],
        requirements=[requirement],
        metadata={constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}"]},
    )


def seed_team(runtime_paths: RuntimePaths, config: Config, team: TeamScope) -> None:
    """Write a team's history the way MindRoom does: two team runs with their member runs, the first summarized."""
    context = team_context(runtime_paths, config, team)
    scope, storage = context.scope, context.storage
    session = context.first_session()
    session.session_state = add_pending_force_compaction_scope({}, scope)
    for run_id in ("r1", "r2"):
        record = team_run(scope.scope_id, team, run_id)
        # Each member's run is stored in the team session as its own row, then the team's run.
        session.runs += [*record.member_responses, record]
    persist_session(storage, session)
    update_scope_seen_event_ids(storage, session, scope, ["$preserved"])
    set_force_compaction(storage, session=session, scope=scope, forced=True)
    covered = summary_rows.covered_runs(storage, session_id=SESSION, run_ids=["r1"])
    summary_rows.append_summary(
        storage,
        session=session,
        scope_key=scope.key,
        based_on=None,
        covered=covered,
        summary=f"summary of {scope.scope_id}'s r1",
        summary_model="test-model",
    )
    add_agno_era_fields(
        storage.path,
        storage.names.sessions,
        owner_field="team_id",
        owner=scope.scope_id,
        display_name=f"Team-{'-'.join(team.members)}",
    )


def install_teams(runtime_paths: RuntimePaths) -> RuntimePaths:
    """Write the history of every team scope of the fixture."""
    for team in TEAMS:
        seed_team(runtime_paths, team_config(OLD), team)
    return runtime_paths


@dataclass(frozen=True)
class TeamHistory:
    """A team's history as MindRoom's history code loads it."""

    scope_id: str
    replayed: list[str | None]
    summary: str | None
    seen: set[str]
    queued: bool
    member_runs: list[tuple[str | None, str | None]]
    member_responses: list[list[str | None]]
    requirement_members: list[str | None]


def team_history(runtime_paths: RuntimePaths, config: Config, team: TeamScope) -> TeamHistory:
    """Load a team's history through MindRoom's history code, under the scope its members resolve to now."""
    context = team_context(runtime_paths, config, team)
    session, scope = context.session, context.scope
    assert session is not None
    assert session.kind == "team"
    replayed = scope_visible_runs(session, scope)
    return TeamHistory(
        scope_id=scope.scope_id,
        replayed=[run.run_id for run in replayed],
        summary=current_summary_text(session),
        seen=read_scope_seen_event_ids(context.storage, session, scope),
        queued=has_queued_force_compaction(context.storage, SESSION, scope),
        member_runs=[(run.agent_id, run.run_id) for run in session.runs if isinstance(run, RunRecord)],
        member_responses=[
            [member.agent_id for member in run.member_responses] for run in replayed if isinstance(run, TeamRunRecord)
        ],
        requirement_members=[
            requirement.member_agent_id
            for run in replayed
            if isinstance(run, TeamRunRecord)
            for requirement in run.requirements or []
        ],
    )
