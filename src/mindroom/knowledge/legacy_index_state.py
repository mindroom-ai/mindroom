"""Knowledge index state that earlier releases kept outside each collection, converted once at startup.

Earlier releases kept one base's state in one file with two writers (``indexing_settings.json``), and which source
files a collection held in a candidate checkpoint, its journal, and two seal files beside the collections, whose own
files had no state tables. Each base directory is converted before any refresh or search reads it, so the current
readers take only the current shape.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.knowledge.index_metadata import (
    Publication,
    RefreshStatus,
    is_count,
    load_json_object,
    optional_str,
    publication_path,
    save_publication,
    settings_from_json,
    update_refresh_status,
    utc_now,
    write_json_atomic,
)
from mindroom.knowledge.vector_store import (
    SCHEMA_VERSION,
    CollectionMeta,
    KnowledgeCollection,
    install_state_tables,
    list_collection_names,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.indexing_config import IndexingSettings

logger = get_logger(__name__)

_LEGACY_STATE_FILE = "indexing_settings.json"
_LEGACY_CHECKPOINT_FILE = "candidate_index.json"
_LEGACY_JOURNAL_FILE = "candidate_index.jsonl"
_LEGACY_PUBLISHED_SEAL_FILE = "published_index_seal.json"
_LEGACY_RETIRED_SEAL_FILE = "retired_index_seal.json"
_LEGACY_SCHEMA_VERSION = 1


# LEGACY_COMPAT: One knowledge index state file holding both the publication and the refresh job.
# Legacy format: `knowledge_db/<key>/indexing_settings.json` (and under `knowledge_db/private/<ns>/knowledge_db/`) with
# `settings`, `status` (`complete`, or `indexing`, `failed`, `resetting` before any publication), the publication's
# `collection`, `indexed_count`, `source_signature`, `published_revision`, `last_published_at`, and the refresh job's
# `refresh_job`, `reason`, `last_error`, `last_refresh_at`, `consecutive_refresh_failures`.
# Last legacy release: v2026.10.715 wrote it; the next release writes `publication.json` and `refresh_status.json`.
# Handling: At startup, a `complete` record that names its publication becomes `publication.json`, keeping the retired
# seal's collection as the kept one; the refresh job's reason, error, failure streak, and time become
# `refresh_status.json`, and a running or pending job is dropped, since no refresh survives a restart. A record that
# does not parse is kept, wrapped so it stays unreadable, so the next refresh still deletes and resumes nothing until
# a replacement publishes. The legacy file is deleted last, so a conversion a crash stopped reruns whole at the next
# start. Collections are upgraded before the publication is written, so a failed upgrade leaves the base unpublished
# and its first access refreshes it from what did convert. A conversion that fails is not retried: its legacy files
# are deleted too, so no later start can restore the legacy publication over one a refresh made since.
# Coverage: tests/test_knowledge_legacy_index_state.py::test_a_published_base_with_its_retired_collection_converts_and_refreshes_without_embedding,
# tests/test_knowledge_legacy_index_state.py::test_a_failed_first_build_keeps_its_error_and_resumes_its_candidate,
# tests/test_knowledge_legacy_index_state.py::test_a_stale_mark_survives_the_conversion,
# tests/test_knowledge_legacy_index_state.py::test_a_files_mode_publication_converts_without_a_collection,
# tests/test_knowledge_legacy_index_state.py::test_an_unreadable_legacy_state_stays_unreadable_and_protects_every_collection,
# tests/test_knowledge_legacy_index_state.py::test_a_second_start_changes_nothing_and_an_interrupted_conversion_finishes,
# tests/test_knowledge_legacy_index_state.py::test_a_failed_conversion_is_not_retried_over_a_later_publication,
# tests/test_knowledge_legacy_index_state.py::test_a_failed_conversion_leaves_the_base_unpublished_until_a_refresh_rebuilds_it,
# tests/test_knowledge_legacy_index_state.py::test_a_private_base_converts_where_private_indexes_live,
# tests/test_knowledge_legacy_index_state.py::test_a_linked_base_directory_is_left_alone.
def _legacy_publication(payload: dict[str, object], retired: str | None) -> Publication | None:
    settings = settings_from_json(payload.get("settings"))
    collection = optional_str(payload.get("collection"))
    indexed_count = _legacy_count(payload.get("indexed_count"))
    source_signature = optional_str(payload.get("source_signature"))
    if (
        settings is None
        or payload.get("status") != "complete"
        or indexed_count is None
        or source_signature is None
        or (collection is None and settings.mode != "files")
    ):
        return None
    return Publication(
        settings=settings,
        collection=collection,
        indexed_count=indexed_count,
        source_signature=source_signature,
        published_revision=optional_str(payload.get("published_revision")),
        last_published_at=optional_str(payload.get("last_published_at")) or utc_now(),
        retired=retired if retired != collection else None,
    )


def _legacy_refresh_status(payload: dict[str, object]) -> RefreshStatus:
    return RefreshStatus(
        reason=optional_str(payload.get("reason")),
        last_error=optional_str(payload.get("last_error")),
        consecutive_refresh_failures=_legacy_count(payload.get("consecutive_refresh_failures")) or 0,
        last_refresh_at=optional_str(payload.get("last_refresh_at")),
    )


# LEGACY_COMPAT: Knowledge file claims kept outside the collection, in a candidate checkpoint and seals.
# Legacy format: `candidate_index.json` (schema 2: `collection`, `settings`, `target_revision`, `total_files`,
# `completed`, `failed`) with its `candidate_index.jsonl` journal, and `published_index_seal.json` and
# `retired_index_seal.json` snapshots naming the published and the retired collection and their settings.
# Last legacy release: v2026.10.715 wrote them (the checkpoint since v2026.7.266, at schema 2 since v2026.9.7, the
# seals since v2026.10.558); the next release keeps each collection's claims in its own `files` table. An earlier
# checkpoint names only a Chroma-era candidate, which no SQLite file matches, so it is only deleted.
# Handling: They only name each collection and its settings for the schema upgrade below; claims come from the rows.
# A checkpoint candidate with no claims in its snapshot and an empty journal is a bulk copy interrupted before its
# claims were recorded, whose rows may cut a file short, or a build that claimed nothing yet, so it is deleted, as the
# old code rebuilt it.
# Then every checkpoint and seal file is deleted.
# Coverage: tests/test_knowledge_legacy_index_state.py::test_a_published_base_with_its_retired_collection_converts_and_refreshes_without_embedding,
# tests/test_knowledge_legacy_index_state.py::test_an_interrupted_build_with_a_journal_resumes_where_it_stopped,
# tests/test_knowledge_legacy_index_state.py::test_an_interrupted_bulk_copy_is_discarded_and_the_publication_kept.
@dataclass(frozen=True)
class _LegacySnapshot:
    """A checkpoint or seal: the collection it names, its settings, and what its claims described."""

    collection: str
    settings: IndexingSettings
    revision: str | None
    total_files: int | None
    #: Whether the snapshot itself recorded claims.
    claimed: bool


def _legacy_snapshot(path: Path) -> _LegacySnapshot | None:
    payload = load_json_object(path)
    if payload is None:
        return None
    collection = optional_str(payload.get("collection"))
    settings = settings_from_json(payload.get("settings"))
    if collection is None or settings is None:
        return None
    return _LegacySnapshot(
        collection=collection,
        settings=settings,
        revision=optional_str(payload.get("target_revision")),
        total_files=_legacy_count(payload.get("total_files")),
        claimed=bool(payload.get("completed")),
    )


def _interrupted_copy(storage: Path, checkpoint: _LegacySnapshot) -> bool:
    try:
        journal = (storage / _LEGACY_JOURNAL_FILE).read_text(encoding="utf-8")
    except OSError:
        journal = ""
    return not checkpoint.claimed and not journal.strip()


# LEGACY_COMPAT: Knowledge collection files without their own state tables.
# Legacy format: `<collection>.sqlite3` at `PRAGMA user_version = 1`, holding `chunks`, its keyword index, and
# `generation`; each row's metadata carries its source file's signature.
# Last legacy release: v2026.10.715 wrote them (since v2026.10.569); the next release writes schema 2.
# Handling: A schema-1 collection whose settings the legacy state names is upgraded in place, in one transaction:
# `files` claims each path at the signature its rows carry (every path's rows were written in one transaction),
# `failures` starts empty, and `meta` records those settings, `complete` for the published and retired collections
# and `building` for a candidate. No row and no vector is rewritten. A file the old code restamped without rewriting
# its rows is hashed again by each unchanged check until a refresh that indexes a change restamps its claim, and empty
# files and failed files are simply indexed again. Other schema-1 collections stay as they are for the next refresh,
# which deletes them as unusable unless the publication is unreadable. The published collection is upgraded first.
# Coverage: tests/test_knowledge_legacy_index_state.py::test_a_published_base_with_its_retired_collection_converts_and_refreshes_without_embedding,
# tests/test_knowledge_legacy_index_state.py::test_a_publication_of_a_resumed_candidate_converts_with_its_claims,
# tests/test_knowledge_legacy_index_state.py::test_an_interrupted_build_with_a_journal_resumes_where_it_stopped,
# tests/test_knowledge_legacy_index_state.py::test_a_failed_first_build_keeps_its_error_and_resumes_its_candidate,
# tests/test_knowledge_legacy_index_state.py::test_a_failed_upgrade_of_a_candidate_leaves_the_published_collection_converted,
# tests/test_knowledge_legacy_index_state.py::test_a_collection_an_unfinished_record_names_is_resumed.
def _upgrade_collection(collection: KnowledgeCollection, meta: CollectionMeta) -> None:
    with closing(sqlite3.connect(collection.file, timeout=30.0)) as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0] != _LEGACY_SCHEMA_VERSION:
            return
        connection.execute("BEGIN")
        install_state_tables(connection, meta)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()


async def migrate_knowledge_index_state(runtime_paths: RuntimePaths) -> None:
    """Convert every knowledge base directory an earlier release left, before any refresh or search reads one."""
    await run_blocking_until_complete(_migrate_knowledge_index_state, runtime_paths)


def _migrate_knowledge_index_state(runtime_paths: RuntimePaths) -> None:
    root = runtime_paths.storage_root / "knowledge_db"
    states = [*root.glob(f"*/{_LEGACY_STATE_FILE}"), *root.glob(f"private/*/knowledge_db/*/{_LEGACY_STATE_FILE}")]
    for state_file in sorted(states):
        storage = state_file.parent
        if storage.is_symlink() or state_file.is_symlink():
            continue
        try:
            _migrate_base(storage)
        except (OSError, sqlite3.Error, ValueError) as error:
            # Startup goes on, and the base keeps what converted: unpublished when a collection did not, so its first
            # access refreshes it from what did. The legacy state goes too, because that refresh publishes over it and
            # a rerun would restore the older publication.
            logger.warning("knowledge_index_state_conversion_failed", storage=str(storage), error=str(error))
            _delete_legacy_state(storage)


def _migrate_base(storage: Path) -> None:
    state_file = storage / _LEGACY_STATE_FILE
    payload = load_json_object(state_file)
    if payload is None:
        write_json_atomic(
            publication_path(storage),
            {"unreadable_legacy_state": state_file.read_text(encoding="utf-8", errors="replace")},
        )
    else:
        published_seal = _legacy_snapshot(storage / _LEGACY_PUBLISHED_SEAL_FILE)
        retired_seal = _legacy_snapshot(storage / _LEGACY_RETIRED_SEAL_FILE)
        publication = _legacy_publication(payload, None if retired_seal is None else retired_seal.collection)
        # Collections first, so a publication is written only once the collection it names can be read.
        seals = [seal for seal in (published_seal, retired_seal) if seal is not None]
        _upgrade_named_collections(storage, payload, publication, seals)
        if publication is not None:
            save_publication(storage, publication)
        status = _legacy_refresh_status(payload)
        if status != RefreshStatus():
            update_refresh_status(storage, lambda _current: status)
    _delete_legacy_state(storage)


def _delete_legacy_state(storage: Path) -> None:
    """Delete the base's legacy state files, the state file last, so a conversion a crash stopped reruns whole.

    The checkpoint goes before its journal: a rerun that read a checkpoint without its journal would take the
    candidate for an interrupted copy and delete it.
    """
    for name in (_LEGACY_CHECKPOINT_FILE, _LEGACY_JOURNAL_FILE, _LEGACY_PUBLISHED_SEAL_FILE, _LEGACY_RETIRED_SEAL_FILE):
        (storage / name).unlink(missing_ok=True)
    (storage / _LEGACY_STATE_FILE).unlink(missing_ok=True)


def _upgrade_named_collections(
    storage: Path,
    payload: dict[str, object],
    publication: Publication | None,
    seals: list[_LegacySnapshot],
) -> None:
    """Upgrade each schema-1 collection the legacy state names, with the settings and stage it names it under."""
    published = None if publication is None else publication.collection
    named: dict[str, tuple[IndexingSettings, Literal["building", "complete"], str | None, int | None]] = {}
    checkpoint = _legacy_snapshot(storage / _LEGACY_CHECKPOINT_FILE)
    if checkpoint is not None:
        if _interrupted_copy(storage, checkpoint) and checkpoint.collection != published:
            KnowledgeCollection(path=storage, name=checkpoint.collection).delete()
        else:
            named[checkpoint.collection] = (
                checkpoint.settings,
                "building",
                checkpoint.revision,
                checkpoint.total_files,
            )
    if publication is None:
        settings = settings_from_json(payload.get("settings"))
        collection = optional_str(payload.get("collection"))
        if settings is not None and collection is not None:
            named.setdefault(collection, (settings, "building", optional_str(payload.get("published_revision")), None))
    for seal in seals:
        named[seal.collection] = (seal.settings, "complete", seal.revision, seal.total_files)
    if publication is not None and publication.collection is not None:
        named[publication.collection] = (
            publication.settings,
            "complete",
            publication.published_revision,
            publication.indexed_count,
        )
    present = set(list_collection_names(storage))
    now = utc_now()
    # The published collection first, so a failure upgrading another still leaves the live one readable.
    for collection, (settings, stage, revision, total_files) in sorted(
        named.items(),
        key=lambda item: item[0] != published,
    ):
        if collection not in present:
            continue
        meta = CollectionMeta(
            settings=settings.to_metadata(),
            stage=stage,
            rebuild=False,
            revision=revision,
            total_files=total_files or 0,
            created_at=now,
            updated_at=now,
        )
        _upgrade_collection(KnowledgeCollection(path=storage, name=collection), meta)


def _legacy_count(value: object) -> int | None:
    return value if is_count(value) else None
