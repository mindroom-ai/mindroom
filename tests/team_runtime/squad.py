"""The C4 ``squad`` and ``panel`` teams rebuilt on the owned runtime, and the comparison with the rows Agno stored.

The capture ran ``Team-writer-checker`` with a scripted ``Synthetic`` provider: the writer has MindRoom's calculator,
the checker the same calculator with ``multiply`` under the approval policy, and the leader only the delegation tool.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, NO_HOOKS
from mindroom.agent_loop.spec import AgentSpec, RunContext, UnstoredSession
from mindroom.config.main import Config
from mindroom.custom_tools.calculator import CalculatorTools
from mindroom.event_journal.approval_continuations import ApprovalCall, approval_arguments_digest
from mindroom.llm.events import RunFinished
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity
from mindroom.llm.runs import TeamRunRecord
from mindroom.teams.members import BuiltTeam
from mindroom.teams.runtime import TeamDecisions, continue_team, run_team
from mindroom.teams.spec import TeamMember, TeamMode, TeamSpec
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from tests.fakes.scripted_model import ScriptedModel
from tests.llm.agno_dict_helpers import expected_run_dict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

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
PROVIDER = "Synthetic"

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def golden_run(store: str, session_id: str, run_id: str) -> dict[str, Any]:
    """Return one run as the C4 capture loaded it from the store Agno wrote."""
    sessions = json.loads((STORAGE / "loaded_sessions.json").read_text(encoding="utf-8"))[store]
    [run] = [run for run in sessions[session_id]["runs"] if run["run_id"] == run_id]
    return run


def pre_resume_runs(dump: str) -> dict[str, dict[str, Any]]:
    """Return the run rows of one C6 pre-resume dump by run id, as the resume read them."""
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
    """Return one squad member; ``system`` defaults to a short prompt (completed rows store none)."""
    spec = AgentSpec(
        id=agent_id,
        name=name,
        model=model,
        system_message=Message(role="system", content=system or f"You are {name}."),
        toolkits=toolkits,
    )
    return TeamMember(spec=spec, role=f"{name} role.", new_hooks=lambda: NO_HOOKS, request_template=EMPTY_REQUEST)


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
        system_message=Message(role="system", content=system),
        toolkits=tuple(toolkits),
    )
    return TeamSpec(leader=leader, mode=mode, members=tuple(members))


def built_team(spec: TeamSpec) -> BuiltTeam:
    """Return ``spec`` as a built team under the default history settings, with no leader middleware or jobs."""
    return BuiltTeam(
        spec=spec,
        history_settings=Config().resolve_entity(None).history_settings,
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
    system: str = "You lead the squad.",
    checker_system: str | None = None,
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
        system,
        (
            member("writer", "Writer", writer, *writer_tools),
            member("checker", "Checker", checker, *checker_tools, system=checker_system),
        ),
        mode=mode,
        toolkits=leader_toolkits,
    )


def run_context(run_id: str, session_id: str, metadata: dict[str, Any] | None = None) -> RunContext:
    """Return the team run's context for Alice's turn."""
    return RunContext(run_id=run_id, session=UnstoredSession(session_id), user_id=REQUESTER, metadata=metadata)


def stored_as_owned(row: dict[str, Any]) -> dict[str, Any]:
    """Return a run row Agno stored as the owned runtime stores it: a member run holds none of the team's metadata.

    Agno stored the team run's metadata on every member run; the owned runtime stores it on the team's row alone. A
    team row's paused member runs ride along in ``member_responses`` and drop it too.
    """
    result = dict(row)
    if row.get("parent_run_id") is not None:
        result.pop("metadata", None)
    if "member_responses" in row:
        result["member_responses"] = [stored_as_owned(member) for member in row["member_responses"]]
    return result


def golden_metadata(team_row: dict[str, Any]) -> dict[str, Any]:
    """Return the run metadata the turn started with: the stored metadata minus what MindRoom added afterwards."""
    return {key: value for key, value in team_row["metadata"].items() if key != "io.mindroom.ai_run"}


def prompt(team_row: dict[str, Any]) -> list[Message]:
    """Return the leader's run input: the stored turn's user message."""
    [user] = [message for message in team_row["messages"] if message["role"] == "user"]
    return [Message(role="user", content=user["content"])]


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
    return run_team(team, run_input, ctx=ctx, hooks=NO_HOOKS, persist=persist, request_template=request_template)


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
    """Resume ``paused`` (``continue_team``) with the test's decisions under no hooks, storing nothing unless asked."""
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


def comparable(run: dict[str, Any]) -> dict[str, Any]:
    """Return a stored run dict as one comparison sees it: Agno-only keys out, ids renumbered, clocks as sentinels.

    Prompt-role messages leave completed rows, as the store strips them; random ids become ``<id-n>`` in
    first-appearance order; every timestamp and duration becomes a sentinel, so its presence is compared but not its
    value; the run metadata MindRoom adds after the turn (``io.mindroom.ai_run``) goes, the rest of the metadata is
    compared.
    """
    text = json.dumps(_stored_shape(expected_run_dict(run)), sort_keys=True)
    ids: dict[str, str] = {}
    text = _UUID.sub(lambda match: ids.setdefault(match.group(), f"<id-{len(ids) + 1}>"), text)
    return _with_clock_sentinels(json.loads(text))


def _stored_shape(run: dict[str, Any]) -> dict[str, Any]:
    result = dict(run)
    if run["status"] != "PAUSED":
        result["messages"] = [message for message in run["messages"] if message["role"] != "system"]
    if "member_responses" in run:
        result["member_responses"] = [_stored_shape(item) for item in run["member_responses"]]
    return result


_CLOCK = "<clock>"
_METADATA_ADDED_AFTER_THE_TURN = frozenset({"io.mindroom.ai_run"})


def _clocked(values: dict[str, Any], keys: frozenset[str]) -> dict[str, Any]:
    """Return ``values`` with every clock reading among ``keys`` replaced by the sentinel, presence kept."""
    return {key: (_CLOCK if key in keys and value is not None else value) for key, value in values.items()}


def _with_clock_sentinels(run: dict[str, Any]) -> dict[str, Any]:
    result = _clocked(run, frozenset({"created_at"}))
    if result.get("metadata") is not None:
        result["metadata"] = {
            key: value for key, value in result["metadata"].items() if key not in _METADATA_ADDED_AFTER_THE_TURN
        }
    result["messages"] = [_clocked(message, frozenset({"id", "created_at"})) for message in run["messages"]]
    if "metrics" in result and result["metrics"] is not None:
        # The duration is a clock reading. The first-token time is left out altogether: Agno's own timer stamped it
        # even on a tool-call-only request whose message carried none, where the owned loop records only what the
        # provider reported, so even its presence differs.
        metrics = _clocked(result["metrics"], frozenset({"duration"}))
        result["metrics"] = {key: value for key, value in metrics.items() if key != "time_to_first_token"}
    if "member_responses" in result:
        result["member_responses"] = [_with_clock_sentinels(item) for item in result["member_responses"]]
    return result
