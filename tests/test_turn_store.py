"""Tests for canonical turn ownership, precedence, and repair."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

from mindroom import constants
from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.handled_turns import SourceEventMetadata, TurnRecord, TurnRecordCodec
from mindroom.history.types import HistoryScope
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
    assert parsed == turn_record


def test_build_run_metadata_normalizes_additional_source_ids(tmp_path: Path) -> None:
    """Additional discovery IDs should share canonical source-ID normalization."""
    store = _store(tmp_path)
    turn_record = TurnRecord.create(["$first", "$anchor"])

    metadata = store.build_run_metadata(
        turn_record,
        additional_source_event_ids=("", "$first", "$selection", "$selection"),
    )

    assert metadata == {
        constants.MATRIX_TURN_SCHEMA_VERSION_METADATA_KEY: TurnRecordCodec.schema_version(),
        constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$first", "$anchor", "$selection"],
    }


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
    repaired = store.get_turn_record("$first")
    assert repaired is not None
    assert repaired.response_owner == "agent"


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

    assert loaded == recovery_record
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
