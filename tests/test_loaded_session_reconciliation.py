"""Every caller of the shared run persistence leaves the session it loaded holding what the store holds.

An agent turn, an approval resume, a team turn with its members, and a delegation driver's parent run each hand
their records to the store; after each save the loaded session's runs are the runs a fresh load of the store
returns, also when the store refuses a save because a cut removed the run. A save a summary froze leaves the
session's copy of the run as it was, or its absence: the row did not change, though a fresh load, which starts
after the summary, no longer returns it.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom import ai
from mindroom.approval.resume import ResumeDecisions
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.delegation.state import DELEGATION_STATE_KEY
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.history import session_context
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.store import ConversationStore, persist_to
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.conftest import seed_session
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths, pause_parent, respond
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.team_turn_helpers import (
    REQUESTER_ID,
    SESSION_ID,
    delegate,
    squad_turn,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.agent_loop.run import Persist
    from mindroom.constants import RuntimePaths
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.types import SessionKind

pytestmark = pytest.mark.asyncio

REQUESTER = "@alice:example.org"
SESSION = "!room:example.org:$thread"


def _identity(agent_name: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=REQUESTER,
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=SESSION,
    )


def _config(paths: RuntimePaths) -> Config:
    """Return ``code``, whose ``multiply`` waits for approval, and ``leader``, which delegates to ``code``."""
    config = with_responder_access(
        Config(
            agents={
                "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
                "code": AgentConfig(display_name="Code", tools=["calculator"]),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
            tool_approval=ToolApprovalConfig(rules=[ApprovalRuleConfig(match="multiply", action="require_approval")]),
        ),
        "code",
        users=[REQUESTER],
    )
    entity_ids(config, paths)
    return config


def _store(agent_name: str, config: Config, paths: RuntimePaths) -> ConversationStore:
    return create_session_storage(agent_name, config, paths, execution_identity=_identity(agent_name))


def _completed(run_id: str, agent_name: str, content: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id=agent_name,
        session_id=SESSION,
        user_id=REQUESTER,
        status=RunStatus.completed,
        content=content,
    )


def _seed(store: ConversationStore, agent_name: str, *runs: AnyRunRecord) -> None:
    seed_session(
        store,
        ConversationSession(session_id=SESSION, kind="agent", owner_id=agent_name, user_id=REQUESTER, runs=list(runs)),
    )


def _as_stored(session: ConversationSession | None) -> list[dict[str, Any]]:
    assert session is not None
    return [run.to_dict() for run in session.runs]


def _reloaded(store: ConversationStore, session_id: str = SESSION, kind: SessionKind = "agent") -> list[dict[str, Any]]:
    return _as_stored(store.load_session(session_id, kind))


def _cut(store: ConversationStore, run_id: str, session_id: str = SESSION) -> None:
    """Remove ``run_id`` as a redaction's cut does: delete its rows and record it, so a later save of it is refused."""
    store.delete_run_subtrees([run_id], session_id=session_id)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            f'INSERT INTO "{store.names.removed_runs}" (run_id, session_id) VALUES (?, ?)',  # noqa: S608
            (run_id, session_id),
        )


@pytest.fixture
def opened_scopes(monkeypatch: pytest.MonkeyPatch) -> list[ScopeSessionContext]:
    """Record every scope context a turn opens, so a test can read the session the turn loaded and saved into."""
    opened: list[ScopeSessionContext] = []
    original = session_context.open_resolved_scope_session_context

    @contextmanager
    def recording(**kwargs: Any) -> Iterator[ScopeSessionContext | None]:  # noqa: ANN401
        with original(**kwargs) as scope_context:
            if scope_context is not None:
                opened.append(scope_context)
            yield scope_context

    monkeypatch.setattr(session_context, "open_resolved_scope_session_context", recording)
    monkeypatch.setattr(ai, "open_resolved_scope_session_context", recording)
    return opened


def _loaded_session(opened: list[ScopeSessionContext], scope_id: str) -> ConversationSession:
    """Return the session the turn of ``scope_id`` loaded; a delegated child's turn opens a scope of its own."""
    [scope_context] = [scope_context for scope_context in opened if scope_context.scope.scope_id == scope_id]
    assert scope_context.session is not None
    return scope_context.session


async def _respond(agent_name: str, config: Config, paths: RuntimePaths, *, run_id: str) -> object:
    return await respond(
        agent_name,
        "Go",
        config=config,
        runtime_paths=paths,
        identity=_identity(agent_name),
        run_id=run_id,
    )


# --- agent turns -------------------------------------------------------------------------------------------------


