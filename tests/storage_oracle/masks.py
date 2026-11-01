"""Comparison 1's named masks: each deliberate difference of the storage v2 store from the Agno-layout one.

A recording keeps most steps' observables only as digests, so no mask can turn a recorded observable into the new
one. Each mask works the other way: ``MaskedBackend`` drives the v2 store, asserts the new behavior where the change
shows, and reports its outcomes and observables as the Agno-layout store reported them under that one change, so
they compare with the recording digest for digest. A change whose effect reaches later actions (one store writes a
run the other refuses) cannot be undone that way; its mask asserts the new behavior and ends the comparison of that
sequence (``Diverged``).

To undo message differences, the backend checks every run the v2 store shows against what its writes left
(``StoreBackend.expected_view``), and keeps the message list the Agno-layout store held of each run, which kept history
copies, and the messages a fold took until a whole-run rewrite of a loaded copy. It also keeps where that store's
copy of a manual compaction request in the session state was: in each loaded session's state, as each one last
stored it, and in the stored row.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any, override

from mindroom.constants import MATRIX_RESPONSE_EVENT_ID_METADATA_KEY, MINDROOM_HISTORY_METADATA_KEY
from mindroom.delegation.state import DELEGATION_STATE_KEY
from mindroom.history.run_visibility import is_queued_notice_message
from mindroom.history.storage import has_queued_force_compaction
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.llm.usage import TOKEN_FIELDS, ModelMetrics, RunMetrics, TokenCounts
from mindroom.session_storage.legacy_scope_metadata import (
    LEGACY_COMPACTION_METADATA_KEY,
    LEGACY_MATRIX_HISTORY_METADATA_KEY,
)
from mindroom.session_storage.schema import NON_VISIBLE_RUN_STATUS_VALUES, quote_identifier
from mindroom.session_storage.store import ConversationStore
from mindroom.session_storage.summary_rows import folded_tool_call_ids, history_event_ids, latest_row
from tests.storage_oracle import notice_rewrites
from tests.storage_oracle.backend import SESSION_ID
from tests.storage_oracle.observe import probe_request, resume_requests
from tests.storage_oracle.store_backend import (
    StoreBackend,
    edit_cut,
    redaction_cut,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from mindroom.delegation.state import DelegationState
    from mindroom.history.interrupted_replay import InterruptedReplaySnapshot
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.types import ConversationSession
    from tests.storage_oracle.actions import Action, ScenarioKind
    from tests.storage_oracle.backend import FoldBasisView, FoldInput
    from tests.storage_oracle.determinism import Determinism


# Each deliberate difference, by name, and where the design decides it.
MASKS: dict[str, str] = {
    "tables_exist_at_open": "spec v4 3.13: opening a store creates every table",
    "outcome_names": "the v2 operations name their results differently; no behavior changes",
    "visible_run_is_final": "spec v4 3.15, row 1: any visible run is final for P1",
    "interrupted_record_replaces_covered_run": "spec v4 3.15, row 2",
    "member_final_once_team_visible": "spec v4 3.15, row 3",
    "paused_team_members_from_requirements": "spec v4 3.15, row 4",
    "link_reaches_any_stored_run": "spec v4 3.15, row 5",
    "history_copies_not_stored": "spec v4 3.15, row 6",
    "fold_stores_cancelled_row": "spec v4 3.15, row 7",
    "first_stored_by_fold_sorts_at_fold": "spec v4 3.15, row 8",
    "minimal_mode_delegation_patches_checkpoint": "spec v4 3.15, row 10",
    "stored_copy_hides_folded_messages": "spec v4 3.15, row 11",
    "delegation_state_is_a_key_patch": "spec v4 3.6 and 3.7 (v4 change 2): a record that is not paused, or is final, "
    "gets only its delegation key; 3.15 lists only the minimal-mode case",
    "folded_calls_go_with_their_run": "not in spec v4: a discarded or retired fold owner's folded calls go with its "
    "message rows, while its fold row stays",
    "force_request_only_in_metadata": "decision 5 (W9): a manual compaction request is only the metadata flag, with no "
    "copy in the session state that the next reply moves into the flag",
    "ledger_is_the_only_usage_owner": "a run's usage lives only in the ledger: the session row keeps no totals "
    "beside it",
    "readers_follow_the_log": "background readers list a conversation's turns in log order, where a turn finished "
    "after an approval comes after the turns that finished meanwhile, not in creation order",
    "a_finished_interrupted_record_is_news": "an interrupted record new to the log, or saved over its cancelled or "
    "errored row, moves the session's update time, as every save that places a visible run at the log's end does, so "
    "readers that follow the log see it",
    "history_state_is_the_owners_one_value": "layout version 7: the owner's force flag and preserved seen ids are one "
    "metadata value, not maps keyed by scope",
    "summary_requests_name_their_model": "#3109 item 6: a compaction summary's usage row names the model that answered "
    "its request, as its run does",
    "paused_calls_stored_once": "#3106 item 2: a run stores each paused call once, as its tool execution, and no "
    "requirements beside it; a converted delegation call keeps its requirement's id as its pause id",
    "resumes_leave_out_the_paused_reply_s_notice": "#3108: a paused run's resume request leaves out the queued-message "
    "notice the paused run holds, which the reply that paused was given",
    "queued_notices_stored_once": "#3108: a reply stores each queued-message notice once, as its run held it; nothing "
    "finalizes it after delivery or recovers it when a reply opens its scope",
    "preserved_seen_ids_not_read": "#3463: seen ids earlier releases preserved in session metadata no longer count as "
    "seen, so a redaction no longer rewrites the metadata to drop them",
}
_NON_VISIBLE = frozenset(NON_VISIBLE_RUN_STATUS_VALUES)
_MISSING = object()
# Where the Agno-layout store's runtime also queued a manual compaction request: a list of scope keys in the state.
_PENDING_KEY = "mindroom_pending_compaction_scope_keys"


def untag_summary_requests(ledger: object) -> bool:
    """Drop the model each compaction summary request in ``ledger`` names; return whether any named one.

    The Agno-layout store's summary rows named the model only for the row, and a request names the row's model.
    """
    untagged = False
    for _run_id, snapshot in ledger if isinstance(ledger, list) else ():
        if snapshot.get("kind") != "compaction_summary":
            continue
        for request in snapshot["requests"]:
            if "model" in request:
                named = request.pop("model"), request.pop("model_provider")
                assert named == (snapshot["model"], snapshot["model_provider"]), named
                untagged = True
    return untagged


@dataclass(frozen=True)
class Diverged:
    """The change that made the two stores' states differ for the rest of a sequence, and where it showed."""

    mask: str
    reason: str


