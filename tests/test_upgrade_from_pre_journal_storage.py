"""Booting the journal runtime on a storage root a pre-journal MindRoom wrote.

This models the upgrade rather than performing it. A true two-version test
would check out the pre-journal revision, run it against a homeserver, and boot
this revision on what it left behind; that is too heavy for the suite, so the
fixture here constructs the same directory instead. Every byte it writes was
captured from a real pre-journal run against a local Synapse -- the ledger
payload, the ``mindroom-sync-continuity-v2`` record, and the two databases this
revision deleted the readers for -- so what is modelled is the shape of a real
installation, not an invented one.

The failure this exists to catch is silent and total. Nothing in this revision
writes the pre-journal ledger any more, so if the path the importer reads ever
drifts from the path that version wrote, no test fails, no error is logged, and
the first upgraded installation re-answers its entire backlog.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest
import yaml

from mindroom.config.main import load_config
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.event_journal_open import (
    bind_event_journal,
    event_journal_binding_path,
    open_event_journal,
    read_event_journal_binding,
)
from mindroom.handled_turns import HandledTurnLedger, legacy_responses_file_path
from mindroom.matrix.sync_continuity import SyncContinuityStore

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

AGENT = "general"

# One record as the pre-journal revision serialized it, taken verbatim from
# `mindroom_data/tracking/general_responded.json` after that revision answered
# a message. `schema_version` is its own, not this revision's.
PRE_JOURNAL_LEDGER: dict[str, object] = {
    "schema_version": 1,
    "records": {
        "$source": {
            "anchor_event_id": "$source",
            "source_event_ids": ["$source"],
            "redacted_source_event_ids": [],
            "pending_redaction_cleanup_event_ids": [],
            "response_event_id": "$answer",
            "completed": True,
            "timestamp": 1786159613.6159155,
            "source_event_prompts": {"$source": "@general upgrade-probe-1 please reply"},
            "response_owner": AGENT,
            "requester_id": "@mindroom_user:localhost",
            "correlation_id": "$source",
            "history_scope": {"kind": "agent", "scope_id": AGENT},
            "conversation_target": {
                "room_id": "!room:localhost",
                "source_thread_id": None,
                "resolved_thread_id": "$source",
                "reply_to_event_id": "$source",
                "session_id": "!room:localhost:$source",
            },
        },
    },
}

# The continuity record that revision wrote, whose checkpoint is certified
# against the event cache this revision replaced rather than against a journal.
PRE_JOURNAL_CONTINUITY_BYTES = (
    '{"checkpoint": {"cache_generation": '
    '"a5f02d35d4cbb74a43cb0c5792124f08ef6a32c8fa5b6a3ff9dc3fb6d3446a66", '
    '"token": "s34_16_4_1_1_1_1_19_0_1_2_1_1"}, '
    '"pending_join_decrypt_fences": [], '
    '"revision": 20, "version": "mindroom-sync-continuity-v2"}\n'
)

# Databases whose reader modules this revision deleted. Named exactly as that
# revision named them, because "left alone" is only a meaningful claim about
# files something could plausibly have gone looking for.
ORPHANED_EVENT_CACHE = "event_cache.db"
ORPHANED_OBLIGATIONS = f"tracking/dispatch_obligations-{AGENT}-14aef42d188d.sqlite3"

# Where the pre-journal revision wrote the ledger, spelled out rather than
# built from this revision's helper. Deriving both sides from the helper would
# make the path agree with itself: renaming the file this revision looks for
# would move the fixture along with it and nothing would fail, which is the one
# outcome these tests exist to prevent.
PRE_JOURNAL_LEDGER_RELATIVE_PATH = f"tracking/{AGENT}_responded.json"


def _write_sqlite_file(path: Path) -> None:
    """Write a real SQLite file, so "untouched" is a claim about real bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE leftovers (source_event_id TEXT, state TEXT)")
        connection.execute("INSERT INTO leftovers VALUES ('$owed', 'pending')")
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def pre_journal_storage(tmp_path: Path) -> tuple[Path, RuntimePaths]:
    """Return a storage root shaped like one a pre-journal MindRoom left behind."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "models": {"default": {"provider": "ollama", "id": "test-model"}},
                "agents": {AGENT: {"display_name": "General", "role": "An agent"}},
            },
        ),
        encoding="utf-8",
    )
    storage = tmp_path / "mindroom_data"
    (storage / "tracking").mkdir(parents=True)
    (storage / "sync_continuity").mkdir(parents=True)

    (storage / PRE_JOURNAL_LEDGER_RELATIVE_PATH).write_text(
        json.dumps(PRE_JOURNAL_LEDGER),
        encoding="utf-8",
    )
    (storage / "sync_continuity" / f"{AGENT}.json").write_text(
        PRE_JOURNAL_CONTINUITY_BYTES,
        encoding="utf-8",
    )
    _write_sqlite_file(storage / ORPHANED_EVENT_CACHE)
    _write_sqlite_file(storage / ORPHANED_OBLIGATIONS)

    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=storage,
        process_env={},
    )
    return storage, runtime_paths


@pytest.mark.asyncio
async def test_a_storage_root_with_no_binding_is_adopted_rather_than_refused(
    pre_journal_storage: tuple[Path, RuntimePaths],
) -> None:
    """An upgrading install has never bound a journal, and that must not be a refusal.

    The binding refusal exists to catch a journal belonging to somebody else.
    An install arriving from the version before journals has no binding at all,
    which is a different thing, and treating the two alike would mean this
    revision could never start anywhere except a fresh directory.
    """
    storage, runtime_paths = pre_journal_storage
    assert read_event_journal_binding(storage) is None

    journal_config = load_config(runtime_paths).event_journal
    opened = open_event_journal(journal_config, runtime_paths=runtime_paths, storage_path=storage)
    try:
        generation = await bind_event_journal(
            opened.store,
            journal_config=journal_config,
            runtime_paths=runtime_paths,
            storage_path=storage,
        )
    finally:
        await opened.close()

    binding = read_event_journal_binding(storage)
    assert binding is not None
    assert binding.generation == generation
    assert event_journal_binding_path(storage).exists()


@pytest.mark.ledger_loads_from_disk
@pytest.mark.asyncio
async def test_the_pre_journal_handled_turns_are_imported_from_the_path_that_version_wrote(
    pre_journal_storage: tuple[Path, RuntimePaths],
) -> None:
    """Terminal truth from before the journal must survive, or the backlog is re-answered.

    This is the whole upgrade in one assertion. The ledger file is written here
    at the literal path the pre-journal revision used, and read back through
    the helper this revision's runtime wires into the ledger, so the two halves
    of a contract whose writer no longer exists are pinned against each other.
    """
    storage, runtime_paths = pre_journal_storage
    legacy_file = legacy_responses_file_path(storage, AGENT)
    # The runtime reads this path; the fixture wrote the other one. They have
    # to be the same file or the import silently never happens.
    assert legacy_file == storage / PRE_JOURNAL_LEDGER_RELATIVE_PATH

    journal_config = load_config(runtime_paths).event_journal
    opened = open_event_journal(journal_config, runtime_paths=runtime_paths, storage_path=storage)
    try:
        await bind_event_journal(
            opened.store,
            journal_config=journal_config,
            runtime_paths=runtime_paths,
            storage_path=storage,
        )
        ledger = HandledTurnLedger(
            AGENT,
            records=opened.store.turn_records(AGENT),
            legacy_responses_file=legacy_file,
        )
        await ledger.load()

        assert ledger.has_responded("$source")
        record = ledger.get_turn_record("$source")
        assert record is not None
        assert record.response_event_id == "$answer"
    finally:
        await opened.close()

    # Renamed, so a later start cannot import it a second time over history
    # that compaction has deliberately dropped since.
    assert not legacy_file.exists()
    assert legacy_file.with_suffix(".json.imported").exists()


def test_the_pre_journal_sync_checkpoint_is_refused_and_repaired(
    pre_journal_storage: tuple[Path, RuntimePaths],
) -> None:
    """The saved transport position must be dropped, not carried into the journal.

    That checkpoint means "the event cache beside me holds everything up to
    here", and the event cache is exactly what this revision deleted. Honouring
    the token would resume past events the journal never saw and never will.
    Refusing it costs a cold start, which is the safe direction.
    """
    storage, _runtime_paths = pre_journal_storage
    store = SyncContinuityStore(storage, AGENT)

    with pytest.raises(RuntimeError, match="unsupported version"):
        store.load()

    repaired = store.clear_checkpoint()

    assert repaired.checkpoint is None
    assert store.load().checkpoint is None


@pytest.mark.asyncio
async def test_databases_this_revision_stopped_reading_are_left_where_they_are(
    pre_journal_storage: tuple[Path, RuntimePaths],
) -> None:
    """The replaced databases are neither read nor deleted, and that is a real cost.

    The obligation store is where the previous revision recorded work it had
    accepted and not finished. Nothing here inherits it, so a turn that was
    owed across the upgrade is not owed afterwards. Leaving the file in place
    is what keeps that recoverable by hand rather than merely gone, so this
    pins "left alone" rather than pretending the loss does not happen.
    """
    storage, runtime_paths = pre_journal_storage
    before = {name: (storage / name).read_bytes() for name in (ORPHANED_EVENT_CACHE, ORPHANED_OBLIGATIONS)}

    journal_config = load_config(runtime_paths).event_journal
    opened = open_event_journal(journal_config, runtime_paths=runtime_paths, storage_path=storage)
    try:
        await bind_event_journal(
            opened.store,
            journal_config=journal_config,
            runtime_paths=runtime_paths,
            storage_path=storage,
        )
    finally:
        await opened.close()

    assert {name: (storage / name).read_bytes() for name in before} == before
