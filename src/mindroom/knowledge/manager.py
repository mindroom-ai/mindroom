"""The candidate lifecycle of one semantic knowledge base: choose a collection to build, catch it up, publish it.

Every collection holds its own claims, failures, and lifecycle (``knowledge.vector_store``), so the next refresh
resumes from whichever collection survives.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import RuntimePaths, resolve_config_relative_path
from mindroom.embeddings.batching import RefreshEmbedder
from mindroom.embeddings.factory import create_configured_embedder
from mindroom.knowledge.collections import (
    CollectionSpace,
    candidate_collection_name,
    cleanup_superseded_collections,
    drain_readers,
    open_collection,
    unusable_collection,
    working_collections,
)
from mindroom.knowledge.corpus_signature import corpus_signature, scan_corpus, scan_file_signatures_async
from mindroom.knowledge.file_indexer import FileIndexer, max_concurrent_file_indexes
from mindroom.knowledge.file_listing import knowledge_files_from_relative_paths, list_knowledge_files
from mindroom.knowledge.git_source import GitKnowledgeSource
from mindroom.knowledge.index_metadata import (
    Publication,
    load_publication,
    new_publication,
    publication_unreadable,
    save_publication,
    utc_now,
)
from mindroom.knowledge.indexing_config import (
    IndexingSettings,
    indexing_settings_key,
    knowledge_git_dir,
    storage_key_for_base,
)
from mindroom.knowledge.legacy_chroma_storage import (
    chroma_era_publication,
    legacy_chroma_rows,
    legacy_chroma_store_exists,
)
from mindroom.knowledge.redaction import redact_credentials_in_text
from mindroom.knowledge.refresh_outcome import RefreshOutcome
from mindroom.knowledge.vector_store import CollectionMeta
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.knowledge.corpus_signature import FileSignature
    from mindroom.knowledge.vector_store import KnowledgeCollection

logger = get_logger(__name__)

#: Reconciliation passes before a refresh gives up for now. A source that keeps
#: changing keeps its candidate and converges over successive refreshes instead
#: of thrashing inside one.
_MAX_CANDIDATE_RECONCILE_ROUNDS = 4
_PROGRESS_LOG_INTERVAL_FILES = 500
_PROGRESS_LOG_INTERVAL_SECONDS = 30.0
#: A retired collection more than this share free is copied afresh from the publication instead of taken over:
#: deleted rows leave pages SQLite reuses but never returns.
_MAX_REUSED_FREE_FRACTION = 0.25


@dataclass
class _CandidateRun:
    """One refresh's live view of the collection it is advancing."""

    collection: KnowledgeCollection
    #: A forced rebuild, which neither reuses nor keeps the publication it replaces.
    rebuild: bool
    #: The publication this refresh started from, which publishing its candidate retires.
    retired: Publication | None
    #: Revision the candidate's claims describe, when known.
    baseline_revision: str | None
    #: The store's claims, mirrored after each committed write.
    completed: dict[str, FileSignature]
    failed: set[str] = field(default_factory=set)
    vanished: set[str] = field(default_factory=set)
    resumed: bool = False
    published: bool = False
    error: str | None = None

    def record_error(self, detail: str) -> None:
        """Store why this refresh failed, redacted once at the point of record."""
        self.error = redact_credentials_in_text(detail)


