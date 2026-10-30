"""Indexing one refresh's pending files into one collection: read, chunk, embed in batches, and write.

Each file's rows and its claim land in one store transaction, and a file that fails is recorded as failed, so the
collection's own tables say exactly which files it holds. The indexer lives for one refresh, so its retry count,
first error, and failure streak start fresh with it.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import (
    DEFAULT_MAX_CONCURRENT_KNOWLEDGE_FILE_INDEXES,
    KNOWLEDGE_FILE_INDEX_CONCURRENCY_ENV,
    MAX_ALLOWED_CONCURRENT_KNOWLEDGE_FILE_INDEXES,
)
from mindroom.embeddings.batching import plan_embedding_batches
from mindroom.embeddings.errors import (
    classified_embedder_error,
    embedder_credentials_rejected,
    embedder_failure_is_transient,
)
from mindroom.knowledge.chunk_context import chunk_contexts
from mindroom.knowledge.chunking import SafeFixedSizeChunking
from mindroom.knowledge.collections import KnowledgeChunk, write_file_rows
from mindroom.knowledge.corpus_signature import file_signature
from mindroom.knowledge.index_retry import EmbeddingRetryPolicy, run_with_embedding_retry
from mindroom.knowledge.readers import Document, JSONReader, MarkdownReader, Reader, ReaderFactory, TextReader
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.embeddings.batching import RefreshEmbedder
    from mindroom.knowledge.corpus_signature import FileSignature
    from mindroom.knowledge.vector_store import KnowledgeCollection

logger = get_logger(__name__)

#: Files pulled into one prepare/embed/write batch. This bounds live asyncio
#: tasks and peak memory independently of corpus size; the provider request
#: bounds are applied separately when the batch's chunks are planned.
_INDEX_FILES_PER_BATCH = 256
#: Planned provider requests one batch keeps in flight. Every file write waits
#: on the batch's vectors, so one request at a time leaves the provider idle
#: between round trips; a batch needs enough files to plan several requests.
_EMBEDDING_REQUESTS_IN_FLIGHT = 4
#: Chunk text one batch holds between reading its files and writing them. File
#: count alone does not bound memory: 256 large files can materialize far more
#: text (and far more vectors) than 256 small ones. A batch closes once its text
#: reaches this budget, so it holds at most the budget plus one file's chunks,
#: which indexing that file has to materialize anyway.
_MAX_BATCH_TEXT_BYTES = 8_000_000
#: Consecutive classified embedder rejections, with no success in between,
#: taken as proof the fault is global rather than specific to a few files.
_GLOBAL_EMBEDDER_FAILURE_STREAK = 20
_EMBEDDING_RETRY_POLICY = EmbeddingRetryPolicy()
#: Indirection point so fault-injection tests can drive backoff without waiting.
_EMBEDDING_RETRY_SLEEP: Callable[[float], Awaitable[None]] = asyncio.sleep


def max_concurrent_file_indexes(runtime_paths: RuntimePaths) -> int:
    """Return bounded file-level indexing concurrency."""
    raw_value = runtime_paths.env_value(KNOWLEDGE_FILE_INDEX_CONCURRENCY_ENV)
    if raw_value is None:
        return DEFAULT_MAX_CONCURRENT_KNOWLEDGE_FILE_INDEXES
    try:
        value = int(raw_value)
    except ValueError as exc:
        msg = f"{KNOWLEDGE_FILE_INDEX_CONCURRENCY_ENV} must be an integer, got {raw_value!r}"
        raise ValueError(msg) from exc
    if not 1 <= value <= MAX_ALLOWED_CONCURRENT_KNOWLEDGE_FILE_INDEXES:
        msg = (
            f"{KNOWLEDGE_FILE_INDEX_CONCURRENCY_ENV} must be between 1 and "
            f"{MAX_ALLOWED_CONCURRENT_KNOWLEDGE_FILE_INDEXES}, got {value}"
        )
        raise ValueError(msg)
    return value


class _MalformedJSONSourceError(Exception):
    """A JSON parser failure carrying the already-read source text."""

    def __init__(self, source_text: str, *, line: int, column: int) -> None:
        super().__init__("Malformed JSON knowledge source")
        self.source_text = source_text
        self.line = line
        self.column = column


class _FallbackAwareJSONReader(JSONReader):
    """Tag only JSON decoding failures raised inside the source reader."""

    def read(self, path: Path | IO[Any], name: str | None = None) -> list[Document]:
        try:
            return super().read(path, name=name)
        except json.JSONDecodeError as error:
            raise _MalformedJSONSourceError(error.doc, line=error.lineno, column=error.colno) from error


class _InMemoryTextReader(TextReader):
    """Chunk the malformed JSON text already retained by its parse error, with the base's text chunking.

    Only ``read`` is overridden: indexing reads synchronously on a worker thread, and ``async_read`` would reread
    the file instead of serving the retained text.
    """

    def __init__(self, source_text: str) -> None:
        super().__init__()
        self._source_text = source_text

    def read(self, file: Path | IO[Any], name: str | None = None) -> list[Document]:
        return self.chunk_document(Document(name=name or str(file), id=str(uuid.uuid4()), content=self._source_text))


class _PermanentEmbeddingError(Exception):
    """Internal signal that no further file in this refresh can be embedded.

    Raised instead of grinding one doomed provider request per remaining file
    when the embedder rejects work for a reason retrying cannot fix.
    """


class _SourceReadError(Exception):
    """A reader's failure on a file already copied, kept apart from the errors of a file that vanished."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(f"Knowledge reader failed ({type(cause).__name__})")
        self.cause = cause


