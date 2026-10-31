"""Redaction undoes compaction precisely instead of discarding the whole scope."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom import constants
from mindroom.history.compaction import summarize_runs
from mindroom.history.storage import (
    _read_scope_history_event_ids,
    _remove_redacted_event_from_history,
    read_scope_seen_event_ids,
    remove_history_of_redacted_events,
    remove_runs_answering,
)
from mindroom.history.types import HistoryScope, ScopeSummary
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.native_compaction import replay_basis
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames
from mindroom.session_storage.legacy_scope_metadata import (
    LEGACY_COMPACTION_METADATA_KEY,
    LEGACY_MATRIX_HISTORY_METADATA_KEY,
)
from mindroom.session_storage.types import ConversationSession
from tests.conftest import seed_session
from tests.history_helpers import (
    _summarizer,
    checkpointed_answer,
    compact_runs,
    folded_run_ids,
    stored_run_ids,
    summary_rows_of,
)
from tests.session_storage.agno_layout import write_agno_database

if TYPE_CHECKING:
    from collections.abc import Callable, Collection
    from pathlib import Path

    from mindroom.session_storage.store import ConversationStore

_SCOPE = HistoryScope(kind="agent", scope_id="code")
_EPOCH = datetime.fromtimestamp(0, UTC)


def _open(tmp_path: Path) -> ConversationStore:
    return create_agent_state_storage("code", tmp_path)


@pytest.fixture
def storage(tmp_path: Path) -> ConversationStore:
    """Use the production conversation storage owner."""
    return _open(tmp_path)


@pytest.fixture
def open_legacy(tmp_path: Path) -> Callable[[ConversationSession], ConversationStore]:
    """Write a session as a pre-archive release left it, then open it, which converts it and adopts its state."""

    def open_migrated(session: ConversationSession) -> ConversationStore:
        write_agno_database(AgnoTableNames("code").database_path(tmp_path), session, usage=False)
        storage = _open(tmp_path)
        assert _summary(_stored(storage)) == _summary(session)
        return storage

    return open_migrated


def _run(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id="code",
        session_id="session",
        content=f"answer {run_id}",
        messages=[Message(role="user", content=f"question {run_id}")],
        metadata={
            constants.MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}",
            constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}"],
        },
    )


def _agent_session(runs: list[RunRecord]) -> ConversationSession:
    return ConversationSession(session_id="session", kind="agent", owner_id="code", runs=list(runs))


def _seed(
    storage: ConversationStore,
    run_ids: list[str],
    *,
    seen_event_ids: list[str] | None = None,
) -> ConversationSession:
    """Seed one run per id; ``seen_event_ids`` become the owner's seen ids earlier releases preserved in metadata."""
    session = _agent_session([_run(run_id) for run_id in run_ids])
    if seen_event_ids is not None:
        session.metadata = {constants.MINDROOM_HISTORY_METADATA_KEY: {"seen_event_ids": seen_event_ids}}
    return seed_session(storage, session)


def _stored(storage: ConversationStore) -> ConversationSession:
    session = storage.load_session("session")
    assert session is not None
    return session


def _summary(session: ConversationSession) -> str | None:
    return session.summary.text if session.summary is not None else None


def _redact(storage: ConversationStore, session: ConversationSession, event_id: str) -> bool:
    return _remove_redacted_event_from_history(storage, session=session, event_id=event_id)


def _legacy_session(run_ids: list[str], summary: str, seen_event_ids: list[str]) -> ConversationSession:
    session = _agent_session([_run(run_id) for run_id in run_ids])
    session.summary = ScopeSummary(text=summary, updated_at=_EPOCH)
    # The destructive compactor's state, as the Agno-layout database the conversion adopts it from held it.
    session.metadata = {
        LEGACY_COMPACTION_METADATA_KEY: {"version": 2, "states": {_SCOPE.key: {"compacted_run_ids": ["legacy-gone"]}}},
        **(
            {
                LEGACY_MATRIX_HISTORY_METADATA_KEY: {
                    "version": 1,
                    "states": {_SCOPE.key: {"seen_event_ids": seen_event_ids}},
                },
            }
            if seen_event_ids
            else {}
        ),
    }
    return session


