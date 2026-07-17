"""Tests for canonical turn ownership, precedence, and repair."""

from __future__ import annotations

import ast
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary

from mindroom import constants
from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.handled_turns import (
    SourceEventMetadata,
    TurnRecord,
    TurnRecordCodec,
    _reset_handled_turn_ledger_runtime,
)
from mindroom.history.storage import (
    read_scope_seen_event_ids,
    read_scope_state,
    update_scope_seen_event_ids,
    write_scope_state,
)
from mindroom.history.types import HistoryScope, HistoryScopeState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.turn_store import TurnStore, TurnStoreDeps
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, runtime_paths_for, test_runtime_paths


def _store(tmp_path: Path) -> TurnStore:
    return TurnStore(
        TurnStoreDeps(
            agent_name="agent",
            tracking_base_path=tmp_path,
            state_writer=MagicMock(),
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )


def _load_with_recovery(
    store: TurnStore,
    *,
    original_event_id: str,
    recovery_record: TurnRecord | None,
) -> TurnRecord | None:
    room = MagicMock(room_id="!room:example.org")
    with patch.object(store, "_load_persisted_turn_record", return_value=recovery_record):
        return store.load_turn(
            room=room,
            thread_id=None,
            original_event_id=original_event_id,
            requester_user_id="@user:example.org",
        )


@dataclass
class _FakeAgentStorage:
    session: AgentSession | None
    upserted_session: AgentSession | None = None

    def get_session(self, session_id: str, _session_type: object) -> AgentSession | None:
        if self.session is None or self.session.session_id != session_id:
            return None
        return self.session

    def upsert_session(self, session: AgentSession) -> None:
        self.upserted_session = session

    def close(self) -> None:
        return None


def _store_with_storage(tmp_path: Path, storage: _FakeAgentStorage) -> TurnStore:
    state_writer = MagicMock()
    state_writer.create_storage.return_value = storage
    state_writer.session_type_for_scope.return_value = SessionType.AGENT
    return TurnStore(
        TurnStoreDeps(
            agent_name="agent",
            tracking_base_path=tmp_path,
            state_writer=state_writer,
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )


def _owned_turn_record(target: MessageTarget) -> TurnRecord:
    return TurnRecord.create(
        ["$user_msg"],
        response_event_id="$reply",
        response_owner="agent",
        requester_id="@user:example.org",
        history_scope=HistoryScope(kind="agent", scope_id="agent"),
        conversation_target=target,
    )


def test_forget_redacted_turn_removes_persisted_run(tmp_path: Path) -> None:
    """Redacting a handled source message should delete that turn's persisted run."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    session = AgentSession(
        session_id=target.session_id,
        agent_id="agent",
        runs=[
            RunOutput(session_id=target.session_id, metadata={"matrix_event_id": "$user_msg"}),
            RunOutput(session_id=target.session_id, metadata={"matrix_event_id": "$other"}),
        ],
    )
    storage = _FakeAgentStorage(session)
    store = _store_with_storage(tmp_path, storage)
    store.record_turn(_owned_turn_record(target))

    removed = store.forget_redacted_turn(
        room=MagicMock(room_id="!room:example.org"),
        redacted_event_id="$user_msg",
        requester_user_id="@user:example.org",
        target_hint=target,
    )

    assert removed is True
    assert storage.upserted_session is session
    assert [run.metadata["matrix_event_id"] for run in session.runs or []] == ["$other"]


def test_forget_redacted_turn_ignores_unhandled_event(tmp_path: Path) -> None:
    """Redactions of events without a handled turn must not touch session storage."""
    store = _store(tmp_path)

    removed = store.forget_redacted_turn(
        room=MagicMock(room_id="!room:example.org"),
        redacted_event_id="$unknown",
        requester_user_id="@user:example.org",
        target_hint=None,
    )

    assert removed is False
    store.deps.state_writer.create_storage.assert_not_called()


def test_forget_redacted_turn_ignores_turn_owned_by_other_entity(tmp_path: Path) -> None:
    """Only the entity that owns the response removes its persisted runs."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    store = _store(tmp_path)
    store.record_turn(replace(_owned_turn_record(target), response_owner="other"))

    removed = store.forget_redacted_turn(
        room=MagicMock(room_id="!room:example.org"),
        redacted_event_id="$user_msg",
        requester_user_id="@user:example.org",
        target_hint=target,
    )

    assert removed is False
    store.deps.state_writer.create_storage.assert_not_called()


def test_forget_redacted_turn_recovers_missing_response_context(tmp_path: Path) -> None:
    """A ledger record without response context should repair from persisted run metadata."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    session = AgentSession(
        session_id=target.session_id,
        agent_id="agent",
        runs=[RunOutput(session_id=target.session_id, metadata={"matrix_event_id": "$user_msg"})],
    )
    storage = _FakeAgentStorage(session)
    store = _store_with_storage(tmp_path, storage)
    store.record_turn(TurnRecord.create(["$user_msg"], response_event_id="$reply"))

    recovered_record = replace(_owned_turn_record(target), requester_id="@metadata-user:example.org")
    with patch.object(store, "_load_persisted_turn_record", return_value=recovered_record):
        removed = store.forget_redacted_turn(
            room=MagicMock(room_id="!room:example.org"),
            redacted_event_id="$user_msg",
            requester_user_id="@source-user:example.org",
            target_hint=target,
        )

    assert removed is True
    assert session.runs == []
    store.deps.tool_runtime.build_execution_identity.assert_called_once_with(
        target=target,
        user_id="@source-user:example.org",
    )


def test_forget_redacted_turn_invalidates_compacted_replay(tmp_path: Path) -> None:
    """Redaction must remove content already folded into the durable summary."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    scope = HistoryScope(kind="agent", scope_id="agent")
    session = AgentSession(
        session_id=target.session_id,
        agent_id="agent",
        runs=[],
        summary=SessionSummary(summary="The user disclosed REDACTED_SECRET."),
    )
    update_scope_seen_event_ids(session, scope, ["$user_msg", "$older"])
    write_scope_state(
        session,
        scope,
        HistoryScopeState(last_summary_model="summary-model", compacted_run_ids=("$compacted-run",)),
    )
    storage = _FakeAgentStorage(session)
    store = _store_with_storage(tmp_path, storage)
    store.record_turn(_owned_turn_record(target))

    removed = store.forget_redacted_turn(
        room=MagicMock(room_id="!room:example.org"),
        redacted_event_id="$user_msg",
        requester_user_id="@user:example.org",
        target_hint=target,
    )

    assert removed is True
    assert storage.upserted_session is session
    assert session.summary is None
    assert read_scope_seen_event_ids(session, scope) == set()
    assert read_scope_state(session, scope) == HistoryScopeState()


def test_forget_redacted_turn_recovers_without_ledger_after_active_response(tmp_path: Path) -> None:
    """Post-lock cleanup can recover a run persisted before its ledger outcome was recorded."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    session = AgentSession(
        session_id=target.session_id,
        agent_id="agent",
        runs=[RunOutput(session_id=target.session_id, metadata={"matrix_event_id": "$user_msg"})],
    )
    storage = _FakeAgentStorage(session)
    store = _store_with_storage(tmp_path, storage)

    with patch.object(store, "_load_persisted_turn_record", return_value=_owned_turn_record(target)):
        removed = store.forget_redacted_turn(
            room=MagicMock(room_id="!room:example.org"),
            redacted_event_id="$user_msg",
            requester_user_id="@user:example.org",
            target_hint=target,
        )

    assert removed is True
    assert session.runs == []


def test_turn_store_constructs_private_ledger_from_tracking_base_path(tmp_path: Path) -> None:
    """TurnStore should own its private ledger and persist through the tracking base path."""
    store = _store(tmp_path)

    store.record_turn(TurnRecord.create(["$event"], response_event_id="$response"))

    reloaded_store = _store(tmp_path)

    assert reloaded_store.is_handled("$event")
    turn_record = reloaded_store.get_turn_record("$event")
    assert turn_record is not None
    assert turn_record.response_event_id == "$response"


def test_turn_record_codec_projects_and_parses_one_versioned_run_schema() -> None:
    """The same codec should own both run projection and recovery parsing."""
    history_scope = HistoryScope(kind="agent", scope_id="agent")
    target = MessageTarget.resolve("!room:example.org", "$thread", "$anchor")
    turn_record = TurnRecord.create(
        ["$first", "$anchor"],
        discovery_event_ids=["$selection"],
        response_event_id="$response",
        source_event_prompts={"$first": "first", "$anchor": "anchor"},
        source_event_metadata={
            "$first": SourceEventMetadata(sender="@alice:example.org", timestamp_ms=1_774_019_700_000),
        },
        response_owner="agent",
        requester_id="@user:example.org",
        correlation_id="corr-1",
        history_scope=history_scope,
        conversation_target=target,
    )

    metadata = TurnRecordCodec.to_run_metadata(turn_record)
    metadata.update(
        {
            constants.MATRIX_EVENT_ID_METADATA_KEY: "$anchor",
            constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: "$response",
            "requester_id": "@user:example.org",
            "correlation_id": "corr-1",
        },
    )
    parsed = TurnRecordCodec.from_run_metadata(metadata)

    assert metadata[constants.MATRIX_TURN_SCHEMA_VERSION_METADATA_KEY] == TurnRecordCodec.schema_version()
    assert metadata[constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY] == ["$selection"]
    assert parsed == turn_record


def test_build_run_metadata_normalizes_discovery_aliases(tmp_path: Path) -> None:
    """Additional discovery IDs should share canonical source-ID normalization."""
    store = _store(tmp_path)
    turn_record = TurnRecord.create(["$first", "$anchor"])

    metadata = store.build_run_metadata(
        turn_record,
        additional_discovery_event_ids=("", "$first", "$selection", "$selection"),
    )

    assert metadata == {
        constants.MATRIX_TURN_SCHEMA_VERSION_METADATA_KEY: TurnRecordCodec.schema_version(),
        constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$first", "$anchor"],
        constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY: ["$selection"],
    }


def test_discovery_alias_recovery_repairs_anchor_and_alias_rows(tmp_path: Path) -> None:
    """Missing-ledger recovery should index one non-coalesced turn by its anchor and discovery alias."""
    metadata = TurnRecordCodec.to_run_metadata(
        TurnRecord.create(
            ["$question"],
            response_owner="agent",
        ),
    )
    metadata[constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY] = ["$selection"]
    metadata[constants.MATRIX_EVENT_ID_METADATA_KEY] = "$question"
    metadata[constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] = "$response"
    recovery_record = TurnRecordCodec.from_run_metadata(metadata)

    assert recovery_record is not None
    assert recovery_record.source_event_ids == ("$question",)
    assert recovery_record.discovery_event_ids == ("$selection",)
    assert not recovery_record.is_coalesced

    for lookup_event_id in ("$question", "$selection"):
        store = _store(tmp_path / lookup_event_id.removeprefix("$"))
        loaded = _load_with_recovery(
            store,
            original_event_id=lookup_event_id,
            recovery_record=recovery_record,
        )

        assert loaded is not None
        assert loaded.source_event_ids == ("$question",)
        assert loaded.discovery_event_ids == ("$selection",)
        for indexed_event_id in ("$question", "$selection"):
            repaired = store.get_turn_record(indexed_event_id)
            assert repaired is not None
            assert repaired.source_event_ids == ("$question",)
            assert repaired.discovery_event_ids == ("$selection",)
            assert store.is_handled(indexed_event_id)


def test_recovery_does_not_replace_a_conflicting_completed_identity(tmp_path: Path) -> None:
    """Repair missing aliases without overwriting another completed source turn."""
    store = _store(tmp_path)
    store.record_turn(TurnRecord.create(["$selection"], response_event_id="$selection-response"))
    recovery_record = TurnRecord.create(
        ["$question"],
        discovery_event_ids=["$selection"],
        response_event_id="$question-response",
    )

    loaded = _load_with_recovery(
        store,
        original_event_id="$question",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.source_event_ids == ("$question",)
    assert loaded.discovery_event_ids == ()
    assert loaded.indexed_event_ids == ("$question",)
    assert store.get_turn_record("$question") == loaded
    selection_record = store.get_turn_record("$selection")
    assert selection_record is not None
    assert selection_record.source_event_ids == ("$selection",)
    assert selection_record.response_event_id == "$selection-response"

    store._ledger.flush()
    _reset_handled_turn_ledger_runtime()
    reloaded_store = _store(tmp_path)
    assert reloaded_store.get_turn_record("$question") == loaded
    assert reloaded_store.get_turn_record("$selection") == selection_record


def test_newer_delivered_run_recovers_mutable_facts_after_crash(tmp_path: Path) -> None:
    """A delivered run newer than the ledger should repair the edit crash window."""
    store = _store(tmp_path)
    ledger_record = TurnRecord.create(
        ["$first", "$anchor"],
        response_event_id="$old-response",
        source_event_prompts={"$first": "old first", "$anchor": "old anchor"},
        visible_echo_event_id="$echo",
        timestamp=10,
    )
    store._ledger.record_handled_turn(ledger_record)
    recovery_record = TurnRecord.create(
        ["$first", "$anchor"],
        response_event_id="$new-response",
        source_event_prompts={"$first": "edited first", "$anchor": "old anchor"},
        response_owner="agent",
        timestamp=20,
    )

    loaded = _load_with_recovery(
        store,
        original_event_id="$first",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.source_event_ids == ledger_record.source_event_ids
    assert loaded.anchor_event_id == ledger_record.anchor_event_id
    assert loaded.response_event_id == "$new-response"
    assert loaded.source_event_prompts == {"$first": "edited first", "$anchor": "old anchor"}
    assert loaded.visible_echo_event_id == "$echo"
    assert loaded.response_owner == "agent"
    assert loaded.timestamp == 20


def test_same_second_delivered_run_repairs_fractional_ledger_timestamp(tmp_path: Path) -> None:
    """Second-resolution run times should still repair a later run from the same second."""
    store = _store(tmp_path)
    store._ledger.record_handled_turn(
        TurnRecord.create(["$event"], response_event_id="$old-response", timestamp=10.9),
    )
    recovery_record = TurnRecord.create(["$event"], response_event_id="$new-response", timestamp=10)

    loaded = _load_with_recovery(
        store,
        original_event_id="$event",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.response_event_id == "$new-response"
    assert loaded.timestamp > 10.9


def test_repeated_delivered_run_recovery_keeps_ledger_version_stable(tmp_path: Path) -> None:
    """Idempotent recovery should not rewrite the ledger with synthetic timestamp drift."""
    store = _store(tmp_path)
    ledger_record = TurnRecord.create(
        ["$event"],
        response_event_id="$response",
        response_owner="agent",
        timestamp=10,
    )
    store._ledger.record_handled_turn(ledger_record)
    recovery_record = TurnRecord.create(
        ["$event"],
        response_event_id="$response",
        response_owner="agent",
        timestamp=20,
    )

    loaded = _load_with_recovery(
        store,
        original_event_id="$event",
        recovery_record=recovery_record,
    )

    assert loaded == ledger_record
    assert store.get_turn_record("$event") == ledger_record


def test_newer_interrupted_run_keeps_delivered_ledger_outcome(tmp_path: Path) -> None:
    """A newer run without Matrix delivery must not replace a visible response."""
    store = _store(tmp_path)
    store._ledger.record_handled_turn(
        TurnRecord.create(["$event"], response_event_id="$response", timestamp=10),
    )
    recovery_record = TurnRecord.create(["$event"], completed=False, timestamp=20)

    loaded = _load_with_recovery(
        store,
        original_event_id="$event",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.response_event_id == "$response"
    assert loaded.completed
    assert loaded.timestamp == 10


def test_terminal_write_refreshes_ledger_precedence_timestamp(tmp_path: Path) -> None:
    """A successful terminal write should become newer than its recovered input."""
    store = _store(tmp_path)
    store._ledger.record_handled_turn(
        TurnRecord.create(["$event"], response_event_id="$old-response", timestamp=1),
    )

    store.record_turn(TurnRecord.create(["$event"], response_event_id="$new-response", timestamp=1))

    updated = store.get_turn_record("$event")
    assert updated is not None
    assert updated.response_event_id == "$new-response"
    assert updated.timestamp > 1


def test_terminal_turn_can_replace_a_provisional_source_identity(tmp_path: Path) -> None:
    """A partial visible echo may join the canonical coalesced turn that completes it."""
    store = _store(tmp_path)
    store.record_visible_echo("$second", "$echo")

    store.record_turn(TurnRecord.create(["$first", "$second"], response_event_id="$response"))

    first_record = store.get_turn_record("$first")
    second_record = store.get_turn_record("$second")
    assert first_record is not None
    assert first_record == second_record
    assert first_record.source_event_ids == ("$first", "$second")
    assert first_record.visible_echo_event_id == "$echo"


def test_terminal_turn_rejects_conflicting_completed_canonical_source(tmp_path: Path) -> None:
    """A completed source cannot be reassigned into a different canonical turn."""
    store = _store(tmp_path)
    store.record_turn(TurnRecord.create(["$first"], response_event_id="$first-response"))

    store.record_turn(TurnRecord.create(["$first", "$second"], response_event_id="$other-response"))

    first_record = store.get_turn_record("$first")
    assert first_record is not None
    assert first_record.source_event_ids == ("$first",)
    assert first_record.response_event_id == "$first-response"
    assert store.get_turn_record("$second") is None


def test_run_metadata_without_current_schema_version_is_not_recovery_data() -> None:
    """Stale pre-user run metadata should not create an implicit migration path."""
    assert (
        TurnRecordCodec.from_run_metadata(
            {
                constants.MATRIX_EVENT_ID_METADATA_KEY: "$event",
                constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$event"],
            },
        )
        is None
    )


def test_run_metadata_with_empty_normalized_sources_falls_back_to_anchor() -> None:
    """Current metadata should never decode into an eventless canonical record."""
    parsed = TurnRecordCodec.from_run_metadata(
        {
            constants.MATRIX_TURN_SCHEMA_VERSION_METADATA_KEY: TurnRecordCodec.schema_version(),
            constants.MATRIX_EVENT_ID_METADATA_KEY: "$anchor",
            constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["", None, 42],
        },
    )

    assert parsed is not None
    assert parsed.anchor_event_id == "$anchor"
    assert parsed.source_event_ids == ("$anchor",)


def test_undelivered_run_repairs_as_incomplete_and_remains_retryable(tmp_path: Path) -> None:
    """A persisted run without Matrix response linkage must not become a handled turn."""
    store = _store(tmp_path)
    metadata = TurnRecordCodec.to_run_metadata(
        TurnRecord.create(["$event"], response_owner="agent"),
    )
    metadata[constants.MATRIX_EVENT_ID_METADATA_KEY] = "$event"
    recovery_record = TurnRecordCodec.from_run_metadata(metadata)

    assert recovery_record is not None
    loaded = _load_with_recovery(
        store,
        original_event_id="$event",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert not loaded.completed
    repaired = store.get_turn_record("$event")
    assert repaired is not None
    assert not repaired.completed
    assert repaired.response_owner == "agent"
    assert not store.is_handled("$event")


def test_load_turn_uses_ledger_identity_and_outcome_then_backfills_missing_context(tmp_path: Path) -> None:
    """Ledger facts should win field-by-field while absent optional context comes from run metadata."""
    store = _store(tmp_path)
    ledger_record = TurnRecord.create(
        ["$first", "$anchor"],
        response_event_id="$ledger-response",
        source_event_prompts={"$first": "ledger first", "$anchor": "ledger anchor"},
        requester_id="@ledger-user:example.org",
    )
    store.record_turn(ledger_record)
    persisted_ledger_record = store.get_turn_record("$first")
    assert persisted_ledger_record is not None
    recovery_target = MessageTarget.resolve("!room:example.org", None, "$anchor")
    recovery_record = TurnRecord.create(
        ["$run-only", "$anchor"],
        response_event_id="$run-response",
        source_event_prompts={"$run-only": "run", "$anchor": "run anchor"},
        response_owner="agent",
        requester_id="@run-user:example.org",
        history_scope=HistoryScope(kind="agent", scope_id="agent"),
        conversation_target=recovery_target,
    )

    loaded = _load_with_recovery(
        store,
        original_event_id="$first",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.source_event_ids == ("$first", "$anchor")
    assert loaded.anchor_event_id == "$anchor"
    assert loaded.response_event_id == "$ledger-response"
    assert loaded.source_event_prompts == {"$first": "ledger first", "$anchor": "ledger anchor"}
    assert loaded.requester_id == "@ledger-user:example.org"
    assert loaded.response_owner == "agent"
    assert loaded.history_scope == HistoryScope(kind="agent", scope_id="agent")
    assert loaded.conversation_target == recovery_target
    assert loaded.timestamp > persisted_ledger_record.timestamp
    repaired = store.get_turn_record("$first")
    assert repaired == loaded


def test_load_turn_repairs_missing_ledger_row_from_run_metadata(tmp_path: Path) -> None:
    """Run metadata should recover and immediately backfill an absent ledger row."""
    store = _store(tmp_path)
    recovery_record = TurnRecord.create(
        ["$event"],
        response_event_id="$response",
        response_owner="agent",
    )

    loaded = _load_with_recovery(
        store,
        original_event_id="$event",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.timestamp > 0
    assert replace(loaded, timestamp=0.0) == recovery_record
    repaired = store.get_turn_record("$event")
    assert repaired is not None
    assert repaired.response_event_id == "$response"
    assert repaired.response_owner == "agent"


def test_record_turn_preserves_existing_optional_facts_at_the_owner_boundary(tmp_path: Path) -> None:
    """TurnStore, rather than the physical ledger, should merge repeated writes."""
    store = _store(tmp_path)
    store.record_turn(
        TurnRecord.create(
            ["$event"],
            response_event_id="$first-response",
            requester_id="@user:example.org",
            correlation_id="corr-1",
        ),
    )

    store.record_turn(TurnRecord.create(["$event"], response_event_id="$second-response"))

    record = store.get_turn_record("$event")
    assert record is not None
    assert record.response_event_id == "$second-response"
    assert record.requester_id == "@user:example.org"
    assert record.correlation_id == "corr-1"


def test_visible_echo_cannot_overwrite_concurrent_terminal_outcome(tmp_path: Path) -> None:
    """A delayed visible-echo update must preserve a terminal write racing behind it."""
    store = _store(tmp_path)
    terminal_record = TurnRecord.create(["$event"], response_event_id="$response")
    echo_record_built = threading.Event()
    release_echo_record = threading.Event()
    terminal_started = threading.Event()
    terminal_finished = threading.Event()
    create_turn_record = TurnRecord.create

    def blocking_create(source_event_ids: list[str], *, completed: bool = True) -> TurnRecord:
        turn_record = create_turn_record(source_event_ids, completed=completed)
        if not completed:
            echo_record_built.set()
            assert release_echo_record.wait(timeout=2)
        return turn_record

    def record_visible_echo() -> None:
        store.record_visible_echo("$event", "$echo")

    def record_terminal_outcome() -> None:
        terminal_started.set()
        store.record_turn(terminal_record)
        terminal_finished.set()

    with patch.object(TurnRecord, "create", side_effect=blocking_create):
        echo_thread = threading.Thread(target=record_visible_echo)
        echo_thread.start()
        assert echo_record_built.wait(timeout=2)

        terminal_thread = threading.Thread(target=record_terminal_outcome)
        terminal_thread.start()
        assert terminal_started.wait(timeout=2)
        assert not terminal_finished.wait(timeout=0.1)

        release_echo_record.set()
        echo_thread.join(timeout=2)
        terminal_thread.join(timeout=2)

    assert not echo_thread.is_alive()
    assert not terminal_thread.is_alive()
    record = store.get_turn_record("$event")
    assert record is not None
    assert record.completed
    assert record.response_event_id == "$response"
    assert record.visible_echo_event_id == "$echo"


@pytest.mark.parametrize("recovery_response_event_id", [None, "$stale-response"])
def test_recovery_cannot_overwrite_concurrent_terminal_outcome(
    tmp_path: Path,
    recovery_response_event_id: str | None,
) -> None:
    """Slow incomplete or delivered recovery must preserve a concurrent terminal write."""
    store = _store(tmp_path)
    store._ledger.record_handled_turn(
        TurnRecord.create(["$event"], response_event_id="$old-response", timestamp=9),
    )
    recovery_started = threading.Event()
    release_recovery = threading.Event()
    load_finished = threading.Event()
    loaded_record: list[TurnRecord | None] = []
    recovery_record = TurnRecord.create(
        ["$event"],
        response_event_id=recovery_response_event_id,
        completed=recovery_response_event_id is not None,
        response_owner="agent",
        timestamp=10,
    )

    def load_recovery(_request: object) -> TurnRecord:
        recovery_started.set()
        assert release_recovery.wait(timeout=2)
        return recovery_record

    def load_turn() -> None:
        loaded_record.append(
            store.load_turn(
                room=MagicMock(room_id="!room:example.org"),
                thread_id=None,
                original_event_id="$event",
                requester_user_id="@user:example.org",
            ),
        )
        load_finished.set()

    with patch.object(store, "_load_persisted_turn_record", side_effect=load_recovery):
        load_thread = threading.Thread(target=load_turn)
        load_thread.start()
        assert recovery_started.wait(timeout=2)

        with patch("mindroom.handled_turns.time.time", return_value=10.9):
            store.record_turn(TurnRecord.create(["$event"], response_event_id="$response"))
        release_recovery.set()
        assert load_finished.wait(timeout=2)
        load_thread.join(timeout=2)

    assert not load_thread.is_alive()
    assert len(loaded_record) == 1
    assert loaded_record[0] is not None
    assert loaded_record[0].completed
    assert loaded_record[0].response_event_id == "$response"
    assert loaded_record[0].response_owner == "agent"
    assert loaded_record[0].timestamp > 10.9
    record = store.get_turn_record("$event")
    assert record == loaded_record[0]


def test_only_turn_store_imports_handled_turn_ledger_in_production() -> None:
    """HandledTurnLedger imports should stay isolated to TurnStore in production code."""
    src_root = Path(__file__).resolve().parents[1] / "src" / "mindroom"
    offenders: list[str] = []

    for path in src_root.rglob("*.py"):
        if path.name in {"turn_store.py", "handled_turns.py"}:
            continue
        module = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(module):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "mindroom.handled_turns":
                continue
            if any(alias.name == "HandledTurnLedger" for alias in node.names):
                offenders.append(path.relative_to(src_root).as_posix())
                break

    assert offenders == []


def test_agent_bot_does_not_expose_removed_handled_turn_ledger_shim(tmp_path: Path) -> None:
    """AgentBot instances should route handled-turn state only through TurnStore."""
    config = bind_runtime_paths(Config(), test_runtime_paths(tmp_path))
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="agent",
            user_id="@mindroom_agent:localhost",
            display_name="Agent",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )

    removed_attr = "_handled" + "_turn_ledger"
    assert removed_attr not in AgentBot.__dict__
    assert not hasattr(bot, removed_attr)
    assert removed_attr not in vars(bot)


def test_no_test_references_removed_bot_handled_turn_ledger_shim() -> None:
    """Tests should route all handled-turn access through TurnStore."""
    tests_root = Path(__file__).resolve().parent
    needle = "._handled" + "_turn_ledger"
    offenders = [
        path.relative_to(tests_root).as_posix() for path in tests_root.rglob("*.py") if needle in path.read_text()
    ]

    assert offenders == []