@contextmanager
def _knowledge_source_snapshot(file_path: Path) -> Iterator[tuple[Path, FileSignature]]:
    """Copy one listed file into a primary-private directory, because Agno readers reopen sources by path.

    Yields the copy and the signature of the bytes copied into it.
    """
    with tempfile.TemporaryDirectory(prefix="mindroom-knowledge-") as snapshot_dir:
        snapshot = Path(snapshot_dir) / file_path.name
        yield snapshot, file_signature(file_path, snapshot)


def _knowledge_chunks(documents: Sequence[Document]) -> list[KnowledgeChunk]:
    """Turn the documents an Agno reader produced into owned chunks; reader output crosses into MindRoom only here."""
    return [
        KnowledgeChunk(text=document.content, name=document.name, metadata=dict(document.meta_data))
        for document in documents
    ]


@dataclass(frozen=True)
class _PreparedFile:
    """One listed file read and chunked once, waiting for its batch's vectors."""

    path: Path
    relative_path: str
    signature: FileSignature
    chunks: tuple[KnowledgeChunk, ...]

    def text_bytes(self) -> int:
        """Return the UTF-8 size of the chunk text this file holds in memory."""
        return sum(len(chunk.text.encode("utf-8")) for chunk in self.chunks)


@dataclass
class FileIndexer:
    """Index one refresh's pending files into one collection, keeping that refresh's failure evidence."""

    base_id: str
    config: Config
    runtime_paths: RuntimePaths
    knowledge_root: Path
    collection: KnowledgeCollection
    #: Builds the refresh's embedder at its first embedding request, so a pass with nothing to embed loads no model.
    build_embedder: Callable[[], RefreshEmbedder]
    #: Files indexed at once, from ``max_concurrent_file_indexes``.
    max_concurrent_file_indexes: int
    #: The first file failure's credential-safe detail, which the refresh error quotes.
    first_error: str | None = field(default=None, init=False)
    #: Set once the embedder provably rejects every request, which stops the refresh.
    global_failure: str | None = field(default=None, init=False)
    retry_count: int = field(default=0, init=False)
    _failure_streak: int = field(default=0, init=False, repr=False)

    @cached_property
    def embedder(self) -> RefreshEmbedder:
        """The refresh's embedder, built on the event loop the first time a file needs vectors."""
        return self.build_embedder()

    async def index_files(
        self,
        files: Sequence[Path],
        *,
        completed: dict[str, FileSignature],
        failed: set[str],
        vanished: set[str],
        on_file_done: Callable[[], None],
    ) -> int:
        """Index ``files`` in bounded batches, keeping ``completed``, ``failed``, and ``vanished`` in step with the store.

        Work is pulled batch by batch rather than fanned out over the whole list: live asyncio tasks stay bounded by
        the per-file concurrency limit regardless of corpus size. Each batch's files are read once, their distinct
        chunk texts are embedded together, and only then are the files written. Returns how many files it indexed.
        """
        settle = partial(self._settle, completed=completed, failed=failed, on_file_done=on_file_done)
        indexed_count = 0
        start = 0
        while start < len(files):
            prepared, start = await self._prepare_batch(files, start, vanished=vanished, settle=settle)
            try:
                batch_vectors = await self._embed_batch_texts(prepared)
            except _PermanentEmbeddingError:
                return indexed_count
            indexed_count += await self._index_file_batch(prepared, batch_vectors=batch_vectors, settle=settle)
            if self.global_failure is not None:
                logger.warning(
                    "Stopping knowledge refresh: the embedder is rejecting every request",
                    base_id=self.base_id,
                    detail=self.global_failure,
                )
                return indexed_count
        return indexed_count

    async def _settle(
        self,
        relative_path: str,
        signature: FileSignature | None,
        *,
        completed: dict[str, FileSignature],
        failed: set[str],
        on_file_done: Callable[[], None],
    ) -> None:
        """Record one file's outcome: claimed at ``signature``, or failed, which keeps no rows and no claim."""
        if signature is None:
            await run_blocking_until_complete(self.collection.record_failure, relative_path)
            failed.add(relative_path)
        else:
            completed[relative_path] = signature
            failed.discard(relative_path)
        on_file_done()

    def _relative_path(self, file_path: Path) -> str:
        return file_path.relative_to(self.knowledge_root).as_posix()

    def _configure_text_reader(self, reader: TextReader | MarkdownReader) -> TextReader | MarkdownReader:
        """Apply this base's text chunking policy to ``reader`` in place."""
        base_config = self.config.get_knowledge_base_config(self.base_id)
        chunking_strategy = SafeFixedSizeChunking(chunk_size=base_config.chunk_size, overlap=base_config.chunk_overlap)
        reader.chunk = True
        reader.chunk_size = chunking_strategy.chunk_size
        reader.chunking_strategy = chunking_strategy
        return reader

    def _build_reader(self, file_path: Path) -> Reader:
        """Build a per-file reader with conservative chunking for text-like content."""
        reader = ReaderFactory.get_reader_for_extension(file_path.suffix.lower())

        # ReaderFactory hands out cached shared instances, so any branch that
        # configures a reader copies it first instead of mutating the cache.
        if isinstance(reader, JSONReader):
            # Carry the factory reader's configuration (encoding, chunking) onto
            # the subclass that tags its own decode failures for the text fallback.
            return _FallbackAwareJSONReader(**deepcopy(vars(reader)))

        # Large markdown/plain-text files are the common source of oversized embed requests.
        if not isinstance(reader, (TextReader, MarkdownReader)):
            return reader

        return self._configure_text_reader(deepcopy(reader))

    def _read_file(self, file_path: Path) -> _PreparedFile | None:
        """Read and chunk one listed file through a private snapshot; None when its reader dependency is missing.

        A file that vanished or was swapped after listing raises
        ``FileNotFoundError`` or ``ValueError`` from the snapshot, and a reader
        failure raises ``_SourceReadError``, so the two never get confused.
        """
        relative_path = self._relative_path(file_path)
        try:
            reader = self._build_reader(file_path)
        except ImportError as exc:
            logger.warning(
                "Skipping knowledge file because its reader dependency is not installed",
                base_id=self.base_id,
                path=relative_path,
                extension=file_path.suffix.lower(),
                error=str(exc),
            )
            return None
        with _knowledge_source_snapshot(file_path) as (snapshot, signature):
            try:
                documents = self._read_documents(reader, snapshot, relative_path)
            except Exception as exc:
                raise _SourceReadError(exc) from exc
        return _PreparedFile(
            path=file_path,
            relative_path=relative_path,
            signature=signature,
            chunks=tuple(_knowledge_chunks(documents)),
        )

    def _read_documents(self, reader: Reader, snapshot: Path, relative_path: str) -> list[Document]:
        """Read one snapshot with its reader, rereading the retained text only for malformed source JSON."""
        try:
            return reader.read(snapshot, name=snapshot.name)
        except _MalformedJSONSourceError as error:
            logger.warning(
                "Malformed JSON knowledge file; indexing as text",
                base_id=self.base_id,
                path=relative_path,
                line=error.line,
                column=error.column,
            )
            fallback_reader = self._configure_text_reader(_InMemoryTextReader(error.source_text))
            return fallback_reader.read(snapshot, name=snapshot.name)

    async def _prepare_file(self, file_path: Path, *, vanished: set[str]) -> _PreparedFile | None:
        """Read and chunk one file on a worker thread; None when it vanished, failed to read, or lacks its reader."""
        try:
            return await asyncio.to_thread(self._read_file, file_path)
        except _SourceReadError as error:
            self._record_file_failure(file_path, error.cause)
        except (FileNotFoundError, ValueError):
            # Live source folders (e.g. thread exports) delete or replace files while
            # a refresh runs; a file vanishing or turning into a link between listing
            # and indexing is not an indexing failure. Record it so the caller can drop
            # it from its completeness accounting: the trailing
            # source-signature comparison then decides whether the
            # surviving corpus is publishable or another refresh is needed.
            relative_path = self._relative_path(file_path)
            logger.warning("Knowledge file vanished during refresh; skipping", base_id=self.base_id, path=relative_path)
            vanished.add(relative_path)
        return None

    async def _prepare_batch(
        self,
        files: Sequence[Path],
        start: int,
        *,
        vanished: set[str],
        settle: Callable[[str, FileSignature | None], Awaitable[None]],
    ) -> tuple[list[_PreparedFile], int]:
        """Read and chunk the next batch of files once; return it and where the following batch starts.

        A batch ends at the file-count limit or once its chunk text reaches the
        memory budget. Files that failed to read or lack their reader are settled
        as failed right away and never join the batch; vanished files are only noted.
        """
        prepared: list[_PreparedFile] = []
        text_bytes = 0
        end = start
        while end < len(files) and end - start < _INDEX_FILES_PER_BATCH and text_bytes < _MAX_BATCH_TEXT_BYTES:
            file_path = files[end]
            end += 1
            prepared_file = await self._prepare_file(file_path, vanished=vanished)
            if prepared_file is None:
                if (relative_path := self._relative_path(file_path)) not in vanished:
                    await settle(relative_path, None)
                continue
            prepared.append(prepared_file)
            text_bytes += prepared_file.text_bytes()
        model_name = self.config.get_knowledge_base_config(self.base_id).chunk_context_model
        if model_name is None:
            return prepared, end
        # Files get their chunk contexts concurrently, under the same per-file limit as their writes.
        slots = asyncio.Semaphore(self.max_concurrent_file_indexes)

        async def with_contexts(prepared_file: _PreparedFile) -> _PreparedFile | None:
            async with slots:
                return await self._with_chunk_contexts(prepared_file, model_name)

        contextualized: list[_PreparedFile] = []
        for prepared_file, result in zip(
            prepared,
            await asyncio.gather(*(with_contexts(prepared_file) for prepared_file in prepared)),
            strict=True,
        ):
            if result is None:
                await settle(prepared_file.relative_path, None)
            else:
                contextualized.append(result)
        return contextualized, end

    async def _with_chunk_contexts(self, prepared: _PreparedFile, model_name: str) -> _PreparedFile | None:
        """Prefix each chunk with the context the base's ``chunk_context_model`` writes; None when that fails.

        A single-chunk file is its own document, so there is nothing to situate it in.
        """
        if len(prepared.chunks) < 2:
            return prepared
        try:
            contexts = await chunk_contexts(
                self.config,
                self.runtime_paths,
                model_name,
                [chunk.text for chunk in prepared.chunks],
            )
        except Exception as exc:
            self._record_file_failure(prepared.path, exc)
            return None
        return replace(
            prepared,
            chunks=tuple(
                replace(chunk, text=f"{context}\n\n{chunk.text}" if context else chunk.text)
                for chunk, context in zip(prepared.chunks, contexts, strict=True)
            ),
        )

    async def _embed_batch_texts(self, prepared: Sequence[_PreparedFile]) -> dict[str, list[float]]:
        """Embed one batch's distinct chunk texts in as few provider requests as limits allow, several at once.

        A text left out here is embedded by its own file's write, one request
        per chunk, where a failure is retried and recorded against that file.
        Empty chunks are always left to their file, so a provider that rejects
        empty input fails that file instead of the whole batch.
        """
        vectors: dict[str, list[float]] = {}
        texts = list(dict.fromkeys(chunk.text for file in prepared for chunk in file.chunks if chunk.text))
        planned_batches = iter(plan_embedding_batches(texts))
        # Set once the batch gives up, on a permanent rejection or a request out of retries: no worker starts another
        # planned request, and a thread embedding one text per request stops between texts. Requests already running
        # finish; a permanent rejection drops their results, while after an exhausted request their vectors are kept.
        stop = threading.Event()

        async def embed_planned_batches() -> None:
            for planned_batch in planned_batches:
                if stop.is_set() or not self.embedder.batches():
                    # Without batch requests, each file embeds its own chunks,
                    # concurrently with the rest of the batch.
                    return
                try:
                    vectors.update(
                        await run_with_embedding_retry(
                            partial(asyncio.to_thread, self.embedder.embed_texts, planned_batch, stop=stop),
                            policy=_EMBEDDING_RETRY_POLICY,
                            sleep=_EMBEDDING_RETRY_SLEEP,
                            on_retry=self._record_embedding_retry,
                        ),
                    )
                except Exception as exc:
                    stop.set()
                    if not embedder_failure_is_transient(exc):
                        # Bad credentials or a wrong model will reject every
                        # request; stop now instead of grinding out one doomed
                        # request per remaining chunk, and report the failure the
                        # same way a per-file rejection would.
                        self._record_first_error(exc)
                        raise _PermanentEmbeddingError from exc
                    # Exhausted transient retries: stop batching for this batch
                    # and leave the texts no request finished to their files'
                    # writes, which retry their own chunks, so the failure is
                    # attributed to specific files and nothing already
                    # embedded is embedded again.
                    logger.warning(
                        "Falling back to per-file embedding after batch retries were exhausted",
                        base_id=self.base_id,
                        batch_items=len(planned_batch),
                        exc_info=True,
                    )

        try:
            # Each worker pulls the next planned request when its own one
            # finishes. The group cancels and awaits every worker task before a
            # permanent failure or a cancellation leaves this call.
            async with asyncio.TaskGroup() as workers:
                for _worker in range(min(_EMBEDDING_REQUESTS_IN_FLIGHT, self.max_concurrent_file_indexes)):
                    workers.create_task(embed_planned_batches())
        except* _PermanentEmbeddingError as failures:
            raise _PermanentEmbeddingError from failures
        finally:
            stop.set()
        return vectors

    async def _index_file_batch(
        self,
        batch: Sequence[_PreparedFile],
        *,
        batch_vectors: Mapping[str, list[float]],
        settle: Callable[[str, FileSignature | None], Awaitable[None]],
    ) -> int:
        """Write one prepared batch, capping live tasks at the concurrency limit."""

        async def index_one(prepared: _PreparedFile) -> bool:
            if self.global_failure is not None:
                return False
            signature = await self._index_file(prepared, batch_vectors=batch_vectors)
            await settle(prepared.relative_path, signature)
            return signature is not None

        concurrency = min(self.max_concurrent_file_indexes, len(batch))
        if concurrency <= 1:
            indexed = 0
            for prepared in batch:
                indexed += int(await index_one(prepared))
            return indexed

        semaphore = asyncio.Semaphore(concurrency)

        async def index_one_bounded(prepared: _PreparedFile) -> bool:
            async with semaphore:
                return await index_one(prepared)

        # return_exceptions=True so a failing or cancelled child cannot leave its siblings running past this call.
        results = await asyncio.gather(*(index_one_bounded(prepared) for prepared in batch), return_exceptions=True)
        first_error = next((result for result in results if isinstance(result, BaseException)), None)
        if first_error is not None:
            raise first_error
        return sum(1 for result in results if result is True)

    async def _index_file(
        self,
        prepared: _PreparedFile,
        *,
        batch_vectors: Mapping[str, list[float]],
    ) -> FileSignature | None:
        """Write one prepared file's rows and claim, returning its signature, or None when it failed.

        A file's rows are replaced only after all of its chunks embedded, so a
        transient embedding fault costs one retry of this file instead of the
        whole refresh. Embedding the chunks the batch left out and the store
        write run on worker threads, so the loop stays responsive; the write
        finishes even when the refresh is cancelled, so no write outlives the
        lock that admitted it.
        """
        if not prepared.chunks and prepared.signature[1] > 0:
            logger.warning("Indexing produced no vectors for file", base_id=self.base_id, path=prepared.relative_path)
            return None
        missing = [text for text in dict.fromkeys(chunk.text for chunk in prepared.chunks) if text not in batch_vectors]
        try:
            vectors = {**batch_vectors, **(await self._embed_each(missing) if missing else {})}
            await run_blocking_until_complete(
                write_file_rows,
                self.collection,
                prepared.relative_path,
                prepared.signature,
                [(chunk, vectors[chunk.text]) for chunk in prepared.chunks],
            )
        except Exception as exc:
            self._record_file_failure(prepared.path, exc)
            return None
        if prepared.chunks:
            self._failure_streak = 0
            # DEBUG, not INFO: a large corpus is 10^5 of these lines per refresh.
            logger.debug("Indexed knowledge file", base_id=self.base_id, path=prepared.relative_path)
        else:
            logger.debug(
                "Scanned empty knowledge file with no vectors",
                base_id=self.base_id,
                path=prepared.relative_path,
            )
        return prepared.signature

    async def _embed_each(self, texts: list[str]) -> dict[str, list[float]]:
        """Embed one file's chunk texts the batch left out, one request per text, retrying transient failures."""
        embedder = self.embedder
        return await run_with_embedding_retry(
            partial(asyncio.to_thread, lambda: dict(zip(texts, embedder.embed_each(texts), strict=True))),
            policy=_EMBEDDING_RETRY_POLICY,
            sleep=_EMBEDDING_RETRY_SLEEP,
            on_retry=self._record_embedding_retry,
        )

    def _record_file_failure(self, file_path: Path, exc: Exception) -> None:
        """Record why one file failed to index, for the refresh summary and the global-failure evidence."""
        self._record_first_error(exc)
        self._record_embedder_rejection(exc)
        logger.error("Failed to index knowledge file", base_id=self.base_id, path=str(file_path), exc_info=exc)

    def _record_first_error(self, exc: Exception) -> None:
        if self.first_error is None:
            self.first_error = classified_embedder_error(exc) or f"knowledge indexing failed ({type(exc).__name__})"

    def _record_embedding_retry(self) -> None:
        self.retry_count += 1

    def _record_embedder_rejection(self, exc: Exception) -> None:
        """Track evidence that the embedder is rejecting everything, not one file.

        Providers without a batch surface never reach the batch-embedding stop,
        so without this the same doomed request is issued once per remaining
        file. Only a successful embedding resets the streak.
        """
        classified = classified_embedder_error(exc)
        if classified is None:
            return
        self._failure_streak += 1
        # A rejected credential is global by construction; one file is proof enough.
        if embedder_credentials_rejected(exc) or self._failure_streak >= _GLOBAL_EMBEDDER_FAILURE_STREAK:
            self.global_failure = classified
