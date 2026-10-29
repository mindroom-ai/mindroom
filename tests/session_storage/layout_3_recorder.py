"""Record the conversation databases layout version 3 writes, and what every reader shows of them.

Run from the repository root at a commit whose store writes layout version 3, such as v2026.10.667:
``.venv/bin/python -m tests.session_storage.layout_3_recorder``.
It rewrites ``tests/fixtures/conversation_layout_3/``: per database, an SQL dump and the readers' view of it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.constants import MATRIX_EVENT_ID_METADATA_KEY, MATRIX_SEEN_EVENT_IDS_METADATA_KEY
from mindroom.history.interrupted_replay import InterruptedReplaySnapshot, persist_interrupted_replay_snapshot
from mindroom.history.session_context import _TEAM_STORAGE_NAME_PATTERN
from mindroom.history.storage import first_scope_session, new_scope_session, remove_runs_answering
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message, ModelProvenance
from mindroom.llm.provider import ModelIdentity
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.llm.usage import HelperUsage, MessageMetrics, RequestUsage, RunMetrics, TokenCounts
from mindroom.session_storage.agent_storage import (
    create_agent_state_storage,
    create_state_storage,
    save_compaction_usage,
)
from mindroom.usage.storage import SYSTEM_USAGE_ENTITY, SYSTEM_USAGE_STORAGE_NAME
from tests.history_helpers import compact_runs
from tests.run_record_helpers import PROVIDER, execution, reply, result, run_metrics
from tests.session_storage.layout_3_readers import observe_database
from tests.storage_oracle.determinism import deterministic

if TYPE_CHECKING:
    from mindroom.session_storage.store import ConversationStore
    from tests.storage_oracle.determinism import Determinism

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "conversation_layout_3"
_MODEL = "synthetic-model"
_AGENT = HistoryScope(kind="agent", scope_id="code")
_TEAM = HistoryScope(kind="team", scope_id="crew")
_SYSTEM = HistoryScope(kind="agent", scope_id=SYSTEM_USAGE_ENTITY)


def team_storage_name(scope: HistoryScope) -> str:
    """Return the storage name a team scope's database has, as ``create_scope_session_storage`` names it."""
    normalized = _TEAM_STORAGE_NAME_PATTERN.sub("_", scope.scope_id).strip("_") or "team"
    return f"team_{normalized}_{hashlib.sha256(scope.key.encode()).hexdigest()[:12]}"


def _usage(weight: int) -> MessageMetrics:
    return MessageMetrics(tokens=TokenCounts(input_tokens=10 * weight, output_tokens=weight, total_tokens=11 * weight))