@dataclass(frozen=True)
class _Cut:
    """What a cut may change, read before it runs.

    Every run row, the old store's copies of the members of fold-created rows, and a copy of the database.
    """

    rows: dict[str, _Row]
    members: dict[str, dict[str, Any]]
    copy: Path | None
    fold_rows: frozenset[str]


@dataclass(frozen=True)
class _Row:
    """What the masks read of a v2 run row."""

    parent_run_id: str | None
    status: str
    seq: int
    scope_key: str


@dataclass
class _Pending:
    """Whether the Agno-layout store's copy of one session held the queued request: in its state, and as last stored.

    The session is kept so that no other session takes its ``id`` while the backend tracks it.
    """

    session: ConversationSession
    live: bool
    held: bool


def _old_kept(messages: Sequence[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    """Return the messages the Agno-layout store kept of a saved run: all but system ones unless it is paused."""
    return [message for message in messages if status == RunStatus.paused.value or message["role"] != "system"]


def _dicts(record: AnyRunRecord) -> list[dict[str, Any]]:
    return [message.to_dict() for message in record.messages]


def _decoded(data: dict[str, Any]) -> AnyRunRecord:
    return TeamRunRecord.from_dict(data) if "member_responses" in data else RunRecord.from_dict(data)


def _segment_call_ids(fold: FoldInput) -> frozenset[str]:
    """Return the tool calls the Agno-layout store's segment of ``fold`` named, as ``segment_tool_call_ids`` reads."""
    calls = {
        call["id"] for message in fold.messages for call in message.tool_calls or [] if isinstance(call.get("id"), str)
    }
    calls |= {
        tool.tool_call_id
        for member in fold.members
        for tool in member.tools or []
        if isinstance(tool.tool_call_id, str)
    }
    return frozenset(calls)


def _old_reader_view(runs: Sequence[AnyRunRecord]) -> list[str]:
    """Return what the Agno-layout store's background readers listed: its finished or paused Matrix turns by creation."""
    turns = [
        run
        for run in runs
        if isinstance(run, RunRecord)
        and run.parent_run_id is None
        and run.status in (RunStatus.completed, RunStatus.paused)
        and (run.metadata or {}).get("room_id")
    ]
    return [run.run_id for run in sorted(turns, key=lambda run: (run.created_at, run.run_id))]


def _counted_tokens(values: dict[str, Any]) -> TokenCounts:
    """Return the counters of a stored usage dict the way the old totals counted them: a non-count counts as 0."""
    return TokenCounts(
        **{
            name: value if isinstance(value := values.get(name), int) and not isinstance(value, bool) else 0
            for name in TOKEN_FIELDS
        },
    )


def _old_session_totals(ledger: object) -> dict[str, Any] | None:
    """Return the totals the Agno-layout store kept in ``session_data``: every run snapshot's counters, per model too.

    That store moved them by the difference on each usage save, so they were the sum of its run rows; helper rows
    (those with a ``kind``) never counted. None when no run recorded usage.
    """
    snapshots = (
        [row[1] for row in ledger if isinstance(row[1], dict) and "kind" not in row[1]]
        if isinstance(ledger, list)
        else []
    )
    if not snapshots:
        return None
    totals = RunMetrics()
    for snapshot in snapshots:
        metrics = snapshot.get("metrics")
        if not isinstance(metrics, dict):
            continue
        totals.tokens = totals.tokens + _counted_tokens(metrics)
        details = metrics.get("details")
        models = details.get("model") if isinstance(details, dict) else None
        if not isinstance(models, list):
            continue
        if totals.details is None:
            totals.details = {}
        entries = totals.details.setdefault("model", [])
        for raw in models:
            if not isinstance(raw, dict):
                continue
            model_id, provider = raw.get("id"), raw.get("provider")
            entry = ModelMetrics(
                tokens=_counted_tokens(raw),
                id=model_id if isinstance(model_id, str) else "",
                provider=provider if isinstance(provider, str) else "",
            )
            same = next((total for total in entries if (total.provider, total.id) == (entry.provider, entry.id)), None)
            if same is None:
                entries.append(entry)
            else:
                same.accumulate(entry)
    return totals.to_dict()


def drop_inert_session_totals(observed: dict[str, Any]) -> None:
    """Leave out the usage totals a converted row still holds in ``session_data``, which nothing reads any more."""
    row = observed["row"]
    data = row.get("session_data") if isinstance(row, dict) else None
    if isinstance(data, dict):
        data.pop("session_metrics", None)


def empty_missing_metadata(observed: dict[str, Any]) -> bool:
    """Show a session without metadata as one with empty metadata, which an old store's redaction rewrote it to.

    Returns whether the session had no metadata.
    """
    session = observed["session"]
    if isinstance(session, dict) and session["metadata"] is None:
        session["metadata"] = {}
        return True
    return False


def readers_in_creation_order(observed: dict[str, Any], used: set[str]) -> None:
    """Show the turns readers list, which follow the log, in the creation order the old readers sorted them in."""
    turns, listed = observed["session_runs"], observed["list_runs"]
    if not (isinstance(turns, list) and isinstance(listed, list)):
        return
    created = {run["run_id"]: (run["created_at"], run["run_id"]) for run in listed}
    ordered = sorted(turns, key=created.__getitem__)
    if ordered != turns:
        observed["session_runs"] = ordered
        used.add("readers_follow_the_log")


class MaskedBackend(StoreBackend):
    """``StoreBackend``, reporting what the Agno-layout store would have reported, change by named change."""

    def __init__(self, root: Path, kind: ScenarioKind, determinism: Determinism) -> None:
        self.used: set[str] = set()
        self.diverged: Diverged | None = None
        # The message list the old store held of each run.
        self.old_messages: dict[str, list[dict[str, Any]]] = {}
        # A paused team run's members as the Agno-layout store embedded them at the team run's save.
        self.embedded: dict[str, list[dict[str, Any]]] = {}
        # Runs whose only row is the one their fold created, and fold-created runs the old store placed later.
        self.fold_rows: set[str] = set()
        self.orphans: dict[str, dict[str, Any]] = {}
        self.placed: dict[str, float] = {}
        # Changes only the v2 store made: reply links, delegation keys, and interrupted records of covered runs.
        self.link_only: set[str] = set()
        self.delegation_only: dict[str, object] = {}
        self.replaced_covered: dict[str, dict[str, Any]] = {}
        # The Agno-layout store's queued request: per session by ``id``, in the stored row, and the latest time only
        # its writes moved the row's update time to.
        self.pending: dict[int, _Pending] = {}
        self.pending_stored = False
        self.pending_written_at: int | None = None
        self.flag_restored = False
        # The update time an interrupted record's save moved the row to, and the one the old store left it at.
        self.news_at: tuple[int, int] | None = None
        # When a redaction's cut made the old store rewrite a missing metadata value as an empty one, dropping the seen
        # ids earlier releases preserved there, which no recorded sequence holds.
        self.metadata_rewritten_at: int | None = None
        # The probe request, resume requests, and loaded runs of the old store's messages, when its notice rewrites set
        # them apart from the v2 store's at the latest observation.
        self.held_requests: tuple[object, object, list[dict[str, Any]]] | None = None
        super().__init__(root, kind, determinism)

    # What the masks read of the v2 store.

    def _query(self, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        with closing(sqlite3.connect(self.store.path)) as connection:
            return connection.execute(sql, parameters).fetchall()

    def _rows(self) -> dict[str, _Row]:
        return {
            run_id: _Row(*rest)
            for run_id, *rest in self._query(
                f"SELECT run_id, parent_run_id, status, seq, scope_key FROM {quote_identifier(self.names.log_runs)}",  # noqa: S608
            )
        }

    def _covered(self, run_id: str) -> bool:
        """Return whether ``run_id``'s top-level run is visible and its scope's latest summary covers it: frozen."""
        rows = self._rows()
        row = rows.get(run_id)
        top = rows.get(row.parent_run_id) if row is not None and row.parent_run_id is not None else row
        if top is None or top.parent_run_id is not None or top.status in _NON_VISIBLE:
            return False
        latest = latest_row(self.store, session_id=SESSION_ID, scope_key=top.scope_key)
        return latest is not None and top.seq <= latest.through

    def _retired(self, run_id: str) -> bool:
        """Return whether ``run_id`` sorts at or before its scope's latest barrier, which retired it."""
        row = self._rows().get(run_id)
        if row is None:
            return False
        barrier = self._query(
            f"SELECT through FROM {quote_identifier(self.names.log_summaries)} WHERE session_id = ? "  # noqa: S608
            "AND scope_key = ? AND summary IS NULL ORDER BY id DESC LIMIT 1",
            (SESSION_ID, row.scope_key),
        )
        return bool(barrier) and row.seq <= barrier[0][0]

    def _fenced(self, record: AnyRunRecord) -> bool:
        removed = quote_identifier(self.names.log_removed)
        return bool(
            self._query(
                f"SELECT 1 FROM {removed} WHERE session_id = ? AND run_id IN (?, ?)",  # noqa: S608
                (SESSION_ID, record.run_id, record.parent_run_id),
            ),
        )

    def _fold_row_ids(self) -> set[int]:
        summaries = quote_identifier(self.names.log_summaries)
        return {row_id for (row_id,) in self._query(f"SELECT id FROM {summaries} WHERE fold_run_id IS NOT NULL")}  # noqa: S608

    def _diverge(self, mask: str, reason: str) -> None:
        self.used.add(mask)
        if self.diverged is None:
            self.diverged = Diverged(mask, reason)

    # The models.

    def _wrote(self, record: AnyRunRecord, status: str | None = None) -> None:
        """Record a full save the v2 store wrote; the old store wrote it too unless the caller says otherwise."""
        run_id, stored_status = record.run_id, status or record.status.value
        self.old_messages[run_id] = _old_kept(_dicts(record), stored_status)
        if isinstance(record, TeamRunRecord) and record.status is RunStatus.paused:
            self.embedded[run_id] = [self._old_requirements(member.to_dict()) for member in record.member_responses]
        else:
            self.embedded.pop(run_id, None)
        if run_id in self.fold_rows:
            self.fold_rows.discard(run_id)
            if record.parent_run_id is None and stored_status in _NON_VISIBLE:
                # The old store's first row of this run went after everything stored now; the fold's stays earlier.
                (next_seq,) = self._query(f"SELECT next_seq FROM {quote_identifier(self.names.log_meta)}")[0]  # noqa: S608
                self.placed[run_id] = next_seq - 0.5
        elif run_id in self.placed and stored_status not in _NON_VISIBLE:
            # Saved visible, it moves after everything in both stores.
            del self.placed[run_id]

    def _sync(self) -> None:
        """Forget what the models hold of runs the v2 store no longer has, and check the paused teams' members."""
        rows = self._rows()
        for held in (
            self.old_messages,
            self.embedded,
            self.placed,
            self.delegation_only,
            self.replaced_covered,
        ):
            for run_id in [run_id for run_id in held if run_id not in rows]:
                del held[run_id]
        self.fold_rows &= rows.keys()
        self.link_only &= rows.keys()
        for run_id in self.replaced_covered:
            if not self._covered(run_id):
                self._diverge(
                    "interrupted_record_replaces_covered_run",
                    f"a cut uncovered {run_id}, which replays its interrupted record only in the v2 store",
                )
        for team_id, embedded in self.embedded.items():
            team = self.store.get_run(team_id)
            if isinstance(team, TeamRunRecord) and team.status is RunStatus.paused:
                members = [self._old_run(member.to_dict(), loaded=True) for member in team.member_responses]
                if members != embedded:
                    # Spec 3.10: a paused team run's members are its paused member rows, as stored now.
                    assert all(member.status is RunStatus.paused for member in team.member_responses)
                    self._diverge(
                        "paused_team_members_from_requirements",
                        f"paused team run {team_id} resumes its members' rows, not the copies it embedded",
                    )

    # Outcomes and the old store's writes.

    @override
    def apply(self, action: Action) -> object:
        self.flag_restored = False
        outcome = super().apply(action)
        if self.flag_restored and not has_queued_force_compaction(self.store, SESSION_ID):
            # The old reply moved a queued copy into the flag after a compaction had cleared it, and no request of
            # this action set the flag again, so only the old store compacts again.
            self._diverge(
                "force_request_only_in_metadata",
                "the Agno-layout store's queued copy asked for a compaction again after one cleared the flag",
            )
        self._sync()
        return outcome

    def _tracked(self, session: ConversationSession, *, pending: bool) -> ConversationSession:
        self.pending[id(session)] = _Pending(session, live=pending, held=pending)
        return session

    @override
    def load(self) -> ConversationSession | None:
        session = super().load()
        # A loaded session's state is the stored one, which held the request where the old store's did.
        return session if session is None else self._tracked(session, pending=self.pending_stored)

    @override
    def _stand_in(self) -> ConversationSession:
        return self._tracked(super()._stand_in(), pending=False)

    @override
    def request_force(self, session: ConversationSession) -> None:
        super().request_force(session)
        # The old store's tool also queued the request in the turn's state, which the turn's next state save stored.
        self.pending[id(session)].live = True

    @override
    def prepare_scope_state(self, session: ConversationSession) -> None:
        super().prepare_scope_state(session)
        pending = self.pending[id(session)]
        if not pending.live:
            return
        # The old reply stored the flag, then its state without the request. The flag is set already unless a
        # compaction cleared it after the request was stored.
        self.flag_restored = self.flag_restored or not has_queued_force_compaction(self.store, SESSION_ID)
        pending.live = pending.held = self.pending_stored = False
        self.pending_written_at = self.determinism.now

    def _old_state_save(self, session: ConversationSession, state: dict[str, object] | None) -> None:
        """Follow the old store's write of the state a run save carries, before the v2 store writes its own."""
        pending = self.pending[id(session)]
        unchanged = state == session.stored_session_state
        if state is None or (unchanged and pending.live == pending.held):
            return
        if unchanged:
            # Only the queued request made the run's state differ from what the old store held, so only it wrote.
            if self._stored_state() != state:
                self._diverge(
                    "force_request_only_in_metadata",
                    "the Agno-layout store saved a run's state over the one another writer stored",
                )
            self.pending_written_at = self.determinism.now
        self.pending_stored = pending.held = pending.live

    def _stored_state(self) -> object:
        sessions = quote_identifier(self.names.sessions)
        [(session_state,)] = self._query(f"SELECT session_state FROM {sessions} WHERE session_id = ?", (SESSION_ID,))  # noqa: S608
        return json.loads(session_state)

    @override
    def _save(
        self,
        session: ConversationSession,
        record: AnyRunRecord,
        *,
        usage: bool,
        state: dict[str, object] | None,
    ) -> list[str]:
        self._old_state_save(session, state)
        before = self._old_list_entry(record.run_id)
        [outcome] = super()._save(session, record, usage=usage, state=state)
        if outcome == "written":
            self._wrote(record)
            if any(message.get("from_history") for message in self.old_messages[record.run_id]):
                self.used.add("history_copies_not_stored")
            return ["written"]
        if outcome == "removed":
            return ["removed"]
        top = record.parent_run_id or record.run_id
        if self._covered(top):
            self.used.add("outcome_names")
            return ["frozen"]
        # The old store writes this copy over a visible run; the v2 store keeps the run as it is.
        assert self._old_list_entry(record.run_id) == before, f"a final save changed {record.run_id}"
        mask = "member_final_once_team_visible" if record.parent_run_id else "visible_run_is_final"
        written = self._old_requirements(
            {**record.to_dict(), "messages": _old_kept(_dicts(record), record.status.value)},
        )
        if usage or before != written:
            self._diverge(mask, f"{record.run_id} saved over a visible run that the Agno-layout store rewrote")
        self.used.add(mask)
        return ["written"]

    @override
    def link_response(self, run_id: str, response_event_id: str) -> object:
        session = self.load()
        loaded = next(
            (run for run in self._old_view_runs(session.runs if session is not None else []) if run.run_id == run_id),
            None,
        )
        stored = self.store.get_run(run_id)
        before = (stored.metadata or {}).get(MATRIX_RESPONSE_EVENT_ID_METADATA_KEY) if stored is not None else None
        patched = super().link_response(run_id, response_event_id)
        if session is None:
            return "no-session"
        if loaded is None:
            if patched != "not-stored" and before != response_event_id:
                linked = self.store.get_run(run_id)
                assert linked is not None
                assert (linked.metadata or {})[MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] == response_event_id
                if self._covered(run_id) and not self._retired(run_id):
                    # The covering row's event sets now hold the reply, so redacting it cuts that summary.
                    assert response_event_id in history_event_ids(
                        self.store,
                        session_id=SESSION_ID,
                        scope_key=self.scope.key,
                        kind="all",
                    )
                self.link_only.add(run_id)
                self.used.add("link_reaches_any_stored_run")
            return "not-loaded"
        if (loaded.metadata or {}).get(MATRIX_RESPONSE_EVENT_ID_METADATA_KEY) == response_event_id:
            return "linked"
        # The old store rewrote the loaded copy whole, so its row no longer holds what folds took.
        self.old_messages[run_id] = _dicts(loaded)
        self.link_only.discard(run_id)
        return ["written"]

    def _old_notice_rewrite(self, changed: Sequence[AnyRunRecord]) -> object:
        """Hold the messages of the runs the old store's notice rewrite changed, which the v2 store keeps; report it."""
        for run in changed:
            self.old_messages[run.run_id] = _old_kept(_dicts(run), run.status.value)
            self.used.add("queued_notices_stored_once")
        return ["written"] * len(changed) if changed else "unchanged"

    def _stored_messages(self) -> list[list[dict[str, Any]]]:
        return [_dicts(run) for run in self.store.list_runs(SESSION_ID)]

    @override
    def finalize_notice(self, response_turn_id: str) -> object:
        # The v2 store keeps every notice as its run stored it; the old store rewrote the reply's notices.
        session, stored = self.load(), self._stored_messages()
        outcome = super().finalize_notice(response_turn_id)
        assert self._stored_messages() == stored, "a notice finalization changed the v2 store"
        if session is None:
            return outcome
        return self._old_notice_rewrite(notice_rewrites.finalize(self._old_view_runs(session.runs), response_turn_id))

    @override
    def scrub_notices(self, session: ConversationSession) -> object:
        stored = self._stored_messages()
        super().scrub_notices(session)
        assert self._stored_messages() == stored, "a reply's opening of its scope changed the v2 store's notices"
        return self._old_notice_rewrite(notice_rewrites.recover(self._old_view_runs(session.runs)))

    @override
    def persist_delegation(self, session: ConversationSession, record: AnyRunRecord, state: DelegationState) -> object:
        run_id, paused = record.run_id, record.status is RunStatus.paused
        stored = self.store.get_run(run_id)
        key_before = (stored.metadata or {}).get(DELEGATION_STATE_KEY, _MISSING) if stored is not None else _MISSING
        covered, fenced = self._covered(run_id), self._fenced(record)
        result = self._persist_delegation(session, record, state)
        if result == "written":
            self._wrote(record)
            return None
        if result == "removed" or fenced:
            return None
        if covered:
            # The old store refused the whole save as frozen; the v2 store patched the key onto the final run.
            patched = self.store.get_run(run_id)
            assert patched is not None
            assert (patched.metadata or {})[DELEGATION_STATE_KEY] == state.to_dict()
            self.delegation_only.setdefault(run_id, key_before)
            self.used.add("delegation_state_is_a_key_patch")
            return None
        reason = f"the Agno-layout store saved {run_id}'s delegation record whole over what it held"
        if paused:
            # P1 refused a paused copy of a visible run, which the old store wrote over it.
            self._diverge("visible_run_is_final", reason)
        elif record.status is RunStatus.running:
            # The checkpoint row keeps its status and messages, and gains the delegation key.
            checkpoint = self.store.get_run(run_id)
            assert checkpoint is not None
            assert checkpoint.status is not RunStatus.running
            assert (checkpoint.metadata or {})[DELEGATION_STATE_KEY] == state.to_dict()
            self._diverge(
                "minimal_mode_delegation_patches_checkpoint",
                f"the live record of {run_id} replaced its checkpoint row in the Agno-layout store",
            )
        elif not result:
            self._diverge("delegation_state_is_a_key_patch", f"the Agno-layout store stored {run_id} again")
        else:
            written = {**record.to_dict(), "messages": _old_kept(_dicts(record), record.status.value)}
            if self._old_list_entry(run_id) != self._old_requirements(written):
                self._diverge("delegation_state_is_a_key_patch", reason)
            self.used.add("delegation_state_is_a_key_patch")
        return None

    @override
    def save_interrupted(
        self,
        session: ConversationSession,
        run_id: str,
        snapshot: InterruptedReplaySnapshot,
        state: dict[str, object] | None,
    ) -> object:
        before = self._old_list_entry(run_id)
        covered = self._covered(run_id)
        session_row = partial(
            self._query,
            f"SELECT updated_at, session_state FROM {quote_identifier(self.names.sessions)} WHERE session_id = ?",  # noqa: S608
            (SESSION_ID,),
        )
        row_before = session_row()
        retained = {tool.tool_call_id for tool in (*snapshot.completed_tools, *snapshot.interrupted_tools)}
        old_folded = retained & self._old_folded_calls(run_id)
        v2_folded = retained & folded_tool_call_ids(self.store, session_id=session.session_id, run_id=run_id)
        record, outcome = self._save_interrupted(session, run_id, snapshot, state)
        if outcome != "written":
            return None
        row_after = session_row()
        if row_before and row_after[0][1] == row_before[0][1] and row_after[0][0] != row_before[0][0]:
            # The old store moved the update time only when the record's state changed the row, so it kept the time it
            # had before this save, or before an earlier one this mask already undid.
            moved_by_news = self.news_at is not None and self.news_at[0] == row_before[0][0]
            self.news_at = (row_after[0][0], self.news_at[1] if self.news_at and moved_by_news else row_before[0][0])
        old_messages = self.old_messages.get(run_id)
        self._wrote(record, RunStatus.completed.value)
        if covered and before is not None:
            # The old store refused it as frozen; the v2 store replaced the covered run in place.
            stored = self.store.get_run(run_id)
            assert stored is not None
            assert stored.content == record.content
            self.replaced_covered.setdefault(run_id, before)
            if old_messages is not None:
                self.old_messages[run_id] = old_messages
            self.used.add("interrupted_record_replaces_covered_run")
        elif old_folded != v2_folded:
            self._diverge(
                "folded_calls_go_with_their_run",
                f"the Agno-layout store left {run_id}'s folded calls out of its interrupted record",
            )
        return None

    @override
    def discard_run(self, session: ConversationSession, run_id: str) -> object:
        deleted = super().discard_run(session, run_id)
        assert isinstance(deleted, list)
        # The old store reported the ids it was asked to delete too.
        old = sorted({*deleted, run_id})
        if old != deleted:
            self.used.add("outcome_names")
        return old

    @override
    def append_fold(self, session: ConversationSession, basis: FoldBasisView, summary: str, fold: FoldInput) -> bool:
        run_id = fold.record.run_id
        had_row = run_id in self._rows()
        appended = super().append_fold(session, basis, summary, fold)
        if appended and not had_row:
            stored = self.store.get_run(run_id)
            assert stored is not None
            assert stored.status is RunStatus.cancelled
            assert (stored.messages, stored.tools) == ([], None)
            assert stored.metadata == fold.record.metadata
            self.fold_rows.add(run_id)
            self.used.add("fold_stores_cancelled_row")
        return appended

    def _cutting(self) -> _Cut:
        """Read what a cut may change, before it runs."""
        rows = self._rows()
        members = {
            run_id: entry
            for run_id, row in rows.items()
            if row.parent_run_id in self.fold_rows and (entry := self._old_list_entry(run_id)) is not None
        }
        copy = None
        if self.fold_rows:
            copy = self.names.database_path(self.root / "without-fold-rows")
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.unlink(missing_ok=True)
            with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(copy)) as target:
                source.backup(target)
        return _Cut(rows, members, copy, frozenset(self.fold_rows))

    def _cut(self, cut: Callable[[ConversationStore], object]) -> object:
        """Run ``cut`` on the v2 store, keep what the old store kept of it, and end the comparison where they differ.

        The members a cut deleted with a fold-created row stay in the old store, which had no row of their team run.
        A cut that deleted a fold-created row, run again without the rows only folds created, must leave the same
        summary rows, since the old store had no such row to pull a fold's summary into the cut. A fold-placed run
        a cut kept, the old store, placing it later, removed.
        """
        before = self._cutting()
        result = cut(self.store)
        after = self._rows()
        for run_id, entry in before.members.items():
            if run_id not in after:
                # The old store had no row of their team run, so the cut left them, never loading again.
                self.orphans[run_id] = entry
                self.used.add("fold_stores_cancelled_row")
        if before.copy is not None and any(run_id not in after for run_id in before.fold_rows):
            with closing(sqlite3.connect(before.copy)) as connection, connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    f"DELETE FROM {quote_identifier(self.names.log_runs)} WHERE run_id IN (SELECT value FROM json_each(?))",  # noqa: S608
                    (json.dumps(sorted(before.fold_rows)),),
                )
            with self.determinism.ids_repeated():
                cut(ConversationStore(before.copy))
            if self._summary_ids(before.copy) != self._summary_ids(self.store.path):
                self._diverge(
                    "fold_stores_cancelled_row",
                    "a cut took a fold's summary through the cancelled row the fold stored, which the old store lacked",
                )
        deleted = [row.seq for run_id, row in before.rows.items() if run_id not in after and row.parent_run_id is None]
        for run_id, place in self.placed.items():
            row = after.get(run_id)
            if row is not None and any(row.seq < seq < place for seq in deleted):
                self._diverge(
                    "first_stored_by_fold_sorts_at_fold",
                    f"a cut after {run_id}'s fold kept it; the Agno-layout store had placed it after the cut",
                )
        return result

    def _summary_ids(self, database: Path) -> set[int]:
        with closing(sqlite3.connect(database)) as connection:
            return {
                row_id
                for (row_id,) in connection.execute(f"SELECT id FROM {quote_identifier(self.names.log_summaries)}")  # noqa: S608
            }

    @override
    def cut_edit(self, event_id: str) -> object:
        return self._cut(partial(edit_cut, scope=self.scope, event_id=event_id))

    @override
    def redaction_cleanup(self, redacted: set[str]) -> object:
        removed = self._cut(partial(redaction_cut, redacted=redacted))
        assert isinstance(removed, list)
        if removed and self.metadata_rewritten_at is None and self.store.read_session_metadata(SESSION_ID) is None:
            self.metadata_rewritten_at = self.determinism.now
        if any(f"$reply-{run_id}" in removed for run_id in self.link_only):
            self._diverge("link_reaches_any_stored_run", "a redaction of a reply only the v2 store linked cut history")
        return removed

    # Observables as the old store showed them.

    def _old_folded_calls(self, run_id: str) -> frozenset[str]:
        """Return the calls the old store's segments of ``run_id`` named, from the fold rows that still exist."""
        rows = self._fold_row_ids()
        return frozenset().union(
            *(
                _segment_call_ids(fold)
                for row_id, fold in self.folds.items()
                if fold.record.run_id == run_id and row_id in rows
            ),
        )

    def _old_run(self, data: dict[str, Any], *, loaded: bool) -> dict[str, Any]:
        """Return one run dict the v2 store shows as the old store showed it, after checking its v2 messages."""
        run_id = data["run_id"]
        folded = self.folded.get(run_id, set())
        expected = self.expected_view(run_id)
        assert expected is None or data["messages"] == expected, f"v2 messages of {run_id}: {data['messages']}"
        data = deepcopy(self.replaced_covered.get(run_id, data))
        if run_id in self.old_messages:
            held = self.old_messages[run_id]
            if not loaded and any(message["id"] in folded for message in held):
                self.used.add("stored_copy_hides_folded_messages")
            data["messages"] = [message for message in held if not (loaded and message["id"] in folded)]
        metadata = data.get("metadata")
        if run_id in self.link_only and isinstance(metadata, dict):
            metadata.pop(MATRIX_RESPONSE_EVENT_ID_METADATA_KEY, None)
        if run_id in self.delegation_only and isinstance(metadata, dict):
            old_key = self.delegation_only[run_id]
            if old_key is _MISSING:
                metadata.pop(DELEGATION_STATE_KEY, None)
            else:
                metadata[DELEGATION_STATE_KEY] = old_key
        return self._old_requirements(data)

    def _old_requirements(self, data: dict[str, Any]) -> dict[str, Any]:
        """Give run dict ``data`` the requirements the old store kept beside its tools, and no pause ids; return it.

        The driver's names give them: an agent run's latest gated call, as ``<call id>.req``; a member run's one gated
        call, as ``<run id>.req``; and a team run that paused on a member, a copy of that member's call naming the
        member, approved once the team run no longer waits. The old store's delegation state listed its pending
        calls a second time, and the driver's states list none.
        """
        state = (data.get("metadata") or {}).get(DELEGATION_STATE_KEY)
        if isinstance(state, dict) and "pending_requirements" not in state:
            assert state["pending_tools"] == []
            state["pending_requirements"] = []
            self.used.add("paused_calls_stored_once")
        for member in data.get("member_responses") or []:
            self._old_requirements(member)
        tools = data.get("tools") or []
        for tool in tools:
            if tool.pop("pause_id", None) is not None:
                self.used.add("paused_calls_stored_once")
        gated = [tool for tool in tools if tool.get("requires_confirmation")]
        run_id = data["run_id"]
        member_id = self.paused_members.get(run_id) if "team_id" in data else None
        if member_id is not None:
            call = {"tool_call_id": f"{member_id}.q", "tool_name": "dangerous", "tool_args": {}}
            call["requires_confirmation"] = True
            agent_id = self.records[member_id].agent_id if member_id in self.records else None
            entry: dict[str, Any] = {
                "id": f"{member_id}.req",
                "member_agent_id": agent_id,
                "member_agent_name": agent_id,
                "member_run_id": member_id,
                "tool_execution": call,
            }
            if data["status"] != RunStatus.paused.value:
                call.update(confirmed=True, result="approved")
                entry["confirmation"] = True
        elif gated and "team_id" not in data:
            latest = gated[-1]
            entry = {"id": f"{run_id if data.get('parent_run_id') else latest['tool_call_id']}.req"}
            entry["tool_execution"] = deepcopy(latest)
            if latest.get("confirmed"):
                entry["confirmation"] = True
        else:
            return data
        data["requirements"] = [entry]
        self.used.add("paused_calls_stored_once")
        return data

    def _old_list_entry(self, run_id: str) -> dict[str, Any] | None:
        stored = self.store.get_run(run_id)
        if stored is None or run_id in self.fold_rows:
            return None
        return self._old_run(stored.to_dict(), loaded=False)

    def _hidden(self, data: dict[str, Any]) -> bool:
        """Whether the old store had no row for this run: a fold created it, or it is a member of one."""
        return data["run_id"] in self.fold_rows or data.get("parent_run_id") in self.fold_rows

    def _old_view_runs(self, runs: Sequence[AnyRunRecord]) -> list[AnyRunRecord]:
        """Return a loaded session's runs as the old store loaded them, as records a writer edits."""
        return [
            replace(
                run,
                messages=[Message.from_dict(message) for message in old["messages"]],
                metadata=old.get("metadata"),
            )
            for run in runs
            if not self._hidden(run.to_dict())
            for old in (self._old_run(run.to_dict(), loaded=True),)
        ]

    def _seqs(self) -> dict[str, float]:
        return {run_id: self.placed.get(run_id, row.seq) for run_id, row in self._rows().items()}

    def old_view(self, observed: dict[str, Any]) -> dict[str, Any]:
        """Return the observables the v2 store shows as the Agno-layout store showed them under every named change."""
        view = deepcopy(observed)
        if view["row"] is None:
            assert view["ledger"] == []
            view["row"] = {"error": f"OperationalError: no such table: {self.names.sessions}"}
            view["ledger"] = {"error": f"OperationalError: no such table: {self.names.usage}"}
            self.used.add("tables_exist_at_open")
        readers_in_creation_order(view, self.used)
        self._old_resume_requests(view, self._old_requests(view, observed))
        if isinstance(view["loaded_runs"], list):
            view["loaded_runs"] = self._old_loaded(view["loaded_runs"])
        if isinstance(view["list_runs"], list):
            view["list_runs"] = self._old_listed(view["list_runs"])
            if self.replaced_covered:
                runs = [_decoded(run) for run in view["list_runs"]]
                view["session_runs"] = _old_reader_view(runs)
        replies = {f"$reply-{run_id}" for run_id in self.link_only}
        for key in ("seen_event_ids", "redaction_candidates"):
            if isinstance(view[key], list):
                view[key] = [event_id for event_id in view[key] if event_id not in replies]
        if isinstance(view["folded_tool_call_ids"], dict):
            view["folded_tool_call_ids"] = self._old_folded_view(view["folded_tool_call_ids"])
        if untag_summary_requests(view["ledger"]):
            self.used.add("summary_requests_name_their_model")
        self._old_pending_view(view)
        self._old_rewritten_metadata(view)
        self._old_history_metadata(view)
        self._old_session_totals(view)
        row = view["row"]
        if self.news_at is not None and isinstance(row, dict) and row.get("updated_at") == self.news_at[0]:
            row["updated_at"] = self.news_at[1]
            self.used.add("a_finished_interrupted_record_is_news")
        return view

    @override
    def observe(self) -> dict[str, object]:
        """Observe the v2 store, and build the requests the old store's runs built where its notice rewrites set them apart.

        Those runs replay as the old store held them, without the history copies it kept, which no request replayed.
        The old store's requests are built with the ids the observed ones were given, as one observation of it would.
        """
        self.held_requests = None
        session = self.load()
        if session is None:
            return super().observe()
        old = [
            replace(run, messages=[message for message in run.messages if not message.from_history])
            for run in self._old_view_runs(session.runs)
        ]
        if [_dicts(run) for run in old] == [_dicts(run) for run in session.runs if not self._hidden(run.to_dict())]:
            return super().observe()
        with self.determinism.ids_repeated():
            observed = super().observe()
        held = replace(session, runs=old)
        self.held_requests = (
            probe_request(held, self.scope),
            resume_requests(held, self.scope),
            [run.to_dict() for run in old],
        )
        return observed

    def _old_requests(self, view: dict[str, Any], observed: dict[str, Any]) -> object:
        """Show the requests the old store's runs built, when its notice rewrites set them apart; return the loaded runs.

        The loaded runs are those whose paused messages the resume requests repeat.
        """
        if self.held_requests is None:
            return observed["loaded_runs"]
        view["request"], view["resume_requests"], loaded = deepcopy(self.held_requests)
        self.used.add("queued_notices_stored_once")
        return loaded

    def _old_resume_requests(self, view: dict[str, Any], loaded: object) -> None:
        """Show each paused run's resume request with the queued-message notices its paused run holds, as it was sent.

        Each notice goes back before the paused message that follows it, so after a scope summary placed after the
        paused prompt, as the request held it.
        """
        requests = view["resume_requests"]
        if not (isinstance(requests, dict) and isinstance(loaded, list)):
            return
        for run in loaded:
            if run["run_id"] not in requests:
                continue
            request = requests[run["run_id"]]["request"]
            position = len(request)
            for message in reversed([message for message in run["messages"] if message["role"] != "system"]):
                if is_queued_notice_message(Message.from_dict(message)):
                    request.insert(position, message)
                    self.used.add("resumes_leave_out_the_paused_reply_s_notice")
                    continue
                position = next(index for index in range(position - 1, -1, -1) if request[index] == message)

    def _old_session_totals(self, view: dict[str, Any]) -> None:
        """Show the usage totals the Agno-layout store kept beside its ledger, the sum of the ledger's run rows."""
        row, totals = view["row"], _old_session_totals(view["ledger"])
        if totals is None or not isinstance(row, dict):
            return
        data = row["session_data"] if isinstance(row.get("session_data"), dict) else {}
        row["session_data"] = {**data, "session_metrics": totals}
        self.used.add("ledger_is_the_only_usage_owner")

    def _old_rewritten_metadata(self, view: dict[str, Any]) -> None:
        """Show the empty metadata a redaction left in the old store, and the update time its rewrite moved the row to."""
        if self.metadata_rewritten_at is None or not isinstance(view["session"], dict):
            return
        if empty_missing_metadata(view):
            self.used.add("preserved_seen_ids_not_read")
        self._old_update_time(view["row"], self.metadata_rewritten_at, "preserved_seen_ids_not_read")

    def _old_history_metadata(self, view: dict[str, Any]) -> None:
        """Show the owner's history state as the per-scope maps the Agno-layout store kept it in."""
        session = view["session"]
        metadata = session.get("metadata") if isinstance(session, dict) else None
        if not isinstance(metadata, dict) or MINDROOM_HISTORY_METADATA_KEY not in metadata:
            return
        state = metadata.pop(MINDROOM_HISTORY_METADATA_KEY)
        if state.get("force_compact_before_next_run"):
            states = {self.scope.key: {"force_compact_before_next_run": True}}
            metadata[LEGACY_COMPACTION_METADATA_KEY] = {"version": 2, "states": states}
        if state.get("seen_event_ids"):
            states = {self.scope.key: {"seen_event_ids": state["seen_event_ids"]}}
            metadata[LEGACY_MATRIX_HISTORY_METADATA_KEY] = {"version": 1, "states": states}
        self.used.add("history_state_is_the_owners_one_value")

    def _old_pending_view(self, view: dict[str, Any]) -> None:
        """Show the request the old store queued in the stored state, and the update time only its writes made."""
        session, row = view["session"], view["row"]
        if self.pending_stored:
            view["force_queued"] = True
            for state in (session.get("session_state"), row.get("session_data", {}).get("session_state")):
                state[_PENDING_KEY] = [self.scope.key]
            self.used.add("force_request_only_in_metadata")
        self._old_update_time(row, self.pending_written_at, "force_request_only_in_metadata")

    def _old_update_time(self, row: dict[str, Any], at: int | None, mask: str) -> None:
        """Show the update time ``at`` a write only the old store made, unless a later write moved the row further."""
        if at is not None and row.get("updated_at", at) < at:
            row["updated_at"] = at
            self.used.add(mask)

    def _old_loaded(self, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = self._rows()
        ids = [run["run_id"] for run in runs]
        assert ids == sorted(ids, key=lambda run_id: rows[run_id].seq), f"the v2 load is not in seq order: {ids}"
        loaded = [self._old_run(run, loaded=True) for run in runs if not self._hidden(run)]
        if len(loaded) < len(runs):
            self.used.add("fold_stores_cancelled_row")
        seqs = self._seqs()
        placed = sorted(loaded, key=lambda run: seqs[run["run_id"]])
        if placed != loaded:
            self.used.add("first_stored_by_fold_sorts_at_fold")
        return placed

    def _old_listed(self, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        listed = [self._old_run(run, loaded=False) for run in runs if run["run_id"] not in self.fold_rows]
        listed += [deepcopy(orphan) for orphan in self.orphans.values()]
        return sorted(listed, key=lambda run: (run["created_at"], run["run_id"]))

    def _old_folded_view(self, folded: dict[str, list[str]]) -> dict[str, list[str]]:
        rows = self._rows()
        for run_id in self.known_run_ids():
            old_calls = self._old_folded_calls(run_id)
            if run_id not in rows and old_calls:
                folded[run_id] = sorted(old_calls)
                self.used.add("folded_calls_go_with_their_run")
        return dict(sorted(folded.items()))


__all__ = ["MASKS", "Diverged", "MaskedBackend", "untag_summary_requests"]