@dataclass
class _CandidateProgress:
    """Throttled progress accounting for one candidate build."""

    base_id: str
    resumed: bool = False
    target_revision: str | None = None
    collection: str = ""
    total: int = 0
    completed: int = 0
    failed: int = 0
    retrying: int = 0
    #: Files this pass actually embedded, as opposed to reused from the candidate.
    indexed_this_run: int = 0
    started_at: float = field(default_factory=time.monotonic)
    _last_logged_at: float = field(default_factory=time.monotonic, repr=False)
    _last_logged_completed: int = field(default=0, repr=False)

    @property
    def pending(self) -> int:
        """Return files still owed by the candidate."""
        return max(self.total - self.completed, 0)

    def elapsed_seconds(self) -> float:
        """Return wall-clock seconds since this refresh started working."""
        return max(time.monotonic() - self.started_at, 0.0)

    def _fields(self) -> dict[str, object]:
        return {
            "base_id": self.base_id,
            "collection": self.collection,
            "resumed": self.resumed,
            "target_revision": self.target_revision,
            "total": self.total,
            "completed": self.completed,
            "indexed_this_run": self.indexed_this_run,
            "pending": self.pending,
            "failed": self.failed,
            "retrying": self.retrying,
            "elapsed_seconds": round(self.elapsed_seconds(), 3),
        }

    def maybe_log(self) -> None:
        """Emit one periodic INFO summary instead of one line per file."""
        now = time.monotonic()
        due = (self.completed - self._last_logged_completed) >= _PROGRESS_LOG_INTERVAL_FILES or (
            now - self._last_logged_at
        ) >= _PROGRESS_LOG_INTERVAL_SECONDS
        if not due:
            return
        self._last_logged_at = now
        self._last_logged_completed = self.completed
        logger.info("knowledge_candidate_progress", **self._fields())

    def log_summary(self, outcome: RefreshOutcome) -> None:
        """Emit the single terminal summary for this refresh."""
        logger.info("knowledge_candidate_finished", published=outcome.published, error=outcome.error, **self._fields())


def _ensure_knowledge_directory_ready(knowledge_path: Path) -> None:
    if knowledge_path.exists() and not knowledge_path.is_dir():
        msg = f"Knowledge path {knowledge_path} must be a directory"
        raise ValueError(msg)
    knowledge_path.mkdir(parents=True, exist_ok=True)