def test_a_compacted_run_counts_as_seen_what_it_counted_while_loaded(storage: ConversationStore) -> None:
    """Compaction changes where seen ids come from, not which ids a run marks seen; redaction still matches all.

    Compacted runs count as seen through their summary row, so session metadata keeps no copy to repair.
    """
    run = _run("r1")
    assert run.metadata is not None
    run.metadata[constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY] = ["$coalesced"]
    session = seed_session(storage, _agent_session([run, _run("r2")]))
    seen_while_loaded = read_scope_seen_event_ids(storage, _stored(storage))
    compact_runs(storage, session, ["r1"], "summary of r1")

    assert constants.MINDROOM_HISTORY_METADATA_KEY not in (_stored(storage).metadata or {})
    assert read_scope_seen_event_ids(storage, _stored(storage)) == seen_while_loaded == {"$r1", "$r2"}
    assert _redact(storage, _stored(storage), "$coalesced")
    assert _summary(_stored(storage)) is None


def test_redaction_restores_the_runs_compacted_before_the_redacted_one(storage: ConversationStore) -> None:
    """Only the redacted run and what follows it leave history; earlier folded runs return to replay."""
    session = _seed(storage, ["r1", "r2", "r3", "r4", "r5"])
    compact_runs(storage, session, ["r1"], "summary of r1")
    compact_runs(storage, session, ["r2", "r3"], "summary of r1 to r3")

    assert _redact(storage, session, "$r3") is True

    stored = _stored(storage)
    assert [run.run_id for run in stored.runs] == ["r2"]
    assert _summary(stored) == "summary of r1"
    assert stored_run_ids(storage, "session") == ["r1", "r2"]
    assert read_scope_seen_event_ids(storage, stored) == {"$r1", "$r2"}
    assert _summary(session) == "summary of r1"


def test_redacting_the_first_compacted_run_clears_the_summary_for_good(storage: ConversationStore) -> None:
    """A cut past every summary row leaves no summary."""
    session = _seed(storage, ["r1", "r2"])
    compact_runs(storage, session, ["r1"], "summary with the redacted content")

    assert _redact(storage, session, "$r1") is True
    stored = _stored(storage)
    assert stored.runs == []
    assert stored.summary is None


def test_redacting_a_loaded_only_event_keeps_the_summary(storage: ConversationStore) -> None:
    """A summary provably built without the event survives its redaction."""
    session = _seed(storage, ["r1", "r2"], seen_event_ids=["$team-consumed"])
    compact_runs(storage, session, ["r1"], "summary of r1")

    assert _redact(storage, session, "$r2")

    stored = _stored(storage)
    assert _summary(stored) == "summary of r1"
    assert stored.runs == []
    # Preserved ids are dropped; the summary rows and loaded runs still supply everything replay represents.
    assert read_scope_seen_event_ids(storage, stored) == {"$r1"}


def test_compacted_history_that_read_a_redacted_event_is_found_and_removed(storage: ConversationStore) -> None:
    """A message a compacted run read only as context still marks that history, and its removal syncs the session."""
    run = _run("r1")
    assert run.metadata is not None
    run.metadata[constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY] = ["$context", "$r1"]
    session = seed_session(storage, _agent_session([run, _run("r2")]))
    compact_runs(storage, session, ["r1"], "summary that quotes the context")
    loaded = _stored(storage)

    assert "$context" in _read_scope_history_event_ids(storage, loaded)
    assert _redact(storage, loaded, "$context") is True

    assert loaded.runs == []
    assert loaded.summary is None
    assert "$context" not in _read_scope_history_event_ids(storage, _stored(storage))


