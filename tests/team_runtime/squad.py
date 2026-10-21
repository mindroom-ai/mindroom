"""The C4 ``squad`` and ``panel`` teams rebuilt on the owned runtime, and the comparison with the rows Agno stored.

The capture ran ``Team-writer-checker`` with a scripted ``Synthetic`` provider: the writer has MindRoom's calculator,
the checker the same calculator with ``multiply`` under the approval policy, and the leader only the delegation tool.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.agent_loop.spec import AgentSpec, RunContext
from mindroom.custom_tools.calculator import CalculatorTools
from mindroom.event_journal.approval_continuations import ApprovalCall, approval_arguments_digest
from mindroom.llm.events import RunFinished
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity
from mindroom.llm.runs import TeamRunRecord
from mindroom.team_runtime.runtime import MemberRunEnded
from mindroom.team_runtime.spec import TeamMember, TeamMode, TeamSpec
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from tests.fakes.scripted_model import ScriptedModel
from tests.llm.agno_dict_helpers import expected_run_dict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from mindroom.llm.runs import AnyRunRecord, RunRecord
    from mindroom.team_runtime.runtime import TeamRunEvent
    from mindroom.tool_system.agno_toolkits import AdaptedToolkit

STORAGE = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "storage"
SQUAD = "!room:example.org:$squad"
SQUAD_APPROVED = "!room:example.org:$squad-approved"
PANEL = "!room:example.org:$panel"
REQUESTER = "@alice:example.org"
TEAM_NAME = "Team-writer-checker"
PROVIDER = "Synthetic"

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_PROMPT_ROLES = frozenset({"system", "developer"})


def golden_run(store: str, session_id: str, run_id: str) -> dict[str, Any]:
    """Return one run as the C4 capture loaded it from the store Agno wrote."""
    sessions = json.loads((STORAGE / "loaded_sessions.json").read_text(encoding="utf-8"))[store]
    [run] = [run for run in sessions[session_id]["runs"] if run["run_id"] == run_id]
    return run


def pre_resume_runs(dump: str) -> dict[str, dict[str, Any]]:
    """Return the run rows of one C6 pre-resume dump by run id, as the resume read them."""
    connection = sqlite3.connect(":memory:")
    connection.executescript((STORAGE / f"{dump}.sql").read_text(encoding="utf-8"))
    [table] = [
        name
        for (name,) in connection.execute(
            "select name from sqlite_master where type = 'table' and name like '%_sessions_runs'",
        )
    ]
    return {run_id: json.loads(data) for run_id, data in connection.execute(f"select run_id, run_data from {table}")}  # noqa: S608


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
    model: ScriptedModel,
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
    return TeamMember(spec=spec, role=f"{name} role.")


def squad(
    leader: ScriptedModel,
    writer: ScriptedModel,
    checker: ScriptedModel,
    *,
    mode: TeamMode = TeamMode.COORDINATE,
    team_id: str = "squad",
    system: str = "You lead the squad.",
    checker_system: str | None = None,
    leader_toolkits: Sequence[AdaptedToolkit] = (),
    checker_toolkits: Sequence[AdaptedToolkit] | None = None,
) -> TeamSpec:
    """Return the capture's team: the writer with the calculator, the checker with the gated calculator."""
    checker_tools = (calculator(gate_multiply=True),) if checker_toolkits is None else tuple(checker_toolkits)
    return TeamSpec(
        id=team_id,
        name=TEAM_NAME,
        mode=mode,
        model=leader,
        system_message=Message(role="system", content=system),
        toolkits=tuple(leader_toolkits),
        members=(
            member("writer", "Writer", writer, calculator(gate_multiply=False)),
            member("checker", "Checker", checker, *checker_tools, system=checker_system),
        ),
    )


def run_context(run_id: str, session_id: str, metadata: dict[str, Any] | None = None) -> RunContext:
    """Return the team run's context for Alice's turn."""
    return RunContext(run_id=run_id, session_id=session_id, user_id=REQUESTER, metadata=metadata)


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
    """A persist callback that remembers every record it was handed, as a snapshot, in order."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def __call__(self, record: AnyRunRecord) -> None:
        """Store ``record`` after yielding once, as a store that writes off the loop would."""
        await asyncio.sleep(0)
        self.records.append(record.to_dict())

    @property
    def statuses(self) -> list[tuple[str, str]]:
        """Return ``(owner id, status)`` per stored record, members by agent id and the team by team id."""
        return [(record.get("agent_id") or record["team_id"], record["status"]) for record in self.records]


async def collect(stream: AsyncIterator[TeamRunEvent]) -> tuple[list[TeamRunEvent], TeamRunRecord]:
    """Drain a team run and return its events and the finished team record."""
    events = [event async for event in stream]
    finished = events[-1]
    assert isinstance(finished, RunFinished)
    assert isinstance(finished.record, TeamRunRecord)
    return events, finished.record


def member_records(events: Sequence[TeamRunEvent]) -> list[RunRecord]:
    """Return every member run's terminal record, in the order the members ended."""
    return [event.record for event in events if isinstance(event, MemberRunEnded)]


def comparable(run: dict[str, Any]) -> dict[str, Any]:
    """Return a stored run dict as one comparison sees it: Agno-only keys and volatile values out, ids renumbered.

    Prompt-role messages leave completed rows, as the store strips them; random ids become ``<id-n>`` in
    first-appearance order; timestamps, clock readings, and metadata go; a tool's timing keeps only which keys it
    recorded.
    """
    text = json.dumps(_stored_shape(expected_run_dict(run)), sort_keys=True)
    ids: dict[str, str] = {}
    text = _UUID.sub(lambda match: ids.setdefault(match.group(), f"<id-{len(ids) + 1}>"), text)
    return _without_clocks(json.loads(text))


def _stored_shape(run: dict[str, Any]) -> dict[str, Any]:
    result = dict(run)
    if run["status"] != "PAUSED":
        result["messages"] = [message for message in run["messages"] if message["role"] not in _PROMPT_ROLES]
    if "member_responses" in run:
        result["member_responses"] = [_stored_shape(item) for item in run["member_responses"]]
    return result


def _without_clocks(run: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in run.items() if key not in {"created_at", "metadata"}}
    result["messages"] = [
        {key: value for key, value in message.items() if key not in {"id", "created_at"}} for message in run["messages"]
    ]
    if "metrics" in result:
        # Durations and the first-token time are clock readings: Agno's timer stamped the run's first-token time
        # even for a tool-call-only request whose message carries none.
        result["metrics"] = {
            key: value for key, value in result["metrics"].items() if key not in {"duration", "time_to_first_token"}
        }
    if "tools" in result:
        result["tools"] = [_tool_without_clocks(tool) for tool in result["tools"]]
    if "requirements" in result:
        result["requirements"] = [
            {
                **{key: value for key, value in item.items() if key != "created_at"},
                "tool_execution": _tool_without_clocks(item["tool_execution"]),
            }
            for item in result["requirements"]
        ]
    if "member_responses" in result:
        result["member_responses"] = [_without_clocks(item) for item in result["member_responses"]]
    return result


def _tool_without_clocks(tool: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in tool.items() if key != "created_at"}
    result["metrics"] = None if tool["metrics"] is None else sorted(tool["metrics"])
    return result
