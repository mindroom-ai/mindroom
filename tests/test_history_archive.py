"""The compaction archive moves runs between the live table and durable history atomically."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession

from mindroom.agent_storage import create_state_storage, get_agent_session
from mindroom.history import archive
from tests.conftest import seed_session
from tests.history_helpers import StoredGeneration, compaction_generations

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_SCOPE = "agent:code"


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[SqliteDb]:
    """Use the production conversation storage owner."""
    db = create_state_storage("code", tmp_path, subdir="sessions", session_table="code_sessions")
    assert isinstance(db, SqliteDb)
    try:
        yield db
    finally:
        db.close()


def _run(run_id: str, *, parent_run_id: str | None = None) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        session_id="session",
        agent_id="code",
        parent_run_id=parent_run_id,
        user_id="@alice:example.test",
        content=f"answer {run_id}",
        messages=[Message(role="user", content=f"question {run_id}"), Message(role="assistant", content=run_id)],
    )


def _seed(storage: SqliteDb, run_ids: list[str], *, children: dict[str, str] | None = None) -> AgentSession:
    runs = [_run(run_id) for run_id in run_ids]
    runs.extend(_run(child, parent_run_id=parent) for child, parent in (children or {}).items())
    return seed_session(storage, AgentSession(session_id="session", agent_id="code", runs=runs))


def _live_run_ids(storage: SqliteDb) -> list[str]:
    session = get_agent_session(storage, "session")
    assert session is not None
    return [run.run_id for run in session.runs or [] if run.run_id]


def _archive(storage: SqliteDb, session: AgentSession, run_ids: list[str], summary: str) -> None:
    runs = [run for run in session.runs or [] if run.run_id in run_ids]
    archive.archive_runs(
        storage,
        session_id="session",
        scope_key=_SCOPE,
        summary=summary,
        summary_model="summary-model",
        runs=runs,
        event_ids={run_id: {f"${run_id}"} for run_id in run_ids},
    )


def test_archiving_moves_runs_and_member_runs_out_of_the_live_table(storage: SqliteDb) -> None:
    """Archived runs leave replay but stay readable with their summary generation."""
    session = _seed(storage, ["r1", "r2", "r3"], children={"r1-member": "r1"})

    _archive(storage, session, ["r1-member", "r1", "r2"], "summary one")

    assert _live_run_ids(storage) == ["r3"]
    assert archive.archived_run_ids(storage, session_id="session", run_ids=["r1", "r1-member", "r2", "r3"]) == {
        "r1",
        "r1-member",
        "r2",
    }
    assert compaction_generations(storage, _SCOPE, "session") == [
        StoredGeneration(summary="summary one", summary_model="summary-model", legacy=False),
    ]


def test_archived_event_ids_are_scoped(storage: SqliteDb) -> None:
    """Seen ids come from the scope's own archive only."""
    session = _seed(storage, ["r1", "r2"])
    _archive(storage, session, ["r1"], "summary")
    archive.archive_runs(
        storage,
        session_id="session",
        scope_key="team:other",
        summary="other",
        summary_model="summary-model",
        runs=[run for run in session.runs or [] if run.run_id == "r2"],
        event_ids={"r2": {"$other"}},
    )

    assert archive.archived_event_ids(storage, session_id="session", scope_key=_SCOPE) == {"$r1"}
    assert archive.latest_generation(storage, session_id="session", scope_key="missing") is None


def test_roll_back_restores_earlier_runs_of_the_hit_generation_in_order(storage: SqliteDb) -> None:
    """Rolling back keeps the prior generation and restores the runs archived before the hit."""
    session = _seed(storage, ["r1", "r2", "r3", "r4", "r5", "r6"], children={"r4-member": "r4"})
    _archive(storage, session, ["r1"], "generation one")
    _archive(storage, session, ["r2", "r3", "r4-member", "r4"], "generation two")
    _archive(storage, session, ["r5"], "generation three")

    hit = archive.find_archived_event(storage, session_id="session", scope_key=_SCOPE, event_id="$r4")
    assert hit is not None
    archive.roll_back_to(storage, session_id="session", scope_key=_SCOPE, hit=hit, live_run_ids=["r6"])

    assert _live_run_ids(storage) == ["r2", "r3"]
    restored = get_agent_session(storage, "session")
    assert restored is not None
    assert [message.content for message in restored.runs[0].messages or []] == ["question r2", "r2"]
    generation = archive.latest_generation(storage, session_id="session", scope_key=_SCOPE)
    assert generation is not None
    assert generation.summary == "generation one"
    assert archive.archived_run_ids(
        storage,
        session_id="session",
        run_ids=["r1", "r2", "r3", "r4", "r4-member", "r5"],
    ) == {"r1"}


def test_clear_to_legacy_keeps_only_content_free_tombstones(storage: SqliteDb) -> None:
    """Legacy invalidation drops archived content and live runs but keeps legacy tombstones."""
    session = _seed(storage, ["r2", "r3"])
    archive.record_legacy_generation(
        storage,
        session_id="session",
        scope_key=_SCOPE,
        summary="legacy summary",
        tombstone_run_ids=["gone"],
        event_ids=["$legacy"],
    )
    _archive(storage, session, ["r2"], "generation one")

    archive.clear_to_legacy(storage, session_id="session", scope_key=_SCOPE, live_run_ids=["r3"])

    assert _live_run_ids(storage) == []
    assert compaction_generations(storage, _SCOPE, "session") == [
        StoredGeneration(summary=None, summary_model=None, legacy=True),
    ]
    assert archive.legacy_event_ids(storage, session_id="session", scope_key=_SCOPE) == set()
    assert archive.archived_run_ids(storage, session_id="session", run_ids=["gone", "r2"]) == {"gone"}


def test_session_deletion_cascades_to_the_archive(storage: SqliteDb) -> None:
    """Deleting a conversation also erases its archived history."""
    session = _seed(storage, ["r1"])
    _archive(storage, session, ["r1"], "summary")

    storage.delete_session("session")
    _seed(storage, ["fresh"])

    assert archive.latest_generation(storage, session_id="session", scope_key=_SCOPE) is None
    assert archive.archived_run_ids(storage, session_id="session", run_ids=["r1"]) == set()


def test_failed_run_deletion_rolls_back_the_archive_write(storage: SqliteDb, monkeypatch: pytest.MonkeyPatch) -> None:
    """The archive insert and the live-row deletion commit together or not at all."""
    session = _seed(storage, ["r1"])

    def fail_deletion(*_args: object) -> None:
        msg = "process stopped"
        raise RuntimeError(msg)

    monkeypatch.setattr(archive, "delete_run_subtrees", fail_deletion)
    with pytest.raises(RuntimeError, match="process stopped"):
        _archive(storage, session, ["r1"], "summary")

    assert _live_run_ids(storage) == ["r1"]
    assert archive.latest_generation(storage, session_id="session", scope_key=_SCOPE) is None
    assert archive.archived_run_ids(storage, session_id="session", run_ids=["r1"]) == set()


def test_archive_requires_sqlite_storage() -> None:
    """Other storage types cannot hold the archive."""
    with pytest.raises(TypeError, match="SQLite"):
        archive.latest_generation(object(), session_id="session", scope_key=_SCOPE)  # type: ignore[arg-type]
