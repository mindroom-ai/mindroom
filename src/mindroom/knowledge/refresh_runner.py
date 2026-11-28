"""Launch knowledge refresh subprocesses and record their outcomes in the runtime.

The child holds the base's source-root lock and alone writes the publication. It reports what it did; the runtime
alone records that in the base's refresh status, so the two never write the same file.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import sys
import tempfile
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, TypedDict, cast

from pydantic import TypeAdapter

from mindroom.background_tasks import create_background_task, run_blocking_until_complete, run_coroutine_until_complete
from mindroom.constants import (
    DEFAULT_KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_SECONDS,
    KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_ENV,
    RuntimePaths,
    runtime_env_values,
)
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.embeddings.embedder_health import check_embedder_health, embedder_in_use
from mindroom.embeddings.errors import extract_classified_embedder_detail
from mindroom.embeddings.health_state import (
    EmbedderHealthRecorder,
    capture_embedder_health_recorder,
    get_embedder_failure,
)
from mindroom.github_app_auth import (
    GitHubAppTokenBinding,
    get_runtime_github_app_token_provider,
)
from mindroom.knowledge.file_listing import (
    OversizedKnowledgeFile,
    warn_oversized_knowledge_file,
)
from mindroom.knowledge.index_metadata import (
    Publication,
    load_publication,
    load_refresh_status,
    publication_unreadable,
)
from mindroom.knowledge.redaction import redact_credentials_in_text
from mindroom.knowledge.refresh_locks import refresh_source_root_lock
from mindroom.knowledge.refresh_outcome import RefreshOutcome
from mindroom.knowledge.registry import (
    PublishedIndexKey,
    index_availability,
    mark_knowledge_source_changed_async,
    mark_published_index_stale,
    published_index_storage_path,
    record_refresh_failed,
    record_refresh_succeeded,
    refresh_reason_generation,
    resolve_published_index_key,
    resolve_refresh_target,
    source_root_for_refresh_target,
)
from mindroom.logging_config import get_logger, subprocess_logging_env
from mindroom.tool_system.worker_routing import (
    SerializedToolExecutionIdentity,
    ToolExecutionIdentity,
    serialize_tool_execution_identity,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.config.main import Config
    from mindroom.knowledge.availability import KnowledgeAvailability


logger = get_logger(__name__)


@dataclass(frozen=True)
class KnowledgeRefreshResult:
    """Result of one explicit knowledge refresh, as recorded by the runtime."""

    indexed_count: int
    index_published: bool
    availability: KnowledgeAvailability
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class RefreshChildResult(RefreshOutcome):
    """What one refresh child did, reported to the runtime that started it."""

    #: The files above the read cap this refresh skipped, for the runtime to warn about once.
    oversized_files: tuple[OversizedKnowledgeFile, ...] = ()


child_result_adapter = TypeAdapter(RefreshChildResult)


class _RefreshChildFailedError(RuntimeError):
    """A refresh child that could not start or ended without reporting a result."""

    def __init__(self, message: str, *, started: bool) -> None:
        super().__init__(message)
        #: Whether the child ran, so it may have published before it died.
        self.started = started


@dataclass(frozen=True)
class SubprocessGitHubAppToken:
    """A parent-minted token and its binding, passed to the refresh child."""

    token: str = field(repr=False)
    expires_at_epoch: int
    binding: GitHubAppTokenBinding


@dataclass(frozen=True)
class SubprocessRefreshRequest:
    """The request the runtime sends to a refresh subprocess."""

    base_id: str
    config_data: dict[str, object]
    config_path: str
    storage_root: str
    runtime_knowledge_base: dict[str, object] | None = None
    execution_identity: SerializedToolExecutionIdentity | None = None
    github_app_token: SubprocessGitHubAppToken | None = None
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


# How long a stopped refresh child, and every process it started, gets to end before the rest is killed.
_TERMINATE_GRACE_SECONDS = 10.0

_REFRESH_SUBPROCESS_THREAD_ENV = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "TOKENIZERS_PARALLELISM": "false",
}


async def github_app_refresh_binding(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str, dict[str, Any]] | None:
    """Return the base's Git remote and GitHub App credentials, if configured."""
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
) -> SubprocessGitHubAppToken | None:
    binding = await github_app_refresh_binding(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
    )
    if binding is None:
        return None
    repo_url, credentials = binding
    provider = get_runtime_github_app_token_provider()
    resolved = await provider.resolve_token(repo_url, credentials)
    return SubprocessGitHubAppToken(
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
    for oversized_file in result.oversized_files:
        warn_oversized_knowledge_file(oversized_file)
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


def _record_child_result(key: PublishedIndexKey, result: RefreshChildResult, reason_generation: int) -> None:
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
) -> RefreshChildResult:
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
        return child_result_adapter.validate_json(reported)


def _serialize_subprocess_refresh_request(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    github_app_token: SubprocessGitHubAppToken | None = None,
    force_reindex: bool,
) -> bytes:
    runtime_knowledge_base = config.runtime_knowledge_base_overlay(base_id)
    payload = SubprocessRefreshRequest(
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


async def _process_group_outlives(process_group_id: int, seconds: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    # There is no asyncio readiness primitive for POSIX process-group exit.
    while _process_group_exists(process_group_id) and loop.time() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    return _process_group_exists(process_group_id)


async def _wait_for_process_group_exit(process_group_id: int, *, wait_seconds: float = 1.0) -> None:
    if await _process_group_outlives(process_group_id, wait_seconds):
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
            await asyncio.wait_for(process.wait(), timeout=_TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
        return

    process_group_id = process.pid
    with suppress(ProcessLookupError):
        os.killpg(process_group_id, signal.SIGTERM)
    # The group outlives its leader while a process the leader started runs, and that process gets the grace period too.
    if await _process_group_outlives(process_group_id, _TERMINATE_GRACE_SECONDS):
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