def test_history_event_ids_include_compacted_sources_and_paused_runs(storage: ConversationStore) -> None:
    """Redaction finds a coalesced source compaction folded and a run paused for approval."""
    coalesced = _run("r1")
    assert coalesced.metadata is not None
    coalesced.metadata[constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY] = ["$coalesced"]
    paused = _run("r3")
    paused.status = RunStatus.paused
    session = seed_session(
        storage,
        _agent_session([coalesced, _run("r2"), paused]),
    )
    compact_runs(storage, session, ["r1"], "summary of r1")

    event_ids = _read_scope_history_event_ids(storage, _stored(storage))

    assert {"$coalesced", "$r1", "$r2", "$r3"} <= event_ids
    assert "$coalesced" not in read_scope_seen_event_ids(storage, _stored(storage))


def test_redaction_without_any_matching_history_changes_nothing(storage: ConversationStore) -> None:
    """An event no stored history represents leaves compaction untouched."""
    session = _seed(storage, ["r1", "r2"])
    compact_runs(storage, session, ["r1"], "summary of r1")

    assert _redact(storage, session, "$other") is False
    assert _summary(_stored(storage)) == "summary of r1"


def test_redacting_legacy_provenance_clears_summary_and_folded_runs(
    open_legacy: Callable[[ConversationSession], ConversationStore],
) -> None:
    """Content-free legacy history cannot be split, so an event it consumed clears the scope."""
    storage = open_legacy(_legacy_session(["r2", "r3"], "legacy summary", ["$legacy"]))
    session = _stored(storage)
    compact_runs(storage, session, ["r2"], "legacy summary and r2")

    assert _redact(storage, session, "$legacy") is True

    stored = _stored(storage)
    assert stored.runs == []
    assert stored.summary is None
    assert read_scope_seen_event_ids(storage, stored) == set()
    assert stored_run_ids(storage, "session") == []
    assert summary_rows_of(storage, _SCOPE.key, "session") == []


def test_removing_a_loaded_run_retires_a_legacy_summary(
    open_legacy: Callable[[ConversationSession], ConversationStore],
) -> None:
    """Legacy summaries lack complete provenance, so removing a loaded run for any event retires them."""
    storage = open_legacy(_legacy_session(["r2", "r3"], "legacy summary", []))
    session = _stored(storage)

    assert _redact(storage, session, "$r3")

    stored = _stored(storage)
    assert [run.run_id for run in stored.runs] == ["r2"]
    assert stored.summary is None


@pytest.mark.asyncio
async def test_a_retired_legacy_summary_does_not_return_through_a_kept_checkpoint(
    open_legacy: Callable[[ConversationSession], ConversationStore],
) -> None:
    """A checkpoint built on the legacy summary stops standing in for history once retiring keeps its run."""
    legacy = _legacy_session(["r1", "r2", "r3"], "RETIRED LEGACY CONTENT", [])
    legacy.runs[1].messages.append(
        checkpointed_answer(
            {"type": "compaction", "content": "RETIRED LEGACY CONTENT and r1"},
            replay_basis("RETIRED LEGACY CONTENT", legacy.runs[:1]),
        ),
    )
    storage = open_legacy(legacy)

    assert _redact(storage, _stored(storage), "$r3")

    kept = _stored(storage)
    assert [run.run_id for run in kept.runs] == ["r1", "r2"]
    assert kept.summary is None
    summary_mock = AsyncMock(return_value="result")
    with patch("mindroom.history.compaction.generate_compaction_summary", new=summary_mock):
        await summarize_runs(_summarizer(), None, kept.runs)
    assert "RETIRED LEGACY CONTENT" not in summary_mock.await_args.kwargs["summary_input"]


def test_removing_a_loaded_run_keeps_runs_folded_after_a_legacy_summary(
    open_legacy: Callable[[ConversationSession], ConversationStore],
) -> None:
    """Retiring a legacy summary keeps later folded runs stored, but no longer counts or replays them."""
    storage = open_legacy(_legacy_session(["r1", "r2", "r3"], "legacy summary", ["$legacy"]))
    session = _stored(storage)
    compact_runs(storage, session, ["r1"], "legacy summary and r1")

    assert _redact(storage, session, "$r3")

    stored = _stored(storage)
    assert [run.run_id for run in stored.runs] == ["r2"]
    assert stored.summary is None
    assert "r1" in stored_run_ids(storage, "session")
    assert read_scope_seen_event_ids(storage, stored) == {"$r2"}


