"""The ``squad`` and ``panel`` teams of the storage goldens, and the helpers that run and resume them.

The capture ran ``Team-writer-checker`` with a scripted ``Synthetic`` provider: the writer has MindRoom's calculator,
the checker the same calculator with ``multiply`` under the approval policy, and the leader only the delegation tool.
``pre_resume_runs`` reads a team run Agno stored paused, which the resume tests continue.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, NO_HOOKS
from mindroom.agent_loop.spec import AgentSpec, RunContext, UnstoredSession
from mindroom.config.main import Config
from mindroom.custom_tools.calculator import CalculatorTools
from mindroom.event_journal.approval_calls import ApprovalCall, approval_arguments_digest
from mindroom.llm.events import RunFinished
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity
from mindroom.llm.runs import TeamRunRecord
from mindroom.teams.members import BuiltTeam, ResolvedExactTeamMembers
from mindroom.teams.runtime import TeamDecisions, continue_team, run_team
from mindroom.teams.spec import MemberBuild, TeamMember, TeamMode, TeamSpec
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from tests.fakes.scripted_model import ScriptedModel
from tests.run_record_helpers import PROVIDER

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.run import ApprovedCall, Persist
    from mindroom.llm.provider import ModelProvider, ModelRequest
    from mindroom.llm.runs import AnyRunRecord, RunRecord
    from mindroom.teams.runtime import TeamCallKey, TeamRunEvent
    from mindroom.tool_system.tool_function import AdaptedToolkit

STORAGE = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "storage"
SQUAD = "!room:example.org:$squad"
SQUAD_APPROVED = "!room:example.org:$squad-approved"
PANEL = "!room:example.org:$panel"
REQUESTER = "@alice:example.org"
TEAM_LABEL = "Team (Writer, Checker)"
# The checker's piece of a collaborate result while the checker is paused.
CHECKER_PAUSED_PIECE = "Agent Checker: Requires human input before continuing."


def pre_resume_runs(dump: str) -> dict[str, dict[str, Any]]:
    """Return the run rows of one pre-resume dump by run id, as the resume read them."""
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript((STORAGE / f"{dump}.sql").read_text(encoding="utf-8"))
        [table] = [
            name
            for (name,) in connection.execute(
                "select name from sqlite_master where type = 'table' and name like '%_sessions_runs'",
            )
        ]
        rows = connection.execute(f"select run_id, run_data from {table}")  # noqa: S608 - the dump's own table name
        return {run_id: json.loads(data) for run_id, data in rows}


def scripted(model_id: str, *turns: list[Any]) -> ScriptedModel:
    """Return a scripted model with the capture's provider identity."""
    return ScriptedModel(list(turns), identity=ModelIdentity(id=model_id, provider=PROVIDER, provider_name=PROVIDER))


def calculator(*, gate_multiply: bool) -> AdaptedToolkit:
    """MindRoom's calculator, with ``multiply`` under the approval policy as the capture's config gated it."""
    adapted = adapt_toolkit(CalculatorTools())
    if not gate_multiply:
        return adapted
    return replace(
        adapted,
        functions=tuple(
            replace(function, requires_confirmation=True, approval_type="mindroom_policy")
            if function.name == "multiply"
            else function
            for function in adapted.functions
        ),
    )


def member(
    agent_id: str,
    name: str,
    model: ModelProvider,
    *toolkits: AdaptedToolkit,
    system: str | None = None,
) -> TeamMember:
    """Return one squad member; ``system`` defaults to a short prompt."""
    spec = AgentSpec(
        id=agent_id,
        name=name,
        model=model,
        system_messages=(Message(role="system", content=system or f"You are {name}."),),
        toolkits=toolkits,
    )
    build = MemberBuild(spec=spec, new_hooks=lambda: NO_HOOKS, request_template=EMPTY_REQUEST, toolkit_owners={})
    return TeamMember(id=agent_id, name=name, role=f"{name} role.", build=lambda: build)


def resolved_members(display_names: Mapping[str, str], *, model_name: str = "default") -> ResolvedExactTeamMembers:
    """Return the resolved set of these members, display names by config name, on ``model_name``; none ever builds."""

    def build(agent_name: str, model_name: str) -> AgentBuild:
        msg = f"member {agent_name!r} on {model_name!r} is built only by a team turn"
        raise AssertionError(msg)

    return ResolvedExactTeamMembers(
        requested_agent_names=list(display_names),
        display_names=list(display_names.values()),
        model_names=dict.fromkeys(display_names, model_name),
        build=build,
    )


def team_spec(
    team_id: str,
    name: str,
    model: ModelProvider,
    system: str,
    members: Sequence[TeamMember],
    *,
    mode: TeamMode = TeamMode.COORDINATE,
    toolkits: Sequence[AdaptedToolkit] = (),
) -> TeamSpec:
    """Return a team of ``members`` led by ``model``, with the leader prompt ``system`` and no call budget."""
    leader = AgentSpec(
        id=team_id,
        name=name,
        model=model,
        system_messages=(Message(role="system", content=system),),
        toolkits=tuple(toolkits),
    )
    return TeamSpec(leader=leader, mode=mode, members=tuple(members))