async def test_an_agent_turn_leaves_its_loaded_session_as_the_store_holds_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_scopes: list[ScopeSessionContext],
) -> None:
    """The turn's run joins the session it loaded as its row holds it, without the system prompt the store drops."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    ScriptedAgents({"code": ScriptedModel([answer("Second answer.")])}).install(monkeypatch)
    store = _store("code", config, paths)
    _seed(store, "code", _completed("run-0", "code", "First answer."))

    assert await _respond("code", config, paths, run_id="run-1") == "Second answer."

    session = _loaded_session(opened_scopes, "code")
    assert [run.run_id for run in session.runs] == ["run-0", "run-1"]
    assert all(message.role != "system" for message in session.runs[1].messages)
    assert _as_stored(session) == _reloaded(store)


async def test_an_agent_turn_whose_run_a_cut_removed_leaves_it_out_of_its_loaded_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_scopes: list[ScopeSessionContext],
) -> None:
    """A redaction that cuts the running run while it answers keeps the finished run out of the store and the session."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    store = _store("code", config, paths)
    _seed(store, "code", _completed("run-0", "code", "First answer."))

    async def redact() -> None:
        _cut(store, "run-1")

    ScriptedAgents({"code": ScriptedModel([[Await(redact), *answer("Late answer.")]])}).install(monkeypatch)

    await _respond("code", config, paths, run_id="run-1")

    session = _loaded_session(opened_scopes, "code")
    assert [run.run_id for run in session.runs] == ["run-0"]
    assert _as_stored(session) == _reloaded(store)


# --- approval resumes --------------------------------------------------------------------------------------------


def _approved(paused: RunRecord) -> ResumeDecisions:
    """Approve the paused ``multiply`` call, as the requester's card does."""
    return ResumeDecisions(
        approved=tuple(
            ApprovalCall(
                tool_call_id=str(tool.tool_call_id),
                tool_name=tool.tool_name,
                invoking_agent="code",
                toolkit_name="calculator",
                expires_at_ns=2**62,
                arguments_digest=approval_arguments_digest(tool.tool_args),
            )
            for tool in paused.tools or ()
            if tool.requires_confirmation
        ),
    )


async def test_an_approval_resume_leaves_its_loaded_session_as_the_store_holds_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resumed run moves after the turn that finished while it waited, in the session as in the store."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    model = ScriptedModel([calls(tool_call("multiply", "call-1", a=6, b=7)), answer("42.")])
    ScriptedAgents({"code": model}).install(monkeypatch)
    identity = _identity("code")
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity, agent_name="code")):
        paused = await pause_parent("code", "Multiply", config=config, runtime_paths=paths, identity=identity)
        # Another reply finished while the approval waited; the resume loads the session as the store holds it.
        later = paused.store.load_session(SESSION, "agent")
        assert later is not None
        paused.store.save_run(later, _completed("run-later", "code", "Meanwhile."), record_usage=True)
        session = paused.store.load_session(SESSION, "agent")
        assert session is not None

        record = await replace(paused.runner, session=session).continue_run(
            paused.record,
            _approved(paused.record),
            None,
        )

    assert record.status is RunStatus.completed
    assert [run.run_id for run in session.runs] == ["run-later", paused.record.run_id]
    assert session.runs[-1] is not record
    assert _as_stored(session) == _reloaded(paused.store)


async def test_an_approval_resume_whose_run_a_cut_removed_leaves_it_out_of_its_loaded_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redaction that cuts the paused run while its resume answers takes the run out of the session it loaded."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    identity = _identity("code")
    store = _store("code", config, paths)

    async def redact() -> None:
        _cut(store, "parent-run")

    ScriptedAgents(
        {"code": ScriptedModel([calls(tool_call("multiply", "call-1", a=6, b=7)), [Await(redact), *answer("42.")]])},
    ).install(monkeypatch)
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity, agent_name="code")):
        paused = await pause_parent("code", "Multiply", config=config, runtime_paths=paths, identity=identity)
        session = paused.store.load_session(SESSION, "agent")
        assert session is not None
        assert [run.run_id for run in session.runs] == ["parent-run"]

        await replace(paused.runner, session=session).continue_run(paused.record, _approved(paused.record), None)

    assert session.runs == []
    assert _as_stored(session) == _reloaded(paused.store)


# --- team turns --------------------------------------------------------------------------------------------------


def _seed_team(store: ConversationStore) -> None:
    """Seed the squad's session with one earlier team run."""
    earlier = TeamRunRecord(
        run_id="team-0",
        team_id="squad",
        session_id=SESSION_ID,
        user_id=REQUESTER_ID,
        status=RunStatus.completed,
        content="Earlier.",
    )
    seed_session(store, ConversationSession(session_id=SESSION_ID, kind="team", owner_id="squad", runs=[earlier]))


@pytest.mark.usefixtures("squad_probe")
async def test_a_team_turn_keeps_its_member_runs_in_its_loaded_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_scopes: list[ScopeSessionContext],
) -> None:
    """The member run saved before its team run stays in the session beside it, as a load of the team scope shows."""
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Draft it"), answer("Done.")],
        writer=[answer("Drafted.")],
    )
    store = team.storage
    _seed_team(store)

    await team.respond()

    session = _loaded_session(opened_scopes, "squad")
    member_runs = [run for run in session.runs if isinstance(run, RunRecord)]
    assert [run.agent_id for run in member_runs] == ["writer"]
    assert [run.run_id for run in session.runs if isinstance(run, TeamRunRecord)] == ["team-0", "run-1"]
    assert member_runs[0].parent_run_id == "run-1"
    assert _as_stored(session) == _reloaded(store, SESSION_ID, "team")