@dataclass
class KnowledgeManager:
    """Manage indexing for one knowledge base folder."""

    base_id: str
    config: Config
    runtime_paths: RuntimePaths
    storage_path: Path | None = None
    knowledge_path: Path | None = None
    #: Collaborator that owns the Git checkout this base indexes, when one is
    #: configured. Always present: it answers ``is_configured()`` for itself.
    git_source: GitKnowledgeSource = field(init=False)
    _indexing_settings: IndexingSettings = field(init=False)
    _base_storage_path: Path = field(init=False)
    _collections: CollectionSpace = field(init=False, repr=False)
    _max_concurrent_file_indexes: int = field(init=False, repr=False)
    #: The files a local base's last corpus hash listed and signed, which the next reconciliation adopts once.
    _source_scan: dict[str, tuple[FileSignature, Path]] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialize filesystem paths and the Git checkout this base indexes; a bad indexing limit raises here."""
        self._max_concurrent_file_indexes = max_concurrent_file_indexes(self.runtime_paths)
        base_config = self.config.get_knowledge_base_config(self.base_id)
        storage_path = (self.storage_path or self.runtime_paths.storage_root).resolve()
        knowledge_path = self.knowledge_path or resolve_config_relative_path(
            base_config.path,
            runtime_paths=self.runtime_paths,
        )
        _ensure_knowledge_directory_ready(knowledge_path)
        # The binding resolved this path; resolving it again would follow a link swapped onto it since.
        if knowledge_path.resolve() != knowledge_path:
            msg = f"Knowledge path must be canonical and not go through a link: {knowledge_path}"
            raise ValueError(msg)
        self.storage_path = storage_path
        self.knowledge_path = knowledge_path
        self._indexing_settings = indexing_settings_key(self.config, storage_path, self.base_id, knowledge_path)
        self._base_storage_path = (
            storage_path / "knowledge_db" / storage_key_for_base(self.base_id, knowledge_path)
        ).resolve()
        self._base_storage_path.mkdir(parents=True, exist_ok=True)
        self.git_source = GitKnowledgeSource(
            base_id=self.base_id,
            config=self.config,
            runtime_paths=self.runtime_paths,
            source_path=knowledge_path,
            # Not ``self.storage_path``: Git directories live in their own
            # primary-only tree keyed by the checkout, so bases sharing a folder
            # share one repository, and the Git directory decides which commands
            # Git runs here.
            git_dir=knowledge_git_dir(self.runtime_paths.storage_root, knowledge_path),
            lfs_hydrated_head_path=self._base_storage_path / "git_lfs_hydrated_head.txt",
        )
        self._collections = CollectionSpace(
            base_id=self.base_id,
            knowledge_path=knowledge_path,
            storage_path=self._base_storage_path,
        )

    def _knowledge_root(self) -> Path:
        knowledge_path = self.knowledge_path
        assert knowledge_path is not None
        return knowledge_path

    def list_files(self) -> list[Path]:
        """List all files currently present in the knowledge folder."""
        knowledge_root = self._knowledge_root()
        if self.git_source.is_configured():
            tracked_relative_paths = self.git_source.tracked_relative_paths()
            if tracked_relative_paths is None:
                return []
            return knowledge_files_from_relative_paths(
                self.config,
                self.base_id,
                knowledge_root,
                tracked_relative_paths,
            )
        return list_knowledge_files(self.config, self.base_id, knowledge_root)

    async def source_signature(self, known: Mapping[str, FileSignature] | None = None) -> str:
        """Return the signature of the corpus this base currently manages.

        A file whose ctime and size still match its ``known`` signature is not read again.

        Git-backed bases hand over the tracked paths this process already
        listed, so an unchanged checkout is not re-listed just to hash it.
        A local base keeps the scan for its next reconciliation instead of
        listing and hashing the corpus again: a file that changes after the
        scan still fails the hash that ends the pass. A Git base's pass ends
        on its revision instead, which a stale scan could slip past.
        """
        scanned = await asyncio.to_thread(
            scan_corpus,
            self.config,
            self.base_id,
            self._knowledge_root(),
            git_dir=self.git_source.git_dir,
            tracked_relative_paths=self.git_source.cached_tracked_relative_paths(),
            known=known,
        )
        self._source_scan = None if self.git_source.is_configured() else scanned
        return corpus_signature({relative_path: signature for relative_path, (signature, _path) in scanned.items()})

    async def published_file_signatures(self, collection: str) -> Mapping[str, FileSignature] | None:
        """Return the signatures the published ``collection`` claims, for a stat-first scan to reuse.

        None when the collection is not one this release can read, which a refresh must rebuild rather than republish.
        """
        try:
            return await asyncio.to_thread(open_collection(self._collections, collection).files)
        except Exception as error:
            if not unusable_collection(error):
                raise
            return None

    async def has_unfinished_rebuild(self, publication: Publication | None) -> bool:
        """Return whether a forced rebuild of the current settings was interrupted before it published."""
        return any(
            self._unpublished_rebuild(meta, publication)
            for _collection, meta in await asyncio.to_thread(self._working_collections, publication)
        )

    @staticmethod
    def _unpublished_rebuild(meta: CollectionMeta, publication: Publication | None) -> bool:
        """Return whether a collection with ``meta`` is a forced rebuild that never published.

        A rebuild keeps its mark after it publishes, so one the current publication replaced, which an interrupted
        cleanup spared, is told apart by age: only a rebuild started after the current publication is still to publish.
        """
        return meta.rebuild and (publication is None or meta.created_at > publication.last_published_at)

    def _relative_path(self, file_path: Path) -> str:
        return file_path.relative_to(self._knowledge_root()).as_posix()

    def _new_meta(self, *, rebuild: bool, revision: str | None = None) -> CollectionMeta:
        now = utc_now()
        return CollectionMeta(
            settings=self._indexing_settings.to_metadata(),
            stage="building",
            rebuild=rebuild,
            revision=revision,
            total_files=0,
            created_at=now,
            updated_at=now,
        )

    async def reindex_all(self, *, force_reindex: bool = False) -> RefreshOutcome:
        """Advance a candidate collection and publish it when it matches the source.

        ``force_reindex`` is the operator asking for vectors to be rebuilt
        rather than trusted: the candidate starts empty and its publication
        keeps no older collection. Every other reason to rebuild -- changed
        settings, a missing collection -- the reuse rules reject on their own,
        apart from a Chroma-era publication, whose vectors are copied once.
        """
        run = await self._open_candidate_run(force_reindex=force_reindex)
        indexer = FileIndexer(
            base_id=self.base_id,
            config=self.config,
            runtime_paths=self.runtime_paths,
            knowledge_root=self._knowledge_root(),
            collection=run.collection,
            build_embedder=lambda: RefreshEmbedder(
                create_configured_embedder(self.config, self.runtime_paths, caller_retries=True),
            ),
            max_concurrent_file_indexes=self._max_concurrent_file_indexes,
        )
        progress = _CandidateProgress(
            base_id=self.base_id,
            resumed=run.resumed,
            target_revision=run.baseline_revision,
            collection=run.collection.name,
            completed=len(run.completed),
        )
        try:
            await self._advance_candidate(run, indexer, progress)
        except Exception as exc:
            if run.error is None:
                run.record_error(str(exc))
            raise
        finally:
            progress.retrying = indexer.retry_count
            outcome = RefreshOutcome(
                indexed_count=progress.indexed_this_run,
                published=run.published,
                error=run.error,
            )
            progress.log_summary(outcome)
        return outcome

    async def _open_candidate_run(self, *, force_reindex: bool) -> _CandidateRun:
        """Choose the collection this refresh advances, deleting the working collections it will not.

        A working collection is an owned one the publication does not name. Its own tables are exact, so any of
        them built under the current settings is a sound place to resume.
        """
        storage = self._base_storage_path
        publication = await asyncio.to_thread(load_publication, storage)
        if await asyncio.to_thread(publication_unreadable, storage, publication):
            # The live collection cannot be proven, so nothing is resumed or deleted, whatever the refresh asked.
            logger.warning(
                "Starting a fresh knowledge candidate and deleting nothing because the publication is unreadable",
                base_id=self.base_id,
            )
            return await self._new_candidate_run(publication=None, rebuild=force_reindex)
        if force_reindex:
            await self._delete_working(publication, keep=None)
            return await self._new_candidate_run(publication=publication, rebuild=True)

        chosen = await self._resumable_collection(publication)
        await self._delete_working(publication, keep=None if chosen is None else chosen[0].name)
        if chosen is None:
            return await self._new_candidate_run(publication=publication, rebuild=False)
        collection, meta = chosen
        return _CandidateRun(
            collection=collection,
            rebuild=self._unpublished_rebuild(meta, publication),
            retired=publication,
            baseline_revision=meta.revision,
            completed=await asyncio.to_thread(collection.files),
            failed=await asyncio.to_thread(collection.failed_paths),
            resumed=meta.stage == "building",
        )

    def _working_collections(self, publication: Publication | None) -> list[tuple[KnowledgeCollection, CollectionMeta]]:
        """Return the working collections built under the current settings, with their state."""
        return working_collections(
            self._collections,
            published=None if publication is None else publication.collection,
            settings=self._indexing_settings,
        )

    async def _resumable_collection(
        self,
        publication: Publication | None,
    ) -> tuple[KnowledgeCollection, CollectionMeta] | None:
        """Return the working collection to resume, or None to start a fresh candidate.

        In order: an unfinished build, resumed whatever its free space, so a failed build never loses completed work;
        a forced rebuild that completed but never published; then the collection the publication retired, taken over
        only while it is at most a quarter free and only after its readers drain. Any other complete collection is a
        leftover the last publication did not keep, such as the one a forced rebuild replaced, and is deleted. Before
        any semantic publication, or after a files-mode one, which names no collection, there is nothing to leave
        over, so a complete collection is a finished build to resume.
        """
        usable = await asyncio.to_thread(self._working_collections, publication)
        retired_name = None if publication is None else publication.retired
        no_semantic_publication = publication is None or publication.collection is None
        unfinished = [entry for entry in usable if entry[1].stage == "building" or no_semantic_publication]
        if unfinished:
            return max(unfinished, key=lambda entry: entry[1].updated_at)
        rebuilt = [entry for entry in usable if self._unpublished_rebuild(entry[1], publication)]
        if rebuilt:
            return max(rebuilt, key=lambda entry: entry[1].updated_at)
        retired = next((entry for entry in usable if entry[0].name == retired_name), None)
        if retired is None:
            return None
        collection, meta = retired
        assert publication is not None
        assert publication.collection is not None
        if (
            await asyncio.to_thread(open_collection(self._collections, publication.collection).exists)
            and await asyncio.to_thread(collection.free_fraction) > _MAX_REUSED_FREE_FRACTION
        ):
            return None
        # Readers that selected it before the last publication finish first; no new reader can select it since.
        await run_blocking_until_complete(drain_readers, self._collections)
        logger.info(
            "Resuming the retired knowledge collection instead of copying the published one",
            base_id=self.base_id,
            collection=collection.name,
        )
        return collection, meta

    async def _delete_working(self, publication: Publication | None, *, keep: str | None) -> None:
        """Delete every working collection but ``keep``; a legacy one under the default name waits for a publication."""
        published = None if publication is None else publication.collection
        await run_blocking_until_complete(
            partial(
                cleanup_superseded_collections,
                self._collections,
                preserved=frozenset(name for name in (published, keep) if name is not None),
                candidates_only=True,
            ),
        )

    async def _new_candidate_run(self, *, publication: Publication | None, rebuild: bool) -> _CandidateRun:
        """Create a fresh candidate: a compact copy of the publication when its settings allow, else empty."""
        collection = open_collection(self._collections, candidate_collection_name(self._collections))
        revision = await self._create_candidate(collection, rebuild=rebuild, publication=publication)
        completed = await asyncio.to_thread(collection.files)
        failed = await asyncio.to_thread(collection.failed_paths)
        if completed:
            logger.info(
                "Reused published knowledge vectors in a new candidate",
                base_id=self.base_id,
                collection=collection.name,
                reused_files=len(completed),
            )
        return _CandidateRun(
            collection=collection,
            rebuild=rebuild,
            retired=publication,
            baseline_revision=revision,
            completed=completed,
            failed=failed,
        )

    async def _create_candidate(
        self,
        collection: KnowledgeCollection,
        *,
        rebuild: bool,
        publication: Publication | None,
    ) -> str | None:
        """Create ``collection``, copied from ``publication`` when that is sound; return the revision it describes.

        Matching settings are what make a copy sound: they pin the chunker, the embedder identity and every corpus
        filter. Reconciliation then compares each copied claim with the file on disk. A forced rebuild copies
        nothing, and a copy that cannot be made costs a full rebuild rather than a failed refresh.
        """
        if (
            not rebuild
            and publication is not None
            and publication.collection is not None
            and self._copy_allowed(publication)
        ):
            meta = self._new_meta(rebuild=False, revision=publication.published_revision)
            published = open_collection(self._collections, publication.collection)
            try:
                if await asyncio.to_thread(published.exists):
                    await run_blocking_until_complete(collection.create_copy, published, meta)
                    return publication.published_revision
                if chroma_era_publication(publication.settings) and legacy_chroma_store_exists(self._base_storage_path):
                    await run_blocking_until_complete(self._import_chroma, collection, publication.collection, meta)
                    return publication.published_revision
            except Exception:
                logger.warning(
                    "Falling back to a full knowledge rebuild after the published index could not be copied",
                    base_id=self.base_id,
                    collection=publication.collection,
                    exc_info=True,
                )
        await run_blocking_until_complete(collection.create, self._new_meta(rebuild=rebuild))
        return None

    def _copy_allowed(self, publication: Publication) -> bool:
        settings = publication.settings
        if chroma_era_publication(settings):
            # Compared as if the publication were owned; the copy reads Chroma.
            settings = replace(settings, vector_store=self._indexing_settings.vector_store)
        return settings == self._indexing_settings

    def _import_chroma(self, collection: KnowledgeCollection, chroma_collection: str, meta: CollectionMeta) -> None:
        with legacy_chroma_rows(self._base_storage_path, chroma_collection, runtime_paths=self.runtime_paths) as pages:
            collection.create(meta, pages=pages)

    async def _reconcile_candidate(
        self,
        run: _CandidateRun,
        *,
        round_revision: str | None,
        changed_files: frozenset[str] | None,
    ) -> tuple[frozenset[str], tuple[Path, ...]]:
        """Align the candidate with the current source listing; return the expected paths and the files still owed."""
        # ``vanished`` describes files lost during one indexing pass, so it must
        # not outlive the pass and permanently exclude a path that came back.
        run.vanished.clear()
        scanned, self._source_scan = self._source_scan, None
        if changed_files is None and scanned is not None:
            signatures = scanned
        else:
            listed = sorted((self._relative_path(path), path) for path in await asyncio.to_thread(self.list_files))
            if changed_files is None:
                signatures = await scan_file_signatures_async(listed)
            else:
                to_scan = [entry for entry in listed if entry[0] in changed_files or entry[0] not in run.completed]
                signatures = {
                    relative_path: (run.completed[relative_path], file_path)
                    for relative_path, file_path in listed
                    if relative_path not in changed_files and relative_path in run.completed
                }
                signatures.update(await scan_file_signatures_async(to_scan))
                logger.info(
                    "Used knowledge Git delta for candidate reconciliation",
                    base_id=self.base_id,
                    changed_count=len(changed_files),
                    scanned_count=len(to_scan),
                    managed_count=len(listed),
                )
        present = frozenset(signatures)

        # Rows are dropped for paths that left the corpus and for paths whose
        # content changed: a changed file whose re-index later fails must not
        # leave stale rows behind.
        dropped = (set(run.completed) | run.failed) - present
        restamped: dict[str, FileSignature] = {}
        for relative_path in set(run.completed) & present:
            recorded = run.completed[relative_path]
            current = signatures[relative_path][0]
            # Git checkouts, archive restores and chmod may change only the stamp.
            # Size and digest are the content identity that decides whether vectors survive.
            if recorded[1:] != current[1:]:
                dropped.add(relative_path)
            elif recorded != current:
                restamped[relative_path] = current
        await run_blocking_until_complete(
            partial(
                run.collection.reconcile,
                drop=sorted(dropped),
                restamp=restamped,
                revision=round_revision,
                total_files=len(present),
                rebuild=run.rebuild,
            ),
        )
        for relative_path in dropped:
            run.completed.pop(relative_path, None)
            run.failed.discard(relative_path)
        run.completed.update(restamped)
        if restamped:
            logger.info(
                "Kept knowledge candidate vectors whose content is unchanged",
                base_id=self.base_id,
                count=len(restamped),
            )
        pending = tuple(
            file_path
            for relative_path, (_signature, file_path) in sorted(signatures.items())
            if relative_path not in run.completed
        )
        return present, pending

    async def _candidate_matches_source(
        self,
        round_revision: str | None,
        candidate_signatures: Mapping[str, FileSignature],
        candidate_source_signature: str,
    ) -> bool:
        """Return whether the candidate still matches the source after one pass.

        Two independent things must hold. The source must not have moved while the
        pass ran, and the candidate must cover every managed file: a file whose
        signature scan or read failed is dropped from the pass's own completeness
        accounting, so without a coverage check a transient I/O error would publish
        a silently truncated index -- and the unchanged fast path would then
        republish it at the same revision forever.

        For a local base, the corpus signature proves both at once: it re-lists
        the files, and reads only those whose ctime or size no longer matches the
        candidate's signature. On a network filesystem that stat may come from the
        client's attribute cache, so a change in the last moments of a pass can
        surface at the next refresh instead. For a Git checkout the revision proves
        content, because the checkout is program-owned and realigned with a hard
        reset, and re-listing proves coverage. Neither reads unchanged contents.
        """
        if round_revision is None:
            return await self.source_signature(candidate_signatures) == candidate_source_signature

        if await self.git_source.head() != round_revision:
            return False
        current_files = await asyncio.to_thread(self.list_files)
        return {self._relative_path(path) for path in current_files} == set(candidate_signatures)

    async def _advance_candidate(self, run: _CandidateRun, indexer: FileIndexer, progress: _CandidateProgress) -> None:
        """Reconcile, index and publish until the candidate matches the live source."""

        def record_file() -> None:
            progress.completed = len(run.completed)
            progress.failed = len(run.failed)
            progress.retrying = indexer.retry_count
            progress.maybe_log()

        for round_index in range(_MAX_CANDIDATE_RECONCILE_ROUNDS):
            round_revision = await self.git_source.head() if self.git_source.is_configured() else None
            changed_files = (
                await self.git_source.changed_files_between(run.baseline_revision, round_revision)
                if round_index == 0 and self.git_source.is_configured()
                else None
            )
            expected, pending = await self._reconcile_candidate(
                run,
                round_revision=round_revision,
                changed_files=changed_files,
            )
            run.baseline_revision = round_revision
            progress.total = len(expected)
            progress.completed = len(run.completed)

            if pending:
                progress.indexed_this_run += await indexer.index_files(
                    pending,
                    completed=run.completed,
                    failed=run.failed,
                    vanished=run.vanished,
                    on_file_done=record_file,
                )
                progress.completed = len(run.completed)
                progress.failed = len(run.failed)

            expected_paths = set(expected) - run.vanished
            if expected_paths - set(run.completed):
                summary = f"Indexed {len(run.completed)} of {len(expected)} managed knowledge files"
                if indexer.first_error is not None:
                    summary = f"{summary} (first error: {indexer.first_error})"
                run.record_error(summary)
                return

            candidate_signatures = {
                path: signature for path, signature in run.completed.items() if path in expected_paths
            }
            candidate_source_signature = corpus_signature(candidate_signatures)
            if not await self._candidate_matches_source(
                round_revision,
                candidate_signatures,
                candidate_source_signature,
            ):
                # The source moved while this pass ran. Keep every unchanged
                # vector and reconcile the delta instead of discarding the
                # candidate; only the changed files are re-embedded.
                logger.info(
                    "Knowledge source changed during refresh; reconciling candidate",
                    base_id=self.base_id,
                    collection=run.collection.name,
                )
                continue

            await self._publish_candidate(run, candidate_signatures, candidate_source_signature)
            return

        run.record_error(
            "Knowledge source kept changing during refresh; candidate progress was kept for the next refresh",
        )

    async def _publish_candidate(
        self,
        run: _CandidateRun,
        candidate_signatures: Mapping[str, FileSignature],
        source_signature: str,
    ) -> None:
        """Publish the verified candidate, then delete what it supersedes.

        The candidate is marked complete before the publication names it, while no reader can see it yet, and is
        never written again while published. The collection it retires is kept for the next refresh to take over,
        unless this is a forced rebuild, which keeps none; the publication names the kept one, so a leftover that an
        interrupted cleanup spared is never mistaken for it.
        """
        storage = self._base_storage_path
        retired = run.retired
        kept = (
            retired.collection
            if retired is not None and not run.rebuild and retired.settings == self._indexing_settings
            else None
        )
        await run_blocking_until_complete(run.collection.mark_complete)
        publication = new_publication(
            settings=self._indexing_settings,
            collection=run.collection.name,
            indexed_count=len(candidate_signatures),
            source_signature=source_signature,
            published_revision=run.baseline_revision,
            retired=kept,
        )

        def publish() -> None:
            save_publication(storage, publication)
            run.published = True

        await run_blocking_until_complete(publish)
        await run_blocking_until_complete(
            partial(
                cleanup_superseded_collections,
                self._collections,
                preserved=frozenset(name for name in (run.collection.name, kept) if name is not None),
            ),
        )
