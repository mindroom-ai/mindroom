"""The driver that interprets the action vocabulary over a store's primitives.

``Driver`` owns which run an action picks, which records and messages it builds, and the order of the storage calls a
turn makes. ``StoreBackend`` (``store_backend.py``) implements the primitives, one per writer or reader of spec v4
section 3.7, through the conversation store's own API; ``MaskedBackend`` (``masks.py``) reports through it what the
Agno-layout store would have reported. Every decision reads the driver's own state or a load, never a store's tables.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.agent_loop.run import resumed_record
from mindroom.constants import (
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
)
from mindroom.delegation.state import DelegationState
from mindroom.history.interrupted_replay import build_interrupted_replay_snapshot
from mindroom.history.mid_turn_compaction import _carry_request_usage
from mindroom.history.replay import current_summary_text
from mindroom.history.request_messages import scope_history_messages
from mindroom.history.run_visibility import QUEUED_MESSAGE_NOTICE_MARKER_KEY, replays_in_scope
from mindroom.history.storage import first_scope_session, read_scope_state
from mindroom.history.summary_message import compaction_summary_message, mark_summary_follows
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, TeamRunRecord, ToolExecution
from mindroom.llm.usage import MessageMetrics, RunMetrics, TokenCounts
from mindroom.session_storage.schema import TableNames
from mindroom.session_storage.types import session_state_snapshot
from mindroom.tool_system.events import ToolTraceEntry
from tests.storage_oracle.actions import Action

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from mindroom.history.interrupted_replay import InterruptedReplaySnapshot
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.types import ConversationSession
    from tests.storage_oracle.actions import ScenarioKind
    from tests.storage_oracle.determinism import Determinism

SESSION_ID = "!room:example.org:$thread"
ROOM_ID = "!room:example.org"
REQUESTER_ID = "@user:example.org"
_OWNERS: dict[ScenarioKind, str] = {"agent": "code", "team": "squad"}
_MEMBERS = ("alpha", "beta")
# The model every compaction summary reports, and the counters it returns.
SUMMARY_MODEL = ModelIdentity(id="oracle-summary-model", provider="oracle", provider_name="oracle")
SUMMARY_USAGE = MessageMetrics(tokens=TokenCounts(input_tokens=7, output_tokens=3, total_tokens=10))
# The response turn id a run's live queued-message notice names.
_NOTICE_TURN_KEY = "mindroom_queued_message_notice_response_turn_id"
# Events the legacy openings' summaries and archived runs hold, as ``Driver.seed_legacy`` documents them.
LEGACY_EVENTS: dict[int, tuple[str, ...]] = {
    0: ("$legacy-1", "$legacy-2"),
    1: ("$legacy-0", "$a0", "$read-by-a0", "$a1", "$read-by-a1"),
    2: ("$legacy-0", "$a0", "$read-by-a0"),
}


@dataclass(frozen=True)
class FoldBasisView:
    """What a mid-turn fold read before its summary call: the backend's own basis, its summary, and its runs."""

    handle: object
    summary: str | None
    run_ids: tuple[str, ...]


@dataclass(frozen=True)
class FoldInput:
    """What a mid-turn fold takes out of its running run.

    ``record`` is the live record at the fold, ``messages`` the exchanges it folds, and ``members`` the member runs
    that ended since the run's previous fold.
    """

    record: AnyRunRecord
    messages: tuple[Message, ...]
    members: tuple[RunRecord, ...]


@dataclass
class _Turn:
    """A reply that is running: its session, its live record, and the request it sends next."""

    session: ConversationSession
    record: AnyRunRecord
    system: Message
    prompt: Message
    history: list[Message] = field(default_factory=list)
    body: list[Message] = field(default_factory=list)
    open_exchanges: list[list[Message]] = field(default_factory=list)
    summary: Message | None = None
    batches: int = 0
    tokens: int = 0
    folded_members: set[str] = field(default_factory=set)
    stopped: bool = False
    cli: bool = False
    paused_member: RunRecord | None = None

    @property
    def messages(self) -> list[Message]:
        """The live request: the prompt, the replayed history or the fold's summary, and the turn's messages."""
        history = [] if self.summary is not None else self.history
        summary = [] if self.summary is None else [self.summary]
        return [self.system, *history, self.prompt, *summary, *self.body]


def summary_tokens(text: str | None) -> set[str]:
    """Return what a summary covers: the runs it names, ``<run>~`` for a fold's part of a running run, or legacy."""
    if not text:
        return set()
    if text.startswith("S:"):
        return {token for token in text[2:].split(",") if token}
    return {"legacy"}


def _summary_text(tokens: set[str]) -> str:
    return "S:" + ",".join(sorted(tokens))


def _usage(weight: int) -> MessageMetrics:
    return MessageMetrics(tokens=TokenCounts(input_tokens=10 * weight, output_tokens=weight, total_tokens=11 * weight))


def _assert_unique_message_ids(record: AnyRunRecord) -> None:
    for run in (record, *(record.member_responses if isinstance(record, TeamRunRecord) else ())):
        ids = [message.id for message in run.messages]
        assert len(ids) == len(set(ids)), f"run {run.run_id} holds a message id twice"


