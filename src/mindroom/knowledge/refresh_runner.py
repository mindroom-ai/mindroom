"""One knowledge refresh, run in a child interpreter, and the runtime side that starts it and records its outcome.

The child holds the base's source-root lock and alone writes the publication. It reports what it did; the runtime
alone records that in the base's refresh status, so the two never write the same file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import sys
import tempfile
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

from pydantic import TypeAdapter

from mindroom.background_tasks import create_background_task, run_blocking_until_complete, run_coroutine_until_complete
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.constants import (
    DEFAULT_KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_SECONDS,
    KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_ENV,
    RuntimePaths,
    resolve_runtime_paths,
    runtime_env_values,
)
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.embeddings.embedder_health import (
    EmbedderHealthRecorder,
    capture_embedder_health_recorder,
    check_embedder_health,
    embedder_in_use,
    get_embedder_failure,
)
from mindroom.embeddings.errors import extract_classified_embedder_detail
from mindroom.knowledge.github_app_auth import (
    GitHubAppTokenBinding,
    get_runtime_github_app_token_provider,
)
from mindroom.knowledge.index_metadata import (
    Publication,
    load_publication,
    load_refresh_status,
    new_publication,
    publication_unreadable,
    save_publication,
    utc_now,
)
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.redaction import redact_credentials_in_text
from mindroom.knowledge.refresh_locks import refresh_source_root_lock
from mindroom.knowledge.registry import (
    PublishedIndexKey,
    index_availability,
    mark_knowledge_source_changed_async,
    mark_published_index_stale,
    published_collection_exists,
    published_index_storage_path,
    record_refresh_failed,
    record_refresh_succeeded,
    refresh_reason_generation,
    resolve_published_index_key,
    resolve_refresh_target,
    source_root_for_published_index_key,
    source_root_for_refresh_target,
)
from mindroom.logging_config import get_logger, setup_subprocess_logging, subprocess_logging_env
from mindroom.runtime_resolution import resolve_knowledge_binding
from mindroom.tool_system.worker_routing import (
    SerializedToolExecutionIdentity,
    ToolExecutionIdentity,
    parse_tool_execution_identity_payload,
    serialize_tool_execution_identity,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.knowledge.availability import KnowledgeAvailability


logger = get_logger(__name__)


@dataclass(frozen=True)
class KnowledgeRefreshResult:
    """Result of one explicit knowledge refresh, as recorded by the runtime."""

    indexed_count: int
    index_published: bool
    availability: KnowledgeAvailability
    last_error: str | None = None


@dataclass(frozen=True)
class _RefreshChildResult:
    """What one refresh child did, reported to the runtime that started it."""

    indexed_count: int = 0
    published: bool = False
    #: Why the refresh failed, credential-redacted, or None.
    error: str | None = None
    #: A content-gated base with no files yet, which publishes nothing.
    deferred: bool = False
    #: Why other bases over the same source must refresh too, when this refresh found the source changed.
    source_changed: str | None = None


_child_result_adapter = TypeAdapter(_RefreshChildResult)


class _RefreshChildFailedError(RuntimeError):
    """A refresh child that could not start or ended without reporting a result."""

    def __init__(self, message: str, *, started: bool) -> None:
        super().__init__(message)
        #: Whether the child ran, so it may have published before it died.
        self.started = started


@dataclass(frozen=True)
class _SubprocessGitHubAppToken:
    token: str = field(repr=False)
    expires_at_epoch: int
    binding: GitHubAppTokenBinding


@dataclass(frozen=True)
class _SubprocessRefreshRequest:
    base_id: str
    config_data: dict[str, object]
    config_path: str
    storage_root: str
    runtime_knowledge_base: dict[str, object] | None = None
    execution_identity: SerializedToolExecutionIdentity | None = None
    github_app_token: _SubprocessGitHubAppToken | None = None
    force_reindex: bool = False


class _SubprocessSessionKwargs(TypedDict, total=False):
    start_new_session: bool


def _refresh_subprocess_timeout_seconds(runtime_paths: RuntimePaths) -> float:
    """Return how long one refresh child may run before it is killed."""
    raw_value = runtime_paths.env_value(KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_ENV)
    if raw_value is None:
        return DEFAULT_KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_SECONDS
    try:
        value = float(raw_value)
    except ValueError as exc:
        msg = f"{KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_ENV} must be a number, got {raw_value!r}"
        raise ValueError(msg) from exc
    if not math.isfinite(value) or value <= 0:
        msg = f"{KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_ENV} must be a finite number greater than 0, got {value}"
        raise ValueError(msg)
    return value


_REFRESH_SUBPROCESS_THREAD_ENV = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "TOKENIZERS_PARALLELISM": "false",
}


async def _github_app_refresh_binding(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str, dict[str, Any]] | None:
    git_config = config.get_knowledge_base_config(base_id).git
    if git_config is None or git_config.credentials_service is None:
        return None
    credentials_manager = get_runtime_shared_credentials_manager(runtime_paths)
    credentials = (
        await asyncio.to_thread(
            credentials_manager.load_credentials,
            git_config.credentials_service,
        )
        or {}
    )
    if credentials.get("auth_type") != "github_app":
        return None
    return git_config.repo_url, credentials


async def _resolve_subprocess_github_app_token(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> _SubprocessGitHubAppToken | None:
    binding = await _github_app_refresh_binding(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
    )
    if binding is None:
        return None
    repo_url, credentials = binding
    provider = get_runtime_github_app_token_provider()
    resolved = await provider.resolve_token(repo_url, credentials)
    return _SubprocessGitHubAppToken(
        token=resolved.token,
        expires_at_epoch=int(resolved.expires_at.timestamp()),
        binding=provider.binding_for(repo_url, credentials),
    )


async def refresh_knowledge_binding_in_subprocess(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    force_reindex: bool = False,
    health_recorder: EmbedderHealthRecorder | None = None,
) -> KnowledgeRefreshResult:
    """Run one knowledge refresh in a child interpreter and record its outcome in the base's refresh status.

    Manual and scheduled refreshes share this boundary. A subprocess keeps
    vector store, embedding, Git, and reader CPU/I/O away from the
    control-plane event loop and its shared thread pool while preserving the
    last-good published index semantics.
    """
    key = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    recorder = health_recorder or capture_embedder_health_recorder()
    storage = published_index_storage_path(key)
    reason_generation = await asyncio.to_thread(refresh_reason_generation, key)
    publication_before = await asyncio.to_thread(load_publication, storage)
    try:
        result = await _run_refresh_child(
            key,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            force_reindex=force_reindex,
        )
    except asyncio.CancelledError:
        await run_blocking_until_complete(
            _record_refresh_without_result,
            key,
            publication_before,
            reason_generation,
            None,
        )
        raise
    except _RefreshChildFailedError as failure:
        error = redact_credentials_in_text(str(failure))
        if failure.started:
            await run_blocking_until_complete(
                _record_refresh_without_result,
                key,
                publication_before,
                reason_generation,
                error,
            )
        else:
            await run_blocking_until_complete(partial(record_refresh_failed, key, error=error))
        _schedule_refresh_health_check(key, config, runtime_paths, recorder, error=error)
        raise
    await run_blocking_until_complete(_record_child_result, key, result, reason_generation)
    if result.source_changed is not None:
        await mark_knowledge_source_changed_async(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            reason=result.source_changed,
            excluding=key,
        )
    _schedule_refresh_health_check(key, config, runtime_paths, recorder, error=result.error)
    publication = await asyncio.to_thread(load_publication, storage)
    return KnowledgeRefreshResult(
        indexed_count=result.indexed_count,
        index_published=result.published,
        availability=index_availability(
            key,
            publication,
            await asyncio.to_thread(load_refresh_status, storage),
            publication_unreadable=await asyncio.to_thread(publication_unreadable, storage, publication),
            refreshing=False,
        ),
        last_error=result.error,
    )


def _record_child_result(key: PublishedIndexKey, result: _RefreshChildResult, reason_generation: int) -> None:
    if result.published:
        record_refresh_succeeded(key, reason_generation=reason_generation)
    elif result.deferred:
        mark_published_index_stale(key, reason="source_empty")
    elif result.error is not None:
        record_refresh_failed(key, error=result.error)


def _record_refresh_without_result(
    key: PublishedIndexKey,
    publication_before: Publication | None,
    reason_generation: int,
    error: str | None,
) -> None:
    """Record a child that ran but reported nothing: it published if the publication changed, else it failed.

    Within one runtime no other refresh of this base runs meanwhile, so a changed publication is this child's. A
    cancelled child that did not publish leaves the status as it was.
    """
    publication = load_publication(published_index_storage_path(key))
    if publication is not None and publication != publication_before:
        record_refresh_succeeded(key, reason_generation=reason_generation)
    elif error is not None:
        record_refresh_failed(key, error=error)


def _schedule_refresh_health_check(
    key: PublishedIndexKey,
    config: Config,
    runtime_paths: RuntimePaths,
    health_recorder: EmbedderHealthRecorder,
    *,
    error: str | None,
) -> None:
    """Restore the runtime's provider health after a refresh child failed on, or worked with, the embedder."""
    if embedder_in_use(config):
        create_background_task(
            _check_embedder_after_refresh(config, runtime_paths, health_recorder, error=error),
            name=f"embedder_refresh_health_check:{key.base_id}",
        )