def test_retirement_survives_a_later_cut(open_legacy: Callable[[ConversationSession], ConversationStore]) -> None:
    """A later cut before the retirement barrier brings back neither the retired summaries nor the runs behind it."""
    cancelled = _run("c1")
    cancelled.status = RunStatus.cancelled
    assert cancelled.metadata is not None
    cancelled.metadata[constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY] = ["$c1", "$late-redaction"]
    legacy = _legacy_session(["r1", "r2", "r3"], "legacy summary", ["$legacy"])
    first, *rest = legacy.runs
    legacy.runs = [first, cancelled, *rest]
    storage = open_legacy(legacy)
    session = _stored(storage)
    compact_runs(storage, session, ["r1"], "legacy summary and r1")
    assert _redact(storage, session, "$r3")

    # The cancelled run sorts after r1 and before the barrier the retirement appended.
    assert _redact(storage, _stored(storage), "$late-redaction")

    stored = _stored(storage)
    assert stored.summary is None
    assert stored.runs == []
    assert "r1" in stored_run_ids(storage, "session")


def test_removing_only_folded_runs_retires_a_legacy_summary(
    open_legacy: Callable[[ConversationSession], ConversationStore],
) -> None:
    """A legacy summary may hold the event without listing it, so a cut of folded runs alone retires it too."""
    storage = open_legacy(_legacy_session(["r1", "r2", "r3"], "legacy summary", ["$legacy"]))
    session = _stored(storage)
    compact_runs(storage, session, ["r1", "r2"], "legacy summary and r1 to r2")

    assert _redact(storage, session, "$r2")

    stored = _stored(storage)
    assert stored.summary is None
    assert [run.run_id for run in stored.runs] == ["r1"]


def test_a_late_save_of_a_redacted_run_cannot_replay_it(storage: ConversationStore) -> None:
    """A stale copy of a run redaction removed is never written back."""
    session = _seed(storage, ["r1", "r2"])
    late_copy = next(run for run in session.runs if run.run_id == "r1")
    compact_runs(storage, session, ["r1"], "summary of r1")
    assert _redact(storage, session, "$r1")

    assert storage.save_run(session, late_copy, record_usage=False) == "removed"

    assert _stored(storage).runs == []
    assert stored_run_ids(storage, "session") == []


def test_a_late_save_after_a_legacy_clear_cannot_replay_the_cleared_run(
    open_legacy: Callable[[ConversationSession], ConversationStore],
) -> None:
    """Runs folded after a legacy summary stay gone when that summary is cleared."""
    storage = open_legacy(_legacy_session(["r1", "r2"], "legacy summary", ["$legacy"]))
    session = _stored(storage)
    late_copy = next(run for run in session.runs if run.run_id == "r1")
    compact_runs(storage, session, ["r1"], "legacy summary and r1")
    assert _redact(storage, session, "$legacy")

    assert storage.save_run(session, late_copy, record_usage=False) == "removed"

    assert _stored(storage).runs == []


@pytest.mark.asyncio
async def test_redaction_cleans_the_agent_runs_of_a_conversation_started_under_its_display_name_id(
    tmp_path: Path,
) -> None:
    """Cleanup takes the scope from the session, which the agent's store open makes name the agent.

    Releases before v2026.2.55 let Agno derive an agent's id from its display name, and a row keeps the owner it was
    created with, so a conversation started then named ``code-agent`` while the runs it gets now belong to ``code``.
    """
    old_run = replace(_run("old"), agent_id="code-agent")
    old_session = ConversationSession(session_id="session", kind="agent", owner_id="code-agent", runs=[old_run])
    write_agno_database(AgnoTableNames("code").database_path(tmp_path), old_session, usage=False)
    storage = _open(tmp_path)
    storage.save_run(_stored(storage), _run("new"), record_usage=False)
    session = _stored(storage)
    assert session.owner_id == "code"

    async def redacted_history_events(event_ids: Collection[str]) -> dict[str, str | None]:
        return dict.fromkeys(set(event_ids) & {"$new"})

    assert await remove_history_of_redacted_events(storage, session, redacted_history_events) == ["$new"]
    assert [run.run_id for run in _stored(storage).runs] == ["old"]


