"""Team history of configured teams and ad-hoc team scopes, written and read through MindRoom's history code."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agno.agent import Agent
from agno.metrics import RunMetrics
from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput, RunStartedEvent
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement
from agno.run.team import RunStartedEvent as TeamRunStartedEvent
from agno.run.team import TeamRunOutput
from agno.session.team import TeamSession

from mindroom import constants
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.history import summary_rows
from mindroom.history.replay import current_summary_text, scope_visible_runs
from mindroom.history.session_context import (
    open_bound_scope_session_context,
)
from mindroom.history.storage import (
    add_pending_force_compaction_scope,
    has_queued_force_compaction,
    read_scope_seen_event_ids,
    read_scope_state,
    set_force_compaction_state,
    update_scope_seen_event_ids,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import seed_session

if TYPE_CHECKING:
    from collections.abc import Iterator

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


@contextmanager
def team_context(runtime_paths: RuntimePaths, config: Config, team: TeamScope) -> Iterator[ScopeSessionContext]:
    """Open a team's history the way a live team run of its members does, scope resolution included."""
    with open_bound_scope_session_context(
        agents=[Agent(id=member, name=member.title()) for member in team.members],
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
        create_session_if_missing=True,
    ) as context:
        assert context is not None
        yield context


def member_run_output(member: str, team_run_id: str) -> RunOutput:
    """Return a member's completed run inside a team run."""
    run_id = f"{team_run_id}-{member}"
    return RunOutput(
        run_id=run_id,
        agent_id=member,
        agent_name=member.title(),
        parent_run_id=team_run_id,
        session_id=SESSION,
        user_id=ALICE,
        status=RunStatus.completed,
        content=f"{member} did {team_run_id}",
        messages=[Message(role="user", content=f"task {team_run_id}"), Message(role="assistant", content="done")],
        events=[RunStartedEvent(agent_id=member, run_id=run_id, session_id=SESSION)],
    )


def team_run_output(scope_id: str, team: TeamScope, run_id: str) -> TeamRunOutput:
    """Return a completed team run with its member runs and a pending requirement."""
    requirement = RunRequirement(tool_execution=ToolExecution(tool_name="approve", tool_call_id=f"{run_id}-call"))
    requirement.member_agent_id = team.members[0]
    return TeamRunOutput(
        run_id=run_id,
        team_id=scope_id,
        team_name=f"Team-{'-'.join(team.members)}",
        session_id=SESSION,
        user_id=ALICE,
        status=RunStatus.completed,
        model="test-model",
        model_provider="test-provider",
        content=f"answer {run_id}",
        messages=[Message(role="user", content=f"question {run_id}"), Message(role="assistant", content="answer")],
        metrics=RunMetrics(input_tokens=2, output_tokens=3, total_tokens=5),
        member_responses=[member_run_output(member, run_id) for member in team.members],
        events=[TeamRunStartedEvent(team_id=scope_id, run_id=run_id, session_id=SESSION)],
        requirements=[requirement],
        metadata={constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}"]},
    )


def seed_team(runtime_paths: RuntimePaths, config: Config, team: TeamScope) -> None:
    """Write a team's history the way MindRoom does: two team runs with their member runs, the first summarized."""
    with team_context(runtime_paths, config, team) as context:
        scope, storage = context.scope, context.storage
        runs: list[RunOutput | TeamRunOutput] = []
        for run_id in ("r1", "r2"):
            team_run = team_run_output(scope.scope_id, team, run_id)
            # Agno saves each member's run into the team session as its own run, then the team's run.
            runs += [*team_run.member_responses, team_run]
        session = TeamSession(
            session_id=SESSION,
            team_id=scope.scope_id,
            user_id=ALICE,
            # Written like agno/team/_telemetry.py's get_team_data.
            team_data={"name": f"Team-{'-'.join(team.members)}", "team_id": scope.scope_id},
            session_data={"session_state": add_pending_force_compaction_scope({}, scope)},
            metadata={},
            runs=runs,
        )
        update_scope_seen_event_ids(session, scope, ["$preserved"])
        set_force_compaction_state(session, scope, read_scope_state(session, scope), force=True)
        seed_session(storage, session)
        covered = summary_rows.covered_runs(storage, session_id=SESSION, run_ids=["r1"])
        summary_rows.append_summary(
            storage,
            session_id=SESSION,
            scope_key=scope.key,
            based_on=None,
            covered=covered,
            summary=f"summary of {scope.scope_id}'s r1",
            summary_model="test-model",
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
    member_messages: list[str]


def team_history(runtime_paths: RuntimePaths, config: Config, team: TeamScope) -> TeamHistory:
    """Load a team's history through MindRoom's history code, under the scope its members resolve to now."""
    with team_context(runtime_paths, config, team) as context:
        session, scope = context.session, context.scope
        assert context.session_exists
        assert isinstance(session, TeamSession)
        replayed = scope_visible_runs(session, scope)
        return TeamHistory(
            scope_id=scope.scope_id,
            replayed=[run.run_id for run in replayed],
            summary=current_summary_text(session),
            seen=read_scope_seen_event_ids(context.storage, session, scope),
            queued=has_queued_force_compaction(context.storage, SESSION, scope),
            member_runs=[(run.agent_id, run.run_id) for run in session.runs or [] if isinstance(run, RunOutput)],
            member_responses=[
                [member.agent_id for member in run.member_responses if isinstance(member, RunOutput)]
                for run in replayed
                if isinstance(run, TeamRunOutput)
            ],
            requirement_members=[
                requirement.member_agent_id
                for run in replayed
                if isinstance(run, TeamRunOutput)
                for requirement in run.requirements or []
            ],
            # Agno's member filter, which finds a member's interactions in a team session by agent id.
            member_messages=[str(message.content) for message in session.get_messages(member_ids=[team.members[0]])],
        )