def built_team(spec: TeamSpec) -> BuiltTeam:
    """Return ``spec`` as a built team under the default history settings in UTC, with no leader middleware or jobs."""
    return BuiltTeam(
        spec=spec,
        history_settings=Config().resolve_entity(None).history_settings,
        timezone="UTC",
        leader_middleware=(),
        leader_jobs=None,
    )


def squad(
    leader: ScriptedModel,
    writer: ModelProvider,
    checker: ModelProvider,
    *,
    mode: TeamMode = TeamMode.COORDINATE,
    team_id: str = "squad",
    leader_toolkits: Sequence[AdaptedToolkit] = (),
    writer_toolkits: Sequence[AdaptedToolkit] | None = None,
    checker_toolkits: Sequence[AdaptedToolkit] | None = None,
) -> TeamSpec:
    """Return the capture's team: the writer with the calculator, the checker with the gated calculator."""
    writer_tools = (calculator(gate_multiply=False),) if writer_toolkits is None else tuple(writer_toolkits)
    checker_tools = (calculator(gate_multiply=True),) if checker_toolkits is None else tuple(checker_toolkits)
    return team_spec(
        team_id,
        TEAM_LABEL,
        leader,
        "You lead the squad.",
        (
            member("writer", "Writer", writer, *writer_tools),
            member("checker", "Checker", checker, *checker_tools),
        ),
        mode=mode,
        toolkits=leader_toolkits,
    )


def run_context(run_id: str, session_id: str, metadata: dict[str, Any] | None = None) -> RunContext:
    """Return the team run's context for Alice's turn."""
    return RunContext(run_id=run_id, session=UnstoredSession(session_id), user_id=REQUESTER, metadata=metadata)


def prompt(text: str) -> list[Message]:
    """Return the leader's run input: one user message holding ``text``."""
    return [Message(role="user", content=text)]


def approval(call_id: str, name: str, arguments: dict[str, Any], *, agent: str) -> ApprovalCall:
    """Return the approval that authorizes one paused call."""
    return ApprovalCall(
        tool_call_id=call_id,
        tool_name=name,
        invoking_agent=agent,
        expires_at_ns=0,
        toolkit_name="calculator",
        arguments_digest=approval_arguments_digest(arguments),
    )


class Store:
    """A persist callback that remembers every record it was handed, as a snapshot, in order.

    ``members`` keeps the member runs' terminal records themselves, in the order they were stored.
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.members: list[RunRecord] = []

    async def __call__(self, record: AnyRunRecord) -> None:
        """Store ``record`` after yielding once, as a store that writes off the loop would."""
        await asyncio.sleep(0)
        self.records.append(record.to_dict())
        if not isinstance(record, TeamRunRecord):
            self.members.append(record)

    @property
    def statuses(self) -> list[tuple[str, str]]:
        """Return ``(owner id, status)`` per stored record, members by agent id and the team by team id."""
        return [(record.get("agent_id") or record["team_id"], record["status"]) for record in self.records]


async def no_store(record: AnyRunRecord) -> None:
    """Store nothing: the persist callback of a run whose stored rows the test does not read."""
    del record


def start_team(
    team: TeamSpec,
    run_input: Sequence[Message],
    *,
    ctx: RunContext,
    persist: Persist[AnyRunRecord] = no_store,
    request_template: ModelRequest = EMPTY_REQUEST,
) -> AsyncIterator[TeamRunEvent]:
    """Start ``team``'s run (``run_team``) under no hooks, storing nothing unless the test passes a store."""
    return run_team(
        team,
        run_input,
        ctx=ctx,
        hooks=NO_HOOKS,
        persist=persist,
        request_template=request_template,
    )


def resume_team(
    team: TeamSpec,
    paused: TeamRunRecord,
    messages: list[Message],
    *,
    context: Callable[[str], RunContext],
    approved: Mapping[TeamCallKey, ApprovedCall] = {},
    denied: Mapping[TeamCallKey, str | None] = {},
    external_results: Mapping[TeamCallKey, str] = {},
    persist: Persist[AnyRunRecord] = no_store,
    request_template: ModelRequest = EMPTY_REQUEST,
) -> AsyncIterator[TeamRunEvent]:
    """Resume ``paused`` (``continue_team``) with the test's decisions under no hooks, storing nothing unless asked.

    The members whose paused runs the resume continues are built first, as a saved approval's resume builds them.
    """
    paused_members = {requirement.member_agent_id for requirement in paused.requirements or ()}
    for member in team.members:
        if member.id in paused_members and member.built is None:
            member.built = member.build()
    return continue_team(
        team,
        paused,
        messages,
        decisions=TeamDecisions(approved, denied, external_results),
        context=context,
        hooks=NO_HOOKS,
        persist=persist,
        request_template=request_template,
    )


async def collect(stream: AsyncIterator[TeamRunEvent]) -> tuple[list[TeamRunEvent], TeamRunRecord]:
    """Drain a team run and return its events and the finished team record."""
    events = [event async for event in stream]
    finished = events[-1]
    assert isinstance(finished, RunFinished)
    assert isinstance(finished.record, TeamRunRecord)
    return events, finished.record