async def _check_embedder_after_refresh(
    config: Config,
    runtime_paths: RuntimePaths,
    health_recorder: EmbedderHealthRecorder,
    *,
    error: str | None,
) -> None:
    if not health_recorder.is_current():
        return
    if extract_classified_embedder_detail(error) is not None:
        reason = "knowledge_refresh_failed"
    elif error is None and get_embedder_failure() is not None:
        reason = "knowledge_refresh_recovery"
    else:
        return
    await check_embedder_health(config, runtime_paths, reason=reason, health_recorder=health_recorder)


async def _run_refresh_child(
    key: PublishedIndexKey,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    force_reindex: bool,
) -> _RefreshChildResult:
    """Run the child and return what it reported; raise ``_RefreshChildFailedError`` when it reported nothing."""
    base_id = key.base_id
    # A file avoids pipe backpressure or waiting for EOF from a Git helper
    # before the process-group cleanup can terminate that helper.
    with tempfile.TemporaryFile() as output:
        try:
            github_app_token = await _resolve_subprocess_github_app_token(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
            )
            request_payload = await asyncio.to_thread(
                _serialize_subprocess_refresh_request,
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                github_app_token=github_app_token,
                force_reindex=force_reindex,
            )
            # Resolved before the spawn so a malformed window rejects the refresh
            # instead of leaving a child nobody is waiting on.
            timeout = _refresh_subprocess_timeout_seconds(runtime_paths)
            env = dict(runtime_env_values(runtime_paths))
            env.setdefault("PATH", os.environ.get("PATH") or os.defpath)
            env.update(_REFRESH_SUBPROCESS_THREAD_ENV)
            env.update(subprocess_logging_env())
            env["MINDROOM_KNOWLEDGE_REFRESH_SUBPROCESS"] = "1"
            # Parent and child derive the cross-process refresh lock from their temp
            # root, so the child shares this one whatever the runtime env names.
            env["TMPDIR"] = tempfile.gettempdir()
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "mindroom.knowledge.refresh_entrypoint",
                stdin=asyncio.subprocess.PIPE,
                stdout=output,
                env=env,
                **_subprocess_session_kwargs(),
            )
        except Exception as exc:
            raise _RefreshChildFailedError(str(exc), started=False) from exc
        try:
            async with asyncio.timeout(timeout):
                with suppress(BrokenPipeError, ConnectionResetError):
                    await _send_subprocess_refresh_request(process, request_payload)
                return_code = await process.wait()
        except TimeoutError:
            # A refresh child can wedge below Python: torch's Metal shader-library
            # caches are unlocked, and a corrupted lookup spins forever. The child
            # gets its own session, so nothing else would ever reap it.
            msg = f"Knowledge refresh subprocess for {base_id!r} timed out after {timeout}s and was terminated"
            logger.warning(msg, base_id=base_id)
            await run_coroutine_until_complete(_terminate_refresh_subprocess(process))
            raise _RefreshChildFailedError(msg, started=True) from None
        except asyncio.CancelledError:
            await run_coroutine_until_complete(_terminate_refresh_subprocess(process))
            raise
        await run_coroutine_until_complete(_terminate_refresh_subprocess(process))
        output.seek(0)
        reported = output.read()
        if return_code != 0 or not reported:
            msg = f"Knowledge refresh subprocess failed for {base_id!r} with exit code {return_code}"
            raise _RefreshChildFailedError(msg, started=True)
        return _child_result_adapter.validate_json(reported)