@pytest.mark.usefixtures("squad_probe")
async def test_a_team_turn_whose_run_a_cut_removed_leaves_it_and_its_members_out_of_its_loaded_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_scopes: list[ScopeSessionContext],
) -> None:
    """A redaction that cuts the running team run after a member answered takes both out of the session it loaded."""
    held_before_cut: list[str | None] = []

    async def redact() -> None:
        held_before_cut.extend(run.parent_run_id for run in _loaded_session(opened_scopes, "squad").runs)
        _cut(team.storage, "run-1", SESSION_ID)

    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "writer", "Draft it"), [Await(redact), *answer("Late answer.")]],
        writer=[answer("Drafted.")],
    )
    _seed_team(team.storage)

    await team.respond()

    # The writer's run had joined the session under the team run before the cut.
    assert held_before_cut == [None, "run-1"]
    session = _loaded_session(opened_scopes, "squad")
    assert [run.run_id for run in session.runs] == ["team-0"]
    assert _as_stored(session) == _reloaded(team.storage, SESSION_ID, "team")


# --- delegation ---------------------------------------------------------------------------------------------------


def _delegating_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Script ``leader`` to delegate one task to ``code`` and answer with its result, and ``code`` to do the task."""
    ScriptedAgents(
        {
            "leader": ScriptedModel(
                [
                    calls(tool_call("run_subagent", "delegate", agent_name="code", task="Report")),
                    answer("Parent done."),
                ],
            ),
            "code": ScriptedModel([answer("Report written.")]),
        },
    ).install(monkeypatch)


async def test_a_delegating_turn_leaves_its_parent_run_in_its_loaded_session_as_stored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_scopes: list[ScopeSessionContext],
) -> None:
    """The driver's pause, child-state, continuation, and final-state saves leave the parent run as the store holds it.

    The delegated child runs in a session of its own that it opens empty, so its turn's first session holds its run
    as the child's store does.
    """
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    _delegating_leader(monkeypatch)
    persisted: list[ConversationSession] = []

    def recording_persist_to(store: ConversationStore, session: ConversationSession) -> Persist[AnyRunRecord]:
        persisted.append(session)
        return persist_to(store, session)

    monkeypatch.setattr(ai, "persist_to", recording_persist_to)
    store = _store("leader", config, paths)
    _seed(store, "leader", _completed("run-0", "leader", "Earlier."))

    assert "Parent done." in str(await _respond("leader", config, paths, run_id="parent-run"))

    session = _loaded_session(opened_scopes, "leader")
    assert [run.run_id for run in session.runs] == ["run-0", "parent-run"]
    assert DELEGATION_STATE_KEY in (session.runs[1].metadata or {})
    assert _as_stored(session) == _reloaded(store)
    [child_scope] = [scope_context for scope_context in opened_scopes if scope_context.scope.scope_id == "code"]
    assert child_scope.session is None
    [child] = [persisted_into for persisted_into in persisted if persisted_into.owner_id == "code"]
    assert child.session_id == child_scope.session_id
    assert [run.content for run in child.runs] == ["Report written."]
    assert _as_stored(child) == _reloaded(_store("code", config, paths), child.session_id)


async def test_a_delegating_turn_keeps_the_stored_parent_when_a_summary_froze_it_before_the_final_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_scopes: list[ScopeSessionContext],
) -> None:
    """A compaction folding the finished parent before the driver's last state save leaves the session's copy as stored."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    _delegating_leader(monkeypatch)
    store = _store("leader", config, paths)
    _seed(store, "leader", _completed("run-0", "leader", "Earlier."))
    original_save_run = ConversationStore.save_run
    refused: list[str] = []

    def fold_then_save(
        self: ConversationStore,
        session: ConversationSession,
        run: AnyRunRecord,
        *,
        record_usage: bool,
    ) -> object:
        if run.status is RunStatus.completed and DELEGATION_STATE_KEY in (run.metadata or {}):
            # Stands in for a compaction that summarized the finished parent just before the driver's state save.
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute(
                    f'INSERT INTO "{self.names.summaries}" (session_id, scope_key, after_run_index, summary, '  # noqa: S608
                    f"created_at) SELECT session_id, 'agent:leader', run_index, 'Summary.', 1 FROM \"{self.names.runs}\" "
                    "WHERE run_id = ?",
                    (run.run_id,),
                )
        outcome = original_save_run(self, session, run, record_usage=record_usage)
        if outcome == "frozen":
            refused.append(run.run_id)
        return outcome

    monkeypatch.setattr(ConversationStore, "save_run", fold_then_save)

    assert "Parent done." in str(await _respond("leader", config, paths, run_id="parent-run"))

    assert refused == ["parent-run"]
    session = _loaded_session(opened_scopes, "leader")
    stored = store.get_run("parent-run")
    assert stored is not None
    assert [run.run_id for run in session.runs] == ["run-0", "parent-run"]
    assert session.runs[1].to_dict() == stored.to_dict()