def test_legacy_seen_ids_without_summary_keep_unrelated_runs(storage: ConversationStore) -> None:
    """Seen ids earlier releases preserved on the session row are not legacy summary provenance."""
    session = _seed(storage, ["t1", "t2", "t3"], seen_event_ids=["$t1", "$t2", "$t3"])

    assert _redact(storage, session, "$t3")

    stored = _stored(storage)
    assert [run.run_id for run in stored.runs] == ["t1", "t2"]
    assert read_scope_seen_event_ids(storage, stored) == {"$t1", "$t2"}


def test_an_edit_cut_forgets_seen_ids_only_the_removed_run_read(storage: ConversationStore) -> None:
    """Messages only an edited turn's removed run had read become unseen, so its regenerated reply reads them.

    An edit of a message no run answered removes nothing and keeps the ids.
    """
    session = _seed(storage, ["t1", "t2"], seen_event_ids=["$t1", "$t2", "$context-of-t2"])
    assert not remove_runs_answering(storage, session.session_id, _SCOPE, "$context-of-t2")
    assert read_scope_seen_event_ids(storage, _stored(storage)) == {"$t1", "$t2", "$context-of-t2"}

    assert remove_runs_answering(storage, session.session_id, _SCOPE, "$t2")

    stored = _stored(storage)
    assert [run.run_id for run in stored.runs] == ["t1"]
    assert read_scope_seen_event_ids(storage, stored) == {"$t1"}


def test_rollback_forgets_seen_ids_of_the_removed_runs(storage: ConversationStore) -> None:
    """Messages whose runs a cut removed become unseen, so thread history supplies them again."""
    session = _seed(storage, ["r1", "r2", "r3"], seen_event_ids=["$r3"])
    compact_runs(storage, session, ["r1"], "summary of r1")
    compact_runs(storage, session, ["r2"], "summary of r1 and r2")

    assert _redact(storage, session, "$r2")

    stored = _stored(storage)
    assert stored.runs == []
    assert _summary(stored) == "summary of r1"
    assert read_scope_seen_event_ids(storage, stored) == {"$r1"}


def test_legacy_provenance_wins_over_a_later_folded_hit(
    open_legacy: Callable[[ConversationSession], ConversationStore],
) -> None:
    """An event a legacy summary may contain clears it even when a folded run also holds it."""
    storage = open_legacy(_legacy_session(["r1", "r2"], "legacy summary quoting $r1", ["$r1"]))
    session = _stored(storage)
    compact_runs(storage, session, ["r1"], "legacy summary quoting $r1, then r1")

    assert _redact(storage, session, "$r1")

    stored = _stored(storage)
    assert stored.summary is None
    assert stored.runs == []


def test_a_stale_copy_of_a_folded_run_stays_folded(storage: ConversationStore) -> None:
    """A late save of a folded run is refused, so it neither replays again nor hides its first summary."""
    session = _seed(storage, ["r1", "r2"])
    stale = next(run for run in session.runs if run.run_id == "r1")
    compact_runs(storage, session, ["r1"], "summary mentioning r1")
    assert storage.save_run(session, stale, record_usage=False) == "final"
    assert folded_run_ids(storage, "session") == ["r1"]
    compact_runs(storage, _stored(storage), ["r2"], "summary mentioning r1 and r2")

    assert _redact(storage, _stored(storage), "$r1")

    stored = _stored(storage)
    assert stored.summary is None
    assert stored.runs == []