def _serialize_subprocess_refresh_request(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    github_app_token: _SubprocessGitHubAppToken | None = None,
    force_reindex: bool,
) -> bytes:
    runtime_knowledge_base = config.runtime_knowledge_base_overlay(base_id)
    payload = _SubprocessRefreshRequest(
        base_id=base_id,
        config_data=config.authored_model_dump(),
        config_path=str(runtime_paths.config_path),
        storage_root=str(runtime_paths.storage_root),
        runtime_knowledge_base=(
            None
            if runtime_knowledge_base is None
            else cast("dict[str, object]", runtime_knowledge_base.model_dump(mode="json", exclude_unset=True))
        ),
        execution_identity=None
        if execution_identity is None
        else serialize_tool_execution_identity(execution_identity),
        github_app_token=github_app_token,
        force_reindex=force_reindex,
    )
    serialized_payload = asdict(payload)
    if github_app_token is None:
        del serialized_payload["github_app_token"]
    return json.dumps(serialized_payload, sort_keys=True).encode()


async def _send_subprocess_refresh_request(
    process: asyncio.subprocess.Process,
    payload: bytes,
) -> None:
    if process.stdin is None:
        msg = "Knowledge refresh subprocess was created without stdin"
        raise RuntimeError(msg)
    process.stdin.write(payload)
    await process.stdin.drain()
    process.stdin.close()
    with suppress(BrokenPipeError, ConnectionResetError):
        await process.stdin.wait_closed()