class Driver(ABC):
    """Interpret the action vocabulary over one conversation, calling the backend's storage primitives."""

    # Whether a fold may follow a member's pause in the same attempt. Real team runs never do: their loop sends no
    # request after a member pauses. The recordings were made with it, so a backend replaying them keeps it.
    folds_after_member_pause = True

    def __init__(self, root: Path, kind: ScenarioKind, determinism: Determinism) -> None:
        self.root = root
        self.kind = kind
        self.determinism = determinism
        self.owner = _OWNERS[kind]
        self.scope = HistoryScope(kind=kind, scope_id=self.owner)
        self.names = TableNames(self.owner)
        self.database = self.names.database_path(root)
        self.step = 0
        self.created: list[str] = []
        self.members: dict[str, list[str]] = {}
        self.records: dict[str, AnyRunRecord] = {}
        self.versions: list[AnyRunRecord] = []
        self.state_versions: list[dict[str, object]] = []
        self.paused: list[str] = []
        self.paused_members: dict[str, str] = {}
        self.resumed: list[str] = []
        self.discarded: set[str] = set()
        self.events: list[str] = []
        self.redacted: set[str] = set()
        self.notice_turns: list[str] = []
        self.turn: _Turn | None = None
        self._handlers: dict[str, Callable[[Action], object]] = {
            "add": self._add,
            "pause": self._pause,
            "resume": self._resume,
            "compact": self._compact,
            "race": self._race,
            "fold": self._fold,
            "fold_first_turn": self._fold_first_turn,
            "end": self._end,
            "crash_after_fold": self._crash_after_fold,
            "checkpoint": self._checkpoint,
            "cli_delegate": self._cli_delegate,
            "notice": self._notice,
            "scrub": self._scrub,
            "link": self._link,
            "delegation_state": self._delegation_state,
            "interrupted": self._interrupted,
            "discard": self._discard,
            "stale_save": self._stale_save,
            "member_late": self._member_late,
            "team_pause_advanced": self._team_pause_advanced,
            "force": self._force,
            "edit": self._edit,
            "redact": self._redact,
            "legacy": self._legacy,
            "reopen": self._reopen,
        }
        self.open_store()

    # The backend's primitives.

    @abstractmethod
    def open_store(self) -> None:
        """Open (or reopen) the conversation database the way the runtime does, migrations included."""

    @abstractmethod
    def load(self) -> ConversationSession | None:
        """Load the conversation as a reply does."""

    @abstractmethod
    def save_terminal(self, session: ConversationSession, record: AnyRunRecord) -> object:
        """Save a run's terminal record with its usage and the session's state, as ``persist_to`` does."""

    @abstractmethod
    def save_checkpoint(self, session: ConversationSession, record: AnyRunRecord) -> object:
        """Save a CLI batch checkpoint (a cancelled record) with the session's state."""

    @abstractmethod
    def save_stale(self, session: ConversationSession, record: AnyRunRecord) -> object:
        """Save an old copy of a run, as a late writer would."""

    @abstractmethod
    def save_session_state(self, session: ConversationSession, state: dict[str, object]) -> object:
        """Write a session state, as a concurrent run's state save does."""

    @abstractmethod
    def link_response(self, run_id: str, response_event_id: str) -> object:
        """Link a reply's event to the run that produced it."""

    @abstractmethod
    def finalize_notice(self, response_turn_id: str) -> object:
        """Finalize one response's queued-message notice."""

    @abstractmethod
    def scrub_notices(self, session: ConversationSession) -> object:
        """Recover crash-left notices of earlier responses in a reply's loaded session."""

    @abstractmethod
    def persist_delegation(self, session: ConversationSession, record: AnyRunRecord, state: DelegationState) -> object:
        """Save delegation state on ``record``, as ``persist_delegation_state`` does."""

    @abstractmethod
    def save_interrupted(
        self,
        session: ConversationSession,
        run_id: str,
        snapshot: InterruptedReplaySnapshot,
        state: dict[str, object] | None,
    ) -> object:
        """Save an interrupted record, as ``persist_interrupted_replay_snapshot`` does."""

    @abstractmethod
    def discard_run(self, session: ConversationSession, run_id: str) -> object:
        """Discard an empty or skipped run."""

    @abstractmethod
    def chunk_basis(self, session: ConversationSession, run_ids: Sequence[str], previous_summary: str | None) -> object:
        """Read a compaction chunk's basis, or None when the stored history changed since the load."""

    @abstractmethod
    def append_chunk(self, session: ConversationSession, basis: object, run_ids: Sequence[str], summary: str) -> bool:
        """Append a compaction chunk's summary."""

    @abstractmethod
    def chunk_summary_usage(self, session: ConversationSession) -> None:
        """Record a pre-reply compaction's summary usage."""

    @abstractmethod
    def fold_basis(self, session: ConversationSession) -> FoldBasisView | None:
        """Read a mid-turn fold's basis, or None when the stored history changed between its reads."""

    @abstractmethod
    def fold_summary_usage(self, session: ConversationSession) -> None:
        """Record a mid-turn fold's summary usage, before its summary is known to be accepted."""

    @abstractmethod
    def append_fold(
        self,
        session: ConversationSession,
        basis: FoldBasisView,
        summary: str,
        fold: FoldInput,
    ) -> bool:
        """Append a mid-turn fold's summary with what it took out of its running run."""

    @abstractmethod
    def cut_edit(self, event_id: str) -> object:
        """Remove the history an edited event's answer started."""

    @abstractmethod
    def redaction_cleanup(self, redacted: set[str]) -> object:
        """Run a reply's redaction cleanup, which sees every event in ``redacted`` as redacted."""

    @abstractmethod
    def set_force(self, session: ConversationSession, *, forced: bool) -> None:
        """Store or clear the scope's force-compaction flag."""

    @abstractmethod
    def request_force(self, session: ConversationSession) -> None:
        """Record a ``compact_context`` request of the turn ``session`` belongs to."""

    @abstractmethod
    def prepare_scope_state(self, session: ConversationSession) -> None:
        """Prepare the scope state of the reply that loaded ``session``, as a reply's preparation does."""

    @abstractmethod
    def seed_legacy(self, variant: int) -> None:
        """Write an empty database as an older release left it, then reopen it.

        Variant 0 is a pre-archive conversation: a replayed summary ``legacy summary`` in the session row, whose
        preserved seen ids are ``$legacy-1`` and ``$legacy-2``, and tombstones for those two runs. Variant 1 is a
        compaction archive: a legacy generation holding ``$legacy-0``, an ordinary generation that archived run
        ``a0``, a barrier, and an ordinary generation that archived run ``a1``. Variant 2 is a compaction archive
        whose legacy generation is still in force: the legacy generation and the ordinary one that archived ``a0``.
        """

    @abstractmethod
    def observe(self) -> dict[str, object]:
        """Return the conversation's observables now."""

    # The driver.

    def apply(self, action: Action) -> object:
        """Apply ``action`` at the next step's clock and return its outcome."""
        self.determinism.at(self.step)
        self.step += 1
        return self._handlers[action.kind](action)

    def known_run_ids(self) -> list[str]:
        """Return every run id the driver made, top-level runs first, in creation order."""
        return [*self.created, *(member for members in self.members.values() for member in members)]

    def dump(self) -> str:
        """Return the database as SQL, as ``sqlite3``'s ``iterdump`` writes it."""
        with closing(sqlite3.connect(self.database)) as connection:
            return "\n".join(connection.iterdump()) + "\n"

    def _stand_in(self) -> ConversationSession:
        return first_scope_session(self.scope, SESSION_ID)

    def _loaded_or_stand_in(self) -> ConversationSession:
        return self.load() or self._stand_in()

    def _waiting(self) -> list[str]:
        return [run_id for run_id in self.paused if run_id not in self.discarded]

    def _remember(self, record: AnyRunRecord) -> None:
        self.records[record.run_id] = deepcopy(record)
        self.versions.append(deepcopy(record))

    def _message(self, run_id: str, suffix: str, role: str, content: str | None = None, **fields: object) -> Message:
        return Message(
            role=role,  # type: ignore[arg-type]
            content=content,
            id=f"{run_id}.{suffix}",
            created_at=self.determinism.now,
            **fields,  # type: ignore[arg-type]
        )

    def _metadata(self, run_id: str) -> dict[str, object]:
        return {
            MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}",
            MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}", f"$ctx-{run_id}"],
            "room_id": ROOM_ID,
            "requester_id": REQUESTER_ID,
        }

    def _record(self, run_id: str, *, member_of: str | None = None, agent_id: str | None = None) -> AnyRunRecord:
        """Return a running record: the session's own run, or a member run of team run ``member_of``."""
        common = {
            "run_id": run_id,
            "session_id": SESSION_ID,
            "user_id": REQUESTER_ID,
            "status": RunStatus.running,
            "model": "oracle-model",
            "model_provider": "oracle",
            "metadata": self._metadata(member_of or run_id),
            "metrics": RunMetrics(),
            "created_at": self.determinism.now,
        }
        if self.kind == "team" and member_of is None:
            return TeamRunRecord(team_id=self.owner, **common)  # type: ignore[arg-type]
        return RunRecord(agent_id=agent_id or self.owner, parent_run_id=member_of, **common)  # type: ignore[arg-type]

    def archived_run(self, run_id: str) -> AnyRunRecord:
        """Return the completed run ``run_id`` an archive opening holds, as ``seed_legacy`` documents it."""
        record = self._record(run_id)
        record.status = RunStatus.completed
        record.content = f"answer {run_id}"
        record.messages = [
            Message(role="user", content=f"question {run_id}", id=f"{run_id}.u", created_at=self.determinism.now),
            Message(role="assistant", content=f"answer {run_id}", id=f"{run_id}.a", created_at=self.determinism.now),
        ]
        record.metadata = {
            MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}",
            MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}"],
            "room_id": ROOM_ID,
        }
        return record

    def _begin_turn(self) -> ConversationSession:
        """Prepare a reply: redaction cleanup, the session it loads or starts from, and its scope state."""
        if self.redacted:
            self.redaction_cleanup(set(self.redacted))
        session = self._loaded_or_stand_in()
        self.prepare_scope_state(session)
        return session

    def _new_turn(self, action: Action, *, cli: bool = False) -> _Turn:
        session = self._begin_turn()
        run_id = f"r{len(self.created)}"
        self.created.append(run_id)
        self.events.extend((f"${run_id}", f"$ctx-{run_id}"))
        turn = _Turn(
            session=session,
            record=self._record(run_id),
            system=self._message(run_id, "s", "system", "system prompt"),
            prompt=self._message(run_id, "u", "user", f"question {run_id}"),
            history=scope_history_messages(
                session.runs,
                scope=self.scope,
                num_history_runs=None,
                num_history_messages=None,
                max_tool_calls_from_history=None,
            ),
            cli=cli,
        )
        if action.index % 4 == 1:
            self._live_notice(turn)
        return turn

    def _live_notice(self, turn: _Turn) -> None:
        run_id = turn.record.run_id
        response_turn_id = f"turn-{run_id}"
        turn.body.append(
            self._message(
                run_id,
                "n",
                "user",
                f"newer message for {run_id}",
                provider_data={QUEUED_MESSAGE_NOTICE_MARKER_KEY: True, _NOTICE_TURN_KEY: response_turn_id},
            ),
        )
        self.notice_turns.append(response_turn_id)

    def _exchange(self, turn: _Turn, *, tool_name: str = "probe", member_paused: bool = False) -> None:
        """Add one tool batch to the turn; a team leader's delegates to a member, whose run ends and is stored."""
        record = turn.record
        run_id = record.run_id
        number = turn.batches
        turn.batches += 1
        turn.tokens += number + 1
        call_id = f"{run_id}.c{number}"
        if isinstance(record, TeamRunRecord):
            tool_name = "delegate_task_to_member"
            member = self._member(turn, number, paused=member_paused)
            result = "paused" if member_paused else f"member answer {member.run_id}"
        else:
            result = f"result {number}"
        call = self._message(
            run_id,
            f"b{number}",
            "assistant",
            f"step {number}",
            tool_calls=[{"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": "{}"}}],
            metrics=_usage(number + 1),
        )
        answer = self._message(run_id, f"t{number}", "tool", result, tool_call_id=call_id, tool_name=tool_name)
        record.tools = [
            *(record.tools or []),
            ToolExecution(
                tool_call_id=call_id,
                tool_name=tool_name,
                tool_args={},
                result=result,
            ),
        ]
        turn.body.extend((call, answer))
        turn.open_exchanges.append([call, answer])

    def _member(self, turn: _Turn, number: int, *, paused: bool) -> RunRecord:
        """End one member run of the turn's team run and store it; a paused member pauses the team run."""
        team = turn.record
        assert isinstance(team, TeamRunRecord)
        agent_id = _MEMBERS[number % len(_MEMBERS)]
        member_id = f"{team.run_id}-{agent_id}{number}"
        member = self._record(member_id, member_of=team.run_id, agent_id=agent_id)
        assert isinstance(member, RunRecord)
        member.messages = [self._message(member_id, "u", "user", f"task {member_id}")]
        if paused:
            call_id = f"{member_id}.q"
            tool = ToolExecution(
                tool_call_id=call_id,
                tool_name="dangerous",
                tool_args={},
                requires_confirmation=True,
            )
            member.messages.append(
                self._message(
                    member_id,
                    "q",
                    "assistant",
                    None,
                    tool_calls=[
                        {"id": call_id, "type": "function", "function": {"name": "dangerous", "arguments": "{}"}},
                    ],
                    metrics=_usage(1),
                ),
            )
            member.tools = [tool]
            member.requirements = [self._requirement(member_id, tool)]
            member.status = RunStatus.paused
            turn.paused_member = member
        else:
            member.messages.append(
                self._message(member_id, "a", "assistant", f"member answer {member_id}", metrics=_usage(1)),
            )
            member.content = f"member answer {member_id}"
            member.status = RunStatus.completed
        member.metrics = RunMetrics(tokens=_usage(1).tokens)
        self.members.setdefault(team.run_id, []).append(member_id)
        team.ended_member_runs.append(member)
        self._terminal(turn.session, member)
        return member

    def _requirement(self, run_id: str, tool: ToolExecution) -> RunRequirement:
        return RunRequirement(
            tool_execution=deepcopy(tool),
            id=f"{run_id}.req",
        )

    def _terminal(self, session: ConversationSession, record: AnyRunRecord) -> object:
        _assert_unique_message_ids(record)
        self.state_versions.append(session_state_snapshot(session.session_state))
        outcome = self.save_terminal(session, record)
        self._remember(record)
        return outcome

    def _close(self, turn: _Turn, outcome: str) -> object:
        """End ``turn``: it completes, stops, pauses, or crashes; a turn whose member paused pauses."""
        if self.turn is turn:
            self.turn = None
        if outcome == "crash":
            return "crashed"
        record = deepcopy(turn.record)
        run_id = record.run_id
        if outcome == "pause" or (outcome == "complete" and turn.paused_member is not None):
            self._pause_record(turn, record)
        elif outcome == "stop":
            turn.body.append(self._message(run_id, "p", "assistant", f"partial {run_id}", metrics=_usage(1)))
            record.status = RunStatus.cancelled
            record.content = f"partial {run_id}"
        else:
            turn.body.append(self._message(run_id, "a", "assistant", f"answer {run_id}", metrics=_usage(2)))
            record.status = RunStatus.completed
            record.content = f"answer {run_id}"
        record.messages = [
            message for message in turn.messages if message.add_to_agent_memory and not message.from_history
        ]
        record.metrics = RunMetrics(tokens=_usage(turn.tokens + 2).tokens)
        if isinstance(record, TeamRunRecord):
            record.ended_member_runs = []
        saved = self._terminal(turn.session, record)
        if record.status is RunStatus.paused and run_id not in self.paused:
            self.paused.append(run_id)
        return saved

    def _pause_record(self, turn: _Turn, record: AnyRunRecord) -> None:
        """Make ``record`` wait for approval: its own call, or, for a team run, the call of a member that paused."""
        run_id = record.run_id
        if isinstance(record, TeamRunRecord):
            if turn.paused_member is None:
                self._exchange(turn, member_paused=True)
                record.tools = deepcopy(turn.record.tools)
            member = turn.paused_member
            assert member is not None
            record.requirements = [
                replace(
                    requirement,
                    member_agent_id=member.agent_id,
                    member_display_name=member.agent_id,
                    member_run_id=member.run_id,
                )
                for requirement in deepcopy(member.requirements or [])
            ]
            record.member_responses = [deepcopy(member)]
            self.paused_members[run_id] = member.run_id
        else:
            number = turn.batches
            turn.batches += 1
            call_id = f"{run_id}.q{number}"
            tool = ToolExecution(
                tool_call_id=call_id,
                tool_name="dangerous",
                tool_args={},
                requires_confirmation=True,
            )
            turn.body.append(
                self._message(
                    run_id,
                    f"q{number}",
                    "assistant",
                    None,
                    tool_calls=[
                        {"id": call_id, "type": "function", "function": {"name": "dangerous", "arguments": "{}"}},
                    ],
                    metrics=_usage(1),
                ),
            )
            record.tools = [*(record.tools or []), tool]
            record.requirements = [self._requirement(f"{run_id}.q{number}", tool)]
        record.status = RunStatus.paused

    # Turns.

    def _add(self, action: Action, *, force: bool = False) -> object:
        """A turn that completes; it may run a tool batch, see a newer message, or change the session state."""
        turn = self._new_turn(action)
        if self.kind == "team" or action.index % 3 == 0:
            self._exchange(turn)
        if action.index % 2 == 0:
            turn.session.session_state["last_turn"] = turn.record.run_id
        if force:
            self.request_force(turn.session)
        return self._close(turn, "complete")

    def _pause(self, action: Action) -> object:
        """A turn that pauses for approval at once: its own call, or a team member's."""
        return self._close(self._new_turn(action), "pause")

    def _resume(self, action: Action) -> object:
        """An approval resumes a waiting run, which completes; a team run's paused member resumes first."""
        waiting = self._waiting()
        if not waiting:
            return "none-waiting"
        run_id = waiting[action.index % len(waiting)]
        session = self._begin_turn()
        paused = next((run for run in session.runs if run.run_id == run_id and run.status is RunStatus.paused), None)
        if paused is None:
            self.paused.remove(run_id)
            return "gone"
        embedded = paused.member_responses if isinstance(paused, TeamRunRecord) else []
        try:
            record = resumed_record(paused)
            members = [self._resumed_member(member) for member in embedded]
        except ValueError as error:
            return f"refused: {error}"
        self.paused.remove(run_id)
        for member in members:
            self._terminal(session, member)
        stored = [message for message in paused.messages if message.role != "system"]
        record.messages = [
            *stored,
            *(
                self._message(
                    run_id,
                    f"r{self.step}.{index}",
                    "tool",
                    "approved",
                    tool_call_id=requirement.tool_execution.tool_call_id,
                )
                for index, requirement in enumerate(record.requirements or [])
                if requirement.member_run_id is None
            ),
            self._message(run_id, f"ra{self.step}", "assistant", f"resumed {run_id}", metrics=_usage(2)),
        ]
        self._approve(record)
        record.status = RunStatus.completed
        record.content = f"resumed {run_id}"
        if isinstance(record, TeamRunRecord):
            record.member_responses = []
            record.ended_member_runs = []
        self.resumed.append(run_id)
        return self._terminal(session, record)

    def _resumed_member(self, paused: RunRecord) -> RunRecord:
        """Return a paused member run completed after its approval; raises when the run is no longer paused."""
        member = resumed_record(paused)
        member.messages = [
            *(message for message in paused.messages if message.role != "system"),
            self._message(member.run_id, f"r{self.step}", "tool", "approved", tool_call_id=f"{member.run_id}.q"),
            self._message(
                member.run_id,
                f"ra{self.step}",
                "assistant",
                f"member resumed {member.run_id}",
                metrics=_usage(1),
            ),
        ]
        self._approve(member)
        member.status = RunStatus.completed
        member.content = f"member resumed {member.run_id}"
        return member

    @staticmethod
    def _approve(record: AnyRunRecord) -> None:
        for requirement in record.requirements or []:
            requirement.confirmation = True
            requirement.tool_execution.confirmed = True
            requirement.tool_execution.result = "approved"
        for tool in record.tools or []:
            if tool.requires_confirmation:
                tool.confirmed = True
                tool.result = "approved"

    def _resume_as_turn(self, run_id: str) -> _Turn | None:
        """Continue a waiting agent run as the running turn, as an approval resume runs before it completes."""
        session = self._begin_turn()
        paused = next((run for run in session.runs if run.run_id == run_id and run.status is RunStatus.paused), None)
        self.paused.remove(run_id)
        if paused is None:
            return None
        record = resumed_record(paused)
        self._approve(record)
        system = next((message for message in paused.messages if message.role == "system"), None)
        prompt = next(message for message in paused.messages if message.role == "user")
        turn = _Turn(
            session=session,
            record=record,
            system=system or self._message(run_id, "s", "system", "system prompt"),
            prompt=deepcopy(prompt),
            history=scope_history_messages(
                session.runs,
                scope=self.scope,
                num_history_runs=None,
                num_history_messages=None,
                max_tool_calls_from_history=None,
            ),
            batches=len(record.tools or []),
        )
        rest = [
            deepcopy(message) for message in paused.messages if message.role != "system" and message.id != prompt.id
        ]
        rest.extend(
            self._message(
                run_id,
                f"r{self.step}.{index}",
                "tool",
                "approved",
                tool_call_id=requirement.tool_execution.tool_call_id,
            )
            for index, requirement in enumerate(record.requirements or [])
        )
        turn.body.extend(rest)
        self.resumed.append(run_id)
        return turn

    def _running_turn(self, action: Action, *, cli: bool = False) -> _Turn | None:
        """Return the running turn, starting one, or resuming a waiting approval, when none runs."""
        if self.turn is not None:
            return self.turn
        waiting = self._waiting()
        if action.index % 2 and waiting and self.kind == "agent" and not cli:
            turn = self._resume_as_turn(waiting[action.index % len(waiting)])
        else:
            turn = self._new_turn(action, cli=cli)
        self.turn = turn
        return turn

    def _end(self, action: Action) -> object:
        """End the running turn: it completes, stops, pauses, or crashes without a save."""
        if self.turn is None:
            return "no-turn"
        return self._close(self.turn, ("complete", "stop", "pause", "crash")[action.index % 4])

    # Compaction.

    def _chunk_candidates(self, session: ConversationSession, count: int) -> list[str]:
        return [run.run_id for run in session.runs if replays_in_scope(run, self.scope)][:count]

    def _compact(self, action: Action) -> object:
        """A pre-reply compaction chunk folds the oldest replayed runs; it clears a forced request once stored."""
        session = self.load()
        if session is None:
            return "no-session"
        chunk = self._chunk_candidates(session, action.count)
        if not chunk:
            return "nothing"
        previous = current_summary_text(session)
        basis = self.chunk_basis(session, chunk, previous)
        if basis is None:
            return "stale-basis"
        self.chunk_summary_usage(session)
        appended = self.append_chunk(session, basis, chunk, _summary_text(summary_tokens(previous) | set(chunk)))
        if appended and read_scope_state(session, self.scope).force_compact_before_next_run:
            self.set_force(session, forced=False)
        return {"appended": appended, "chunk": chunk}

    def _race(self, action: Action) -> object:
        """A chunk races another compaction or a redaction, before its basis read or during its summary call."""
        session = self.load()
        if session is None:
            return "no-session"
        chunk = self._chunk_candidates(session, action.count)
        if not chunk:
            return "nothing"
        previous = current_summary_text(session)
        during_summary_call = action.index % 4 >= 2
        basis = self.chunk_basis(session, chunk, previous) if during_summary_call else None
        racer = self._compact(Action("compact", count=1)) if action.index % 2 == 0 else self._redact(action)
        if not during_summary_call:
            basis = self.chunk_basis(session, chunk, previous)
        if basis is None:
            return {"racer": racer, "appended": None}
        self.chunk_summary_usage(session)
        appended = self.append_chunk(session, basis, chunk, _summary_text(summary_tokens(previous) | set(chunk)))
        return {"racer": racer, "appended": appended}

    def _fold(self, action: Action) -> object:
        """Fold the running turn's older exchanges mid-turn, starting a turn or resuming an approval when none runs.

        The prompt, live notices, and the newest exchange stay; a team turn's members end with the exchanges, and
        ``index % 7 == 5`` pauses one. With ``count == 3`` a pre-reply compaction commits during the summary call.
        """
        turn = self._running_turn(action)
        if turn is None:
            return "gone"
        if turn.stopped:
            return "stopped"
        if turn.paused_member is not None and not self.folds_after_member_pause:
            return "member-paused"
        self._exchange(turn)
        self._exchange(turn, member_paused=action.index % 7 == 5 and turn.paused_member is None)
        if turn.paused_member is not None and not self.folds_after_member_pause:
            return "member-paused"
        return self._fold_turn(turn, racing=action.count == 3)

    def _fold_turn(self, turn: _Turn, *, racing: bool, summary_accepted: bool = True) -> object:
        basis = self.fold_basis(turn.session)
        if basis is None:
            return "stale-basis"
        run_id = turn.record.run_id
        summary = _summary_text(summary_tokens(basis.summary) | set(basis.run_ids) | {f"{run_id}~"})
        racer = self._compact(Action("compact", count=1)) if racing else None
        self.fold_summary_usage(turn.session)
        if not summary_accepted:
            return "summary-rejected"
        folded = [message for exchange in turn.open_exchanges[:-1] for message in exchange]
        ended = turn.record.ended_member_runs if isinstance(turn.record, TeamRunRecord) else []
        members = tuple(member for member in ended if member.run_id not in turn.folded_members)
        appended = self.append_fold(
            turn.session,
            basis,
            summary,
            FoldInput(record=deepcopy(turn.record), messages=tuple(deepcopy(folded)), members=deepcopy(members)),
        )
        if not appended:
            turn.stopped = True
            return {"appended": False, "racer": racer}
        folded_ids = {message.id for message in folded}
        turn.body = [message for message in turn.body if message.id not in folded_ids]
        turn.open_exchanges = turn.open_exchanges[-1:]
        turn.folded_members |= {member.run_id for member in members}
        mark_summary_follows(turn.prompt)
        turn.summary = compaction_summary_message(summary, run_local=False)
        _carry_request_usage(turn.record, folded)
        return {"appended": True, "racer": racer, "folded": sorted(folded_ids)}

    def _fold_first_turn(self, action: Action) -> object:
        """A first turn folds before any run is stored; its summary is accepted, rejected, or cancelled."""
        if self.turn is not None or self.created or self.load() is not None:
            return "not-first"
        turn = self._running_turn(replace(action, index=0))
        assert turn is not None
        self._exchange(turn)
        self._exchange(turn)
        variant = action.index % 3
        outcome = self._fold_turn(turn, racing=False, summary_accepted=variant == 0)
        if variant == 2:
            return {"fold": outcome, "end": self._close(turn, "stop")}
        return outcome

    def _crash_after_fold(self, action: Action) -> object:
        folded = self._fold(action)
        return {"fold": folded, "end": self._close(self.turn, "crash") if self.turn is not None else "no-turn"}

    # CLI turns.

    def _checkpoint(self, action: Action) -> object:
        """A minimal-mode Bash batch is saved as a cancelled run before its calls run, history copies included."""
        if self.kind == "team":
            return "agent-only"
        if self.turn is not None and not self.turn.cli:
            return "busy"
        turn = self._running_turn(action, cli=True)
        assert turn is not None
        self._exchange(turn, tool_name="bash")
        if action.index % 2:
            # The batch's last call is still running, so its result is not in the snapshot yet.
            turn.body.pop()
            turn.open_exchanges[-1].pop()
        record = deepcopy(turn.record)
        record.messages = deepcopy(turn.messages)
        record.status = RunStatus.cancelled
        _assert_unique_message_ids(record)
        self.state_versions.append(session_state_snapshot(turn.session.session_state))
        outcome = self.save_checkpoint(turn.session, record)
        self._remember(record)
        return outcome

    def _cli_delegate(self, action: Action) -> object:
        """A running Bash call delegates: the live record, running and without messages, carries the state."""
        if self.kind == "team":
            return "agent-only"
        if self.turn is None or not self.turn.cli:
            self._checkpoint(action)
        turn = self.turn
        if turn is None or not turn.cli:
            return "busy"
        state = DelegationState(gates={f"{turn.record.run_id}.c{turn.batches - 1}": True})
        outcome = self.persist_delegation(turn.session, turn.record, state)
        self._remember(turn.record)
        return outcome

    # Writers of stored runs.

    def _notice(self, action: Action) -> object:
        if not self.notice_turns:
            return "no-notice"
        return self.finalize_notice(self.notice_turns[action.index % len(self.notice_turns)])

    def _scrub(self, _action: Action) -> object:
        return self.scrub_notices(self._begin_turn())

    def _link(self, action: Action) -> object:
        if not self.created:
            return "no-run"
        run_id = self.created[action.index % len(self.created)]
        event_id = f"$reply-{run_id}"
        if event_id not in self.events:
            self.events.append(event_id)
        return self.link_response(run_id, event_id)

    def _delegation_state(self, action: Action) -> object:
        """Delegation state is saved on a waiting run, or on a run after its continuation completed."""
        candidates = self._waiting() if action.index % 2 == 0 else self.resumed
        if not candidates:
            return "no-run"
        run_id = candidates[(action.index // 2) % len(candidates)]
        record = deepcopy(self.records[run_id])
        outcome = self.persist_delegation(
            self._loaded_or_stand_in(),
            record,
            DelegationState(gates={f"{run_id}:{self.step}": True}),
        )
        self._remember(record)
        return outcome

    def _interrupted(self, action: Action) -> object:
        """An interrupted record replaces a stopped run, or a completed one as voice playout and late failures do.

        Variant 0 saves over a cancelled run through the recovery's loaded session, variant 1 over a completed run
        through a fresh load, as voice playout does, and variant 2 over a completed run through a stand-in session,
        as a failure after the terminal save does.
        """
        variant = action.index % 3
        wanted = RunStatus.cancelled if variant == 0 else RunStatus.completed
        candidates = [
            run_id for run_id in self.created if run_id in self.records and self.records[run_id].status is wanted
        ]
        if not candidates:
            return "no-run"
        run_id = candidates[(action.index // 3) % len(candidates)]
        record = self.records[run_id]
        session = self._stand_in() if variant == 2 else self._loaded_or_stand_in()
        snapshot = build_interrupted_replay_snapshot(
            user_message=f"question {run_id}",
            user_message_is_structured=False,
            partial_text=f"spoken part of {run_id}",
            completed_tools=[
                ToolTraceEntry(
                    type="tool_call_completed",
                    tool_name=tool.tool_name or "",
                    tool_call_id=tool.tool_call_id,
                )
                for tool in record.tools or []
            ],
            interrupted_tools=(),
            run_metadata=record.metadata,
            original_status=record.status,
        )
        outcome = self.save_interrupted(session, run_id, snapshot, None)
        self.records[run_id] = replace(record, status=RunStatus.completed)
        return outcome

    def _discard(self, action: Action) -> object:
        """An empty completed run, or a skipped attempt (the running turn's, when one runs), is discarded."""
        if action.index % 2 == 0:
            turn = self._new_turn(action)
            record = deepcopy(turn.record)
            record.messages = [turn.prompt, self._message(record.run_id, "a", "assistant", "", metrics=_usage(1))]
            record.status = RunStatus.completed
            record.metrics = RunMetrics(tokens=_usage(1).tokens)
            self._terminal(turn.session, record)
        else:
            turn = self.turn if self.turn is not None else self._new_turn(action)
            self._close(turn, "complete")
        self.discarded.add(turn.record.run_id)
        return self.discard_run(turn.session, turn.record.run_id)

    def _stale_save(self, action: Action) -> object:
        """A late writer saves an old copy of a run, or an old session state (``index % 3 == 2``)."""
        if action.index % 3 == 2:
            if not self.state_versions:
                return "no-state"
            state = deepcopy(self.state_versions[(action.index // 3) % len(self.state_versions)])
            return self.save_session_state(self._loaded_or_stand_in(), state)
        if not self.versions:
            return "no-run"
        record = deepcopy(self.versions[(action.index // 3) % len(self.versions)])
        return {"run_id": record.run_id, "saved": self.save_stale(self._loaded_or_stand_in(), record)}

    def _member_late(self, action: Action) -> object:
        """A member run saves again after its team run completed."""
        candidates = [
            member_id
            for run_id in self.created
            if run_id in self.records and self.records[run_id].status is RunStatus.completed
            for member_id in self.members.get(run_id, [])
            if self.records[member_id].status is RunStatus.completed
        ]
        if not candidates:
            return "no-member"
        record = deepcopy(self.records[candidates[action.index % len(candidates)]])
        record.content = f"late answer {record.run_id}"
        return {"run_id": record.run_id, "saved": self._terminal(self._loaded_or_stand_in(), record)}

    def _team_pause_advanced(self, action: Action) -> object:
        """A paused team run's member resumes and stores its row, and the team run's own save never happens."""
        candidates = [
            member_id
            for run_id in self._waiting()
            if (member_id := self.paused_members.get(run_id)) is not None
            and self.records[member_id].status is RunStatus.paused
        ]
        if not candidates:
            return "no-team-run"
        member = self._resumed_member(self.records[candidates[action.index % len(candidates)]])
        return {"member": member.run_id, "saved": self._terminal(self._loaded_or_stand_in(), member)}

    def _force(self, action: Action) -> object:
        """A ``compact_context`` call requests compaction before the next reply, from the running turn or a new one."""
        if self.turn is None:
            return self._add(action, force=True)
        self.request_force(self.turn.session)
        return "requested"

    # Cuts and openings.

    def _edit(self, action: Action) -> object:
        answered = [event for event in self.events if not event.startswith(("$ctx-", "$reply-", "$read-by-"))]
        if not answered:
            return "no-event"
        event_id = answered[action.index % len(answered)]
        return {"event": event_id, "cut": self.cut_edit(event_id)}

    def _redact(self, action: Action) -> object:
        if not self.events:
            return "no-event"
        event_id = self.events[action.index % len(self.events)]
        self.redacted.add(event_id)
        return {"event": event_id, "removed": self.redaction_cleanup(set(self.redacted))}

    def _legacy(self, action: Action) -> object:
        if self.step != 1 or self.load() is not None:
            return "not-first"
        variant = action.index % len(LEGACY_EVENTS)
        self.seed_legacy(variant)
        self.events.extend(LEGACY_EVENTS[variant])
        return variant

    def _reopen(self, _action: Action) -> object:
        self.open_store()
        return "reopened"