def _call(call_id: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": "multiply", "arguments": '{"a": 6, "b": 7}'}}


def _agent_run(run_id: str, session_id: str, *, agent_id: str = "code", weight: int = 1) -> RunRecord:
    call = _call(f"call-{run_id}")
    messages = [
        Message(role="user", content=f"question {run_id}"),
        reply(None, model=_MODEL, metrics=_usage(weight), calls=[call]),
        result(call, "42"),
        reply(f"answer {run_id}", model=_MODEL, metrics=_usage(weight)),
    ]
    return RunRecord(
        run_id=run_id,
        agent_id=agent_id,
        session_id=session_id,
        user_id="@alice:example.org",
        content=f"answer {run_id}",
        messages=messages,
        tools=[execution(call, result="42", is_error=False)],
        metrics=run_metrics((_MODEL, _usage(weight)), (_MODEL, _usage(weight))),
        metadata={MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}", "requester_id": "@alice:example.org"},
        status=RunStatus.completed,
        model=_MODEL,
        model_provider=PROVIDER,
    )


def _team_run(run_id: str, session_id: str, members: list[RunRecord]) -> TeamRunRecord:
    return TeamRunRecord(
        run_id=run_id,
        team_id="crew",
        session_id=session_id,
        user_id="@alice:example.org",
        content=f"team answer {run_id}",
        messages=[
            Message(role="user", content=f"team question {run_id}"),
            reply(f"team answer {run_id}", model=_MODEL, metrics=_usage(2)),
        ],
        metrics=run_metrics((_MODEL, _usage(2))),
        metadata={MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}", "requester_id": "@alice:example.org"},
        status=RunStatus.completed,
        member_responses=members,
        model=_MODEL,
        model_provider=PROVIDER,
    )


def _helper_usage(run_id: str, created_at: float) -> HelperUsage:
    return HelperUsage(
        run_id=run_id,
        model=_MODEL,
        provider=PROVIDER,
        metrics=RunMetrics(tokens=_usage(3).tokens),
        requests=(
            RequestUsage(
                created_at=created_at,
                metrics=_usage(3),
                model=ModelProvenance(model_id=_MODEL, provider=PROVIDER),
            ),
        ),
        created_at=created_at,
    )


def _record_agent(root: Path, determinism: Determinism) -> ConversationStore:
    """Agent ``code``'s database: a compacted conversation, one an edit cut, and one only helper usage created."""
    store = create_agent_state_storage("code", root)
    determinism.at(1)
    session = first_scope_session(_AGENT, "compacted")
    session.session_state["todo"] = ["write the report"]
    store.save_run(session, _agent_run("r1", "compacted"), record_usage=True, state={"todo": ["write the report"]})
    determinism.at(2)
    store.update_metadata(session, lambda metadata: {**(metadata or {}), "note": "kept"})
    store.save_run(session, _agent_run("r2", "compacted", weight=2), record_usage=True, state={"todo": []})
    determinism.at(3)
    compact_runs(store, session, _AGENT, ["r1"], "summary of r1")
    save_compaction_usage(
        store,
        session_id="compacted",
        requester_id="@alice:example.org",
        model=_model_identity(),
        usage=_usage(4),
        initial_session=None,
    )
    determinism.at(4)
    store.save_run(session, _agent_run("r3", "compacted", weight=3), record_usage=True, state={"todo": []})

    determinism.at(5)
    edited = first_scope_session(_AGENT, "edited")
    store.save_run(edited, _agent_run("e1", "edited"), record_usage=True, state={})
    store.save_run(edited, _agent_run("e2", "edited"), record_usage=True, state={})
    determinism.at(6)
    assert remove_runs_answering(store, "edited", _AGENT, "$e2")

    determinism.at(7)
    store.save_independent_usage(
        session_id="helper-only",
        usage_id="memory_auto_flush:h1",
        kind="memory_auto_flush",
        requester_id="@bob:example.org",
        usage=_helper_usage("h1", determinism.time()),
        initial_session=new_scope_session(session_id="helper-only", scope_id="code", is_team=False),
    )
    return store


def _model_identity() -> ModelIdentity:
    return ModelIdentity(id=_MODEL, provider="synthetic", provider_name=PROVIDER)


def _record_team(root: Path, determinism: Determinism) -> ConversationStore:
    """Team ``crew``'s database: a team turn with its members, and one a team bot's interrupted agent path started."""
    store = create_state_storage(team_storage_name(_TEAM), root)
    determinism.at(1)
    session = first_scope_session(_TEAM, "team-turns")
    session.session_state["plan"] = "draft"
    members = [_agent_run("m1", "team-turns", agent_id="writer"), _agent_run("m2", "team-turns", agent_id="editor")]
    for member in members:
        member.parent_run_id = "t1"
    store.save_run(session, _team_run("t1", "team-turns", members), record_usage=True, state={"plan": "draft"})

    # A team bot's first reply on its agent path was interrupted: the row it created is an agent row of the team's id,
    # so a team turn's load of it returns nothing, and the next team turn saves into the row it cannot load.
    determinism.at(2)
    started = new_scope_session(session_id="agent-path", scope_id="crew", is_team=False)
    persist_interrupted_replay_snapshot(
        storage=store,
        session=started,
        run_id="interrupted",
        snapshot=InterruptedReplaySnapshot(
            user_message="question interrupted",
            partial_text="half an answer",
            completed_tools=(),
            interrupted_tools=(),
            run_metadata={MATRIX_EVENT_ID_METADATA_KEY: "$interrupted", MATRIX_SEEN_EVENT_IDS_METADATA_KEY: []},
        ),
    )
    determinism.at(3)
    assert store.load_session("agent-path", "team") is None
    later = first_scope_session(_TEAM, "agent-path")
    store.save_run(later, _team_run("t2", "agent-path", []), record_usage=True, state={})
    return store


def _record_system(root: Path, determinism: Determinism) -> ConversationStore:
    """The system usage database: internal provider calls of two kinds."""
    store = create_state_storage(SYSTEM_USAGE_STORAGE_NAME, root)
    for step, kind in enumerate(("thread_summary", "routing"), start=1):
        determinism.at(step)
        store.save_independent_usage(
            session_id=kind,
            usage_id=f"{kind}:s{step}",
            kind=kind,
            requester_id=None,
            usage=_helper_usage(f"s{step}", determinism.time()),
            initial_session=new_scope_session(session_id=kind, scope_id=SYSTEM_USAGE_ENTITY, is_team=False),
        )
    return store


def _dump(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return "\n".join(connection.iterdump()) + "\n"


def record() -> None:
    """Write every recording: its database's dump and the readers' view of it."""
    FIXTURES.mkdir(exist_ok=True)
    for name, scope, write in (
        ("agent_code", _AGENT, _record_agent),
        ("team_crew", _TEAM, _record_team),
        ("system", _SYSTEM, _record_system),
    ):
        with tempfile.TemporaryDirectory() as directory, deterministic() as determinism:
            store = write(Path(directory), determinism)
            (FIXTURES / f"{name}.sql").write_text(_dump(store.path), encoding="utf-8")
            shown = observe_database(store, scope)
        (FIXTURES / f"{name}.json").write_text(json.dumps(shown, sort_keys=True, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    record()