def _subprocess_session_kwargs() -> _SubprocessSessionKwargs:
    if os.name == "nt":
        return {}
    return {"start_new_session": True}


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_process_group_exit(process_group_id: int, *, wait_seconds: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    # There is no asyncio readiness primitive for POSIX process-group exit.
    while _process_group_exists(process_group_id) and loop.time() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    if _process_group_exists(process_group_id):
        logger.warning(
            "Knowledge refresh process group survived termination wait",
            process_group_id=process_group_id,
            wait_seconds=wait_seconds,
        )


async def _terminate_refresh_subprocess(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()
        return

    process_group_id = process.pid
    with suppress(ProcessLookupError):
        os.killpg(process_group_id, signal.SIGTERM)
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process_group_id, signal.SIGKILL)
            await process.wait()
            await _wait_for_process_group_exit(process_group_id)
            return
    if _process_group_exists(process_group_id):
        with suppress(ProcessLookupError):
            os.killpg(process_group_id, signal.SIGKILL)
        await _wait_for_process_group_exit(process_group_id)


@asynccontextmanager
async def knowledge_binding_mutation_lock(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    create: bool = False,
) -> AsyncIterator[None]:
    """Hold the source-root lock a refresh child holds, so a dashboard mutation never overlaps a refresh."""
    key = resolve_refresh_target(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=create,
    )
    source_root = source_root_for_refresh_target(key)
    async with refresh_source_root_lock(source_root):
        yield


async def _refresh_knowledge_binding(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    force_reindex: bool = False,
) -> _RefreshChildResult:
    """Refresh one resolved knowledge binding under its source-root lock: the refresh child's whole job."""
    key = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    async with refresh_source_root_lock(source_root_for_published_index_key(key)):
        return await _refresh_locked(
            key,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            force_reindex=force_reindex,
        )


async def _refresh_locked(
    key: PublishedIndexKey,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    force_reindex: bool,
) -> _RefreshChildResult:
    base_id = key.base_id
    manager = _knowledge_manager(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    if config.get_knowledge_base_config(base_id).mode == "files":
        return await _refresh_files_mode(manager, key)
    if await _should_defer_cold_empty_publication(manager, key):
        return _RefreshChildResult(deferred=True)
    source_changed: str | None = None
    git_head: str | None = None
    if manager.git_source.is_configured():
        git_sync_result = await manager.git_source.sync()
        if git_sync_result.updated:
            source_changed = "git_source_updated"
        else:
            git_head = git_sync_result.head
    if not force_reindex and source_changed is None:
        unchanged = await _publish_unchanged(manager, key, git_head=git_head)
        if unchanged:
            publication = await asyncio.to_thread(load_publication, published_index_storage_path(key))
            return _RefreshChildResult(
                indexed_count=0 if publication is None else publication.indexed_count,
                published=True,
            )
        if unchanged is False and not manager.git_source.is_configured():
            source_changed = "source_changed"
    outcome = await manager.reindex_all(force_reindex=force_reindex)
    return _RefreshChildResult(
        indexed_count=outcome.indexed_count,
        published=outcome.published,
        error=outcome.error,
        source_changed=source_changed,
    )


def _knowledge_manager(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> KnowledgeManager:
    binding = resolve_knowledge_binding(
        base_id,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        start_watchers=False,
        create=True,
    )
    return KnowledgeManager(
        base_id=base_id,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=binding.storage_root,
        knowledge_path=binding.knowledge_path,
    )


async def _should_defer_cold_empty_publication(manager: KnowledgeManager, key: PublishedIndexKey) -> bool:
    """Return whether a content-gated cold index has no corpus to publish yet."""
    if not manager.config.get_knowledge_base_config(manager.base_id).require_content_before_publish:
        return False
    if await asyncio.to_thread(manager.list_files):
        return False
    publication = await asyncio.to_thread(load_publication, published_index_storage_path(key))
    return publication is None or not publication.indexed_count


async def _publish_unchanged(manager: KnowledgeManager, key: PublishedIndexKey, *, git_head: str | None) -> bool | None:
    """Republish an index whose source has not changed, without opening a candidate or building an embedder.

    Returns True when it did, False when the corpus changed, and None when the publication cannot be republished as
    it is, such as after a settings change, an interrupted forced rebuild, or when its collection cannot be read.

    A Git base whose revision did not move is unchanged by construction: MindRoom owns the checkout and realigns it
    with ``git reset --hard``, and every corpus filter lives in the settings compared here. Any other base hashes its
    corpus, reading only the files whose stamp or size no longer match the published collection's claims.
    """
    storage = published_index_storage_path(key)
    publication = await asyncio.to_thread(load_publication, storage)
    if (
        publication is None
        or publication.collection is None
        or publication.settings != key.indexing_settings
        or not await asyncio.to_thread(published_collection_exists, key, publication)
        or await manager.has_unfinished_rebuild(publication)
    ):
        return None
    known = await manager.published_file_signatures(publication.collection)
    if known is None:
        return None
    needs_scan = git_head is None or publication.published_revision != git_head
    if needs_scan and await manager.source_signature(known) != publication.source_signature:
        return False
    if git_head is not None:
        # The dashboard reports this as the last successful sync.
        await run_blocking_until_complete(
            save_publication,
            storage,
            replace(publication, published_revision=git_head, last_published_at=utc_now()),
        )
    return True


async def _refresh_files_mode(manager: KnowledgeManager, key: PublishedIndexKey) -> _RefreshChildResult:
    """Publish current source metadata for a file-only base, syncing its Git checkout first when it has one."""
    published_revision: str | None = None
    source_changed: str | None = None
    if manager.git_source.is_configured():
        git_sync_result = await manager.git_source.sync()
        published_revision = git_sync_result.head
        source_changed = "git_source_updated" if git_sync_result.updated else None
    await _publish_file_mode_source_metadata(manager, key, published_revision=published_revision)
    return _RefreshChildResult(published=True, source_changed=source_changed)


async def _publish_file_mode_source_metadata(
    manager: KnowledgeManager,
    key: PublishedIndexKey,
    *,
    published_revision: str | None = None,
) -> None:
    publication = new_publication(
        settings=key.indexing_settings,
        collection=None,
        indexed_count=0,
        source_signature=await manager.source_signature(),
        published_revision=published_revision,
    )
    await run_blocking_until_complete(save_publication, published_index_storage_path(key), publication)


async def publish_file_mode_source_metadata_for_base(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> None:
    """Publish a local file-only base's current source listing; the caller holds its source-root lock.

    This runs in the runtime, after a dashboard upload or delete, so it records its success itself.
    """
    key = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    manager = _knowledge_manager(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    reason_generation = await asyncio.to_thread(refresh_reason_generation, key)
    await _publish_file_mode_source_metadata(manager, key)
    await run_blocking_until_complete(partial(record_refresh_succeeded, key, reason_generation=reason_generation))


def _load_subprocess_github_app_binding(raw_binding_payload: object) -> GitHubAppTokenBinding:
    if not isinstance(raw_binding_payload, dict):
        msg = "Knowledge refresh subprocess request github_app_token.binding must be an object"
        raise TypeError(msg)
    binding_payload = cast("dict[str, object]", raw_binding_payload)
    raw_app_id = binding_payload.get("app_id")
    raw_installation_id = binding_payload.get("installation_id")
    raw_owner = binding_payload.get("owner")
    raw_repository = binding_payload.get("repository")
    raw_private_key_file = binding_payload.get("private_key_file")
    if not isinstance(raw_app_id, int) or isinstance(raw_app_id, bool) or raw_app_id <= 0:
        msg = "Knowledge refresh subprocess request github_app_token.binding.app_id must be positive"
        raise TypeError(msg)
    if not isinstance(raw_installation_id, int) or isinstance(raw_installation_id, bool) or raw_installation_id <= 0:
        msg = "Knowledge refresh subprocess request github_app_token.binding.installation_id must be positive"
        raise TypeError(msg)
    if not isinstance(raw_owner, str) or not raw_owner:
        msg = "Knowledge refresh subprocess request github_app_token.binding.owner must be non-empty"
        raise TypeError(msg)
    if not isinstance(raw_repository, str) or not raw_repository:
        msg = "Knowledge refresh subprocess request github_app_token.binding.repository must be non-empty"
        raise TypeError(msg)
    if not isinstance(raw_private_key_file, str) or not raw_private_key_file:
        msg = "Knowledge refresh subprocess request github_app_token.binding.private_key_file must be non-empty"
        raise TypeError(msg)
    return GitHubAppTokenBinding(
        app_id=raw_app_id,
        installation_id=raw_installation_id,
        owner=raw_owner,
        repository=raw_repository,
        private_key_file=raw_private_key_file,
    )


def _load_subprocess_github_app_token(raw_token_payload: object) -> _SubprocessGitHubAppToken | None:
    if raw_token_payload is None:
        return None
    if not isinstance(raw_token_payload, dict):
        msg = "Knowledge refresh subprocess request github_app_token must be an object when present"
        raise TypeError(msg)
    token_payload = cast("dict[str, object]", raw_token_payload)
    raw_token = token_payload.get("token")
    raw_expires_at_epoch = token_payload.get("expires_at_epoch")
    if not isinstance(raw_token, str) or not raw_token:
        msg = "Knowledge refresh subprocess request github_app_token.token must be non-empty"
        raise TypeError(msg)
    if not isinstance(raw_expires_at_epoch, int) or isinstance(raw_expires_at_epoch, bool) or raw_expires_at_epoch <= 0:
        msg = "Knowledge refresh subprocess request github_app_token.expires_at_epoch must be positive"
        raise TypeError(msg)
    return _SubprocessGitHubAppToken(
        token=raw_token,
        expires_at_epoch=raw_expires_at_epoch,
        binding=_load_subprocess_github_app_binding(token_payload.get("binding")),
    )


def _load_subprocess_refresh_request(payload: bytes) -> _SubprocessRefreshRequest:
    raw_payload = json.loads(payload.decode())
    if not isinstance(raw_payload, dict):
        msg = "Knowledge refresh subprocess request must be a JSON object"
        raise TypeError(msg)
    raw_base_id = raw_payload.get("base_id")
    raw_config_data = raw_payload.get("config_data")
    raw_config_path = raw_payload.get("config_path")
    raw_storage_root = raw_payload.get("storage_root")
    raw_runtime_knowledge_base = raw_payload.get("runtime_knowledge_base")
    raw_execution_identity = raw_payload.get("execution_identity")
    raw_force_reindex = raw_payload.get("force_reindex", False)
    if not isinstance(raw_base_id, str) or not raw_base_id.strip():
        msg = "Knowledge refresh subprocess request is missing base_id"
        raise TypeError(msg)
    if not isinstance(raw_config_data, dict):
        msg = "Knowledge refresh subprocess request is missing config_data"
        raise TypeError(msg)
    if not isinstance(raw_config_path, str) or not raw_config_path.strip():
        msg = "Knowledge refresh subprocess request is missing config_path"
        raise TypeError(msg)
    if not isinstance(raw_storage_root, str) or not raw_storage_root.strip():
        msg = "Knowledge refresh subprocess request is missing storage_root"
        raise TypeError(msg)
    if raw_runtime_knowledge_base is not None and not isinstance(raw_runtime_knowledge_base, dict):
        msg = "Knowledge refresh subprocess request runtime_knowledge_base must be an object when present"
        raise TypeError(msg)
    if raw_execution_identity is not None and not isinstance(raw_execution_identity, dict):
        msg = "Knowledge refresh subprocess request execution_identity must be an object when present"
        raise TypeError(msg)
    return _SubprocessRefreshRequest(
        base_id=raw_base_id,
        config_data=raw_config_data,
        config_path=raw_config_path,
        storage_root=raw_storage_root,
        runtime_knowledge_base=cast("dict[str, object] | None", raw_runtime_knowledge_base),
        execution_identity=raw_execution_identity,
        github_app_token=_load_subprocess_github_app_token(raw_payload.get("github_app_token")),
        force_reindex=bool(raw_force_reindex),
    )


async def _prime_subprocess_github_app_token(
    request: _SubprocessRefreshRequest,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    if request.github_app_token is None:
        return
    binding = await _github_app_refresh_binding(
        request.base_id,
        config=config,
        runtime_paths=runtime_paths,
    )
    if binding is None:
        return
    try:
        expires_at = datetime.fromtimestamp(request.github_app_token.expires_at_epoch, tz=UTC)
    except (OSError, OverflowError, ValueError) as exc:
        msg = "Knowledge refresh subprocess GitHub App token expiry is invalid"
        raise TypeError(msg) from exc
    repo_url, credentials = binding
    provider = get_runtime_github_app_token_provider()
    if provider.binding_for(repo_url, credentials) != request.github_app_token.binding:
        return
    provider.prime(
        repo_url,
        credentials,
        token=request.github_app_token.token,
        expires_at=expires_at,
    )


async def _run_subprocess_refresh_request(payload: bytes) -> _RefreshChildResult:
    request = _load_subprocess_refresh_request(payload)
    runtime_paths = resolve_runtime_paths(
        config_path=Path(request.config_path),
        storage_path=Path(request.storage_root),
        process_env=dict(os.environ),
    )
    config = Config.validate_with_runtime(request.config_data, runtime_paths, tolerate_plugin_load_errors=True)
    if request.runtime_knowledge_base is not None:
        base_config = KnowledgeBaseConfig.model_validate(request.runtime_knowledge_base)
        config = config.with_runtime_knowledge_base_overlay(request.base_id, base_config)
    await _prime_subprocess_github_app_token(
        request,
        config=config,
        runtime_paths=runtime_paths,
    )
    execution_identity = (
        None
        if request.execution_identity is None
        else parse_tool_execution_identity_payload(
            request.execution_identity,
            error_prefix="Knowledge refresh execution_identity",
        )
    )
    return await _refresh_knowledge_binding(
        request.base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        force_reindex=request.force_reindex,
    )


def _parse_refresh_runner_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one internal MindRoom knowledge refresh request.")
    return parser.parse_args(argv)


def _failed_child_result(error: Exception) -> _RefreshChildResult:
    """Return what a refresh child reports for an error it did not handle, credential-redacted."""
    return _RefreshChildResult(error=redact_credentials_in_text(str(error)) or type(error).__name__)


def main(argv: list[str] | None = None) -> int:
    """Internal CLI used by scheduled knowledge refresh subprocesses."""
    _parse_refresh_runner_args(argv)
    setup_subprocess_logging()
    payload = sys.stdin.buffer.read()
    # Native libraries and configured handlers can retain stdout's descriptor.
    with os.fdopen(os.dup(sys.stdout.fileno()), "wb") as output:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        try:
            result = asyncio.run(_run_subprocess_refresh_request(payload))
        except Exception as exc:
            logger.exception("Knowledge refresh subprocess failed")
            result = _failed_child_result(exc)
        logger.info(
            "Knowledge refresh subprocess completed",
            indexed_count=result.indexed_count,
            published=result.published,
            error=result.error is not None,
        )
        output.write(_child_result_adapter.dump_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
