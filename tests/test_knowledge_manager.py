"""Knowledge index and refresh behavior tests."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import fcntl
import gc
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import weakref
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from io import BufferedIOBase
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from typing import TYPE_CHECKING, NoReturn, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import UnprocessableEntityError
from pydantic import ValidationError
from structlog.testing import capture_logs
from watchfiles import Change

import mindroom.knowledge.collections as knowledge_collections_module
import mindroom.knowledge.corpus_signature as knowledge_corpus_signature
import mindroom.knowledge.file_indexer as knowledge_file_indexer
import mindroom.knowledge.file_listing as knowledge_file_listing_module
import mindroom.knowledge.git_source as knowledge_git_source_module
import mindroom.knowledge.indexing_config as knowledge_indexing_config
import mindroom.knowledge.manager as knowledge_manager_module
import mindroom.knowledge.refresh_locks as knowledge_refresh_locks
import mindroom.knowledge.refresh_runner as knowledge_refresh_runner
import mindroom.knowledge.refresh_scheduler as knowledge_refresh_scheduler
import mindroom.knowledge.registry as knowledge_registry
import mindroom.knowledge.status as knowledge_status_module
import mindroom.knowledge.utils as knowledge_utils
from mindroom import file_locks
from mindroom.agent_policy import PRIVATE_KNOWLEDGE_BASE_ID_PREFIX
from mindroom.api import config_lifecycle, main
from mindroom.api import knowledge as knowledge_api
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, AgentPrivateKnowledgeConfig
from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import Config
from mindroom.config.resolution import memory_search
from mindroom.constants import (
    DEFAULT_KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_SECONDS,
    KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_ENV,
    resolve_runtime_paths,
)
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.credentials.sync import get_embedder_api_key
from mindroom.embeddings import embedder_health
from mindroom.embeddings.batching import RefreshEmbedder
from mindroom.embeddings.errors import EmbedderRequestError, openai_embedder_error
from mindroom.git_invocation import hardened_git_command, hardened_git_env
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.collections import (
    CollectionSpace,
    _owned_collection_names,
    cleanup_superseded_collections,
    open_collection,
    working_collections,
)
from mindroom.knowledge.corpus_signature import corpus_signature, scan_corpus
from mindroom.knowledge.file_indexer import FileIndexer, _PreparedFile, max_concurrent_file_indexes
from mindroom.knowledge.file_listing import (
    git_checkout_present,
    knowledge_files_from_relative_paths,
    list_git_tracked_knowledge_files,
    list_knowledge_files,
)
from mindroom.knowledge.file_memory_knowledge import (
    resolve_agent_file_memory_knowledge,
    resolve_file_memory_knowledge,
)
from mindroom.knowledge.git_source import GitKnowledgeSource, GitSyncResult
from mindroom.knowledge.github_app_auth import GitHubAppTokenProvider
from mindroom.knowledge.index_metadata import (
    Publication,
    RefreshStatus,
    load_publication,
    load_refresh_status,
    marked_stale,
    new_publication,
    publication_path,
    save_publication,
    update_refresh_status,
)
from mindroom.knowledge.index_reader import PublishedIndexReader
from mindroom.knowledge.indexing_config import IndexingSettings, indexing_settings_key, knowledge_git_dir
from mindroom.knowledge.legacy_chroma_storage import chroma_era_shared_base_ids
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.redaction import (
    credential_free_repo_url,
    credential_free_url_identity,
    redact_credentials_in_text,
)
from mindroom.knowledge.refresh_outcome import RefreshOutcome
from mindroom.knowledge.refresh_runner import (
    KnowledgeRefreshResult,
    _record_child_result,
    _RefreshChildFailedError,
    _RefreshChildResult,
    _should_defer_cold_empty_publication,
    knowledge_binding_mutation_lock,
    refresh_knowledge_binding_in_subprocess,
)
from mindroom.knowledge.refresh_runner import _refresh_knowledge_binding as run_refresh_child
from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler, runtime_refresh_scheduler
from mindroom.knowledge.registry import (
    get_published_index,
    index_refresh_state,
    mark_published_index_stale,
    published_index_storage_path,
    record_refresh_failed,
    record_refresh_succeeded,
    resolve_published_index_key,
)
from mindroom.knowledge.status import (
    get_knowledge_index_status,
    knowledge_base_refreshing,
    reconcile_knowledge_mode_transition_states,
)
from mindroom.knowledge.utils import KnowledgeAvailabilityDetail, resolve_agent_knowledge_access
from mindroom.knowledge.vector_store import KnowledgeCollection, _KnowledgeIndexTooLargeError, list_collection_names
from mindroom.knowledge.watch import KnowledgeSourceWatcher, _shared_poll_targets
from mindroom.logging_config import subprocess_logging_env
from mindroom.memory_scope_ids import agent_scope_user_id
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from tests.conftest import SETTLE_TIMEOUT_SECONDS, bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.cpu_budget_helpers import cpu_budget
from tests.knowledge_test_support import (
    _config,
    _FakeEmbedder,
    collection_meta,
    patch_vector_store,  # noqa: F401  # requested via pytestmark below
    seed_collection,
    stored_rows,
)

pytestmark = pytest.mark.usefixtures("patch_vector_store")

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Iterable, Mapping, Sequence
    from types import ModuleType
    from typing import IO

    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.corpus_signature import FileSignature

#: The spawn seam the runtime starts each refresh child through; ``real_refresh_child`` restores it.
_REAL_RUN_REFRESH_CHILD = knowledge_refresh_runner._run_refresh_child


async def _run_refresh_child_in_process(
    key: knowledge_registry.PublishedIndexKey,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    force_reindex: bool,
) -> _RefreshChildResult:
    """Run the refresh child's job in this process, reporting an exception as the child's entry point does."""
    try:
        return await run_refresh_child(
            key.base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            force_reindex=force_reindex,
        )
    except Exception as exc:
        return knowledge_refresh_runner._failed_child_result(exc)


@pytest.fixture(autouse=True)
def _in_process_refresh_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every refresh child the runtime starts in this process, unless a test asks for ``real_refresh_child``."""
    monkeypatch.setattr(knowledge_refresh_runner, "_run_refresh_child", _run_refresh_child_in_process)


@pytest.fixture
def real_refresh_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start refresh children as real subprocesses, through the production spawn seam."""
    monkeypatch.setattr(knowledge_refresh_runner, "_run_refresh_child", _REAL_RUN_REFRESH_CHILD)


async def refresh_knowledge_binding(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    force_reindex: bool = False,
) -> KnowledgeRefreshResult:
    """Refresh one base as the runtime does, recording its outcome, with the refresh child run in this process."""
    return await refresh_knowledge_binding_in_subprocess(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        force_reindex=force_reindex,
    )


def _storage(
    config: Config,
    runtime_paths: RuntimePaths,
    base_id: str = "docs",
    *,
    execution_identity: ToolExecutionIdentity | None = None,
) -> Path:
    """Return the private storage directory holding one base's publication, refresh status, and collections."""
    return published_index_storage_path(
        resolve_published_index_key(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        ),
    )


def _publication(
    config: Config,
    runtime_paths: RuntimePaths,
    base_id: str = "docs",
    *,
    execution_identity: ToolExecutionIdentity | None = None,
) -> Publication | None:
    return load_publication(_storage(config, runtime_paths, base_id, execution_identity=execution_identity))


def _refresh_status(
    config: Config,
    runtime_paths: RuntimePaths,
    base_id: str = "docs",
    *,
    execution_identity: ToolExecutionIdentity | None = None,
) -> RefreshStatus:
    return load_refresh_status(_storage(config, runtime_paths, base_id, execution_identity=execution_identity))


def _refresh_state_for_key(key: knowledge_registry.PublishedIndexKey) -> str:
    """Return the dashboard refresh state the persisted files give one base while no refresh runs."""
    storage = published_index_storage_path(key)
    return index_refresh_state(
        load_refresh_status(storage),
        publication_unreadable=load_publication(storage) is None and publication_path(storage).exists(),
        refreshing=False,
    )


def _publish(
    key: knowledge_registry.PublishedIndexKey,
    *,
    collection: str | None,
    settings: IndexingSettings | None = None,
    indexed_count: int = 1,
    source_signature: str = "source-signature",
    published_revision: str | None = None,
) -> Publication:
    """Write a publication for ``key`` the way a refresh child does, and return it."""
    publication = new_publication(
        settings=key.indexing_settings if settings is None else settings,
        collection=collection,
        indexed_count=indexed_count,
        source_signature=source_signature,
        published_revision=published_revision,
    )
    save_publication(published_index_storage_path(key), publication)
    return publication


def _backdate_last_refresh(key: knowledge_registry.PublishedIndexKey, when: str = "2000-01-01T00:00:00+00:00") -> None:
    """Make the base's publication and its last refresh look ``when`` old, so its poll interval has passed."""
    storage = published_index_storage_path(key)
    publication = load_publication(storage)
    assert publication is not None
    save_publication(storage, replace(publication, last_published_at=when))
    update_refresh_status(storage, lambda status: replace(status, last_refresh_at=when))


def _forbid_corpus_scans(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    """Fail the test if anything signs the corpus."""

    def _unexpected_signature(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError(message)

    monkeypatch.setattr(knowledge_manager_module, "scan_corpus", _unexpected_signature)
    monkeypatch.setattr(knowledge_corpus_signature, "scan_corpus", _unexpected_signature)


def _published_collection(manager: KnowledgeManager) -> KnowledgeCollection:
    """Return the collection the manager's base publishes."""
    publication = load_publication(manager._base_storage_path)
    assert publication is not None
    assert publication.collection is not None
    return open_collection(manager._collections, publication.collection)


def _working_collections(
    config: Config,
    runtime_paths: RuntimePaths,
    base_id: str = "docs",
) -> list[KnowledgeCollection]:
    """Return the base's owned collections its publication does not name: its candidates."""
    manager = KnowledgeManager(base_id, config=config, runtime_paths=runtime_paths)
    publication = load_publication(manager._base_storage_path)
    published = None if publication is None else publication.collection
    return [
        open_collection(manager._collections, name)
        for name in _owned_collection_names(manager._collections)
        if name != published
    ]


def _source_has_vectors(manager: KnowledgeManager, relative_path: str) -> bool:
    """Return whether the manager's published collection holds a row for one source path."""
    published = _published_collection(manager)
    return published.exists() and any(
        row["metadata"].get("source_path") == relative_path for row in stored_rows(published)
    )


async def _wait_until(condition: Callable[[], bool], failure: str) -> None:
    """Poll `condition` until it holds, and fail with `failure` if the settle deadline passes first."""
    try:
        async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
            while not condition():  # noqa: ASYNC110 - the states polled here are plain attributes
                await asyncio.sleep(0.01)
    except TimeoutError:
        pytest.fail(failure)


def _test_indexing_settings(base_id: str = "docs") -> IndexingSettings:
    return IndexingSettings(
        base_id=base_id,
        storage_root="storage",
        knowledge_path=f"knowledge/{base_id}",
        mode="semantic",
        embedder_provider="openai",
        embedder_model="text-embedding-3-small",
        embedder_host="",
        embedder_dimensions="",
        chunk_size="5000",
        chunk_overlap="0",
        repo_identity="",
        git_branch="",
        git_lfs="",
        git_skip_hidden="",
        git_include_patterns="",
        git_exclude_patterns="",
        include_patterns="()",
        exclude_patterns="()",
        include_extensions="",
        exclude_extensions="()",
        extra_extensions="()",
    )


class _RecordingScheduler(KnowledgeRefreshScheduler):
    """The real scheduler's on-access cooldowns, with each refresh it would start recorded on ``schedule_refresh``."""

    def __init__(self, *, refreshing: bool = False) -> None:
        super().__init__()
        self.is_refreshing = MagicMock(return_value=refreshing)
        self.schedule_refresh = MagicMock()

    def _schedule(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None,
        force_reindex: bool = False,
        waiter: asyncio.Future[KnowledgeRefreshResult] | None = None,
    ) -> None:
        assert not force_reindex
        assert waiter is None
        self.schedule_refresh(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )


def _publish_api_config(api_app: object, config: Config) -> None:
    context = main._app_context(api_app)
    context.config_data = config.authored_model_dump()
    context.runtime_config = config
    context.config_load_result = main.ConfigLoadResult(success=True)


def test_a_files_mode_publication_loads_without_a_collection(tmp_path: Path) -> None:
    """A files-mode publication names no collection and keeps its mode through a save and load."""
    settings = replace(_test_indexing_settings(), mode="files")
    save_publication(
        tmp_path,
        new_publication(
            settings=settings,
            collection=None,
            indexed_count=0,
            source_signature="source-signature",
            published_revision=None,
        ),
    )

    publication = load_publication(tmp_path)

    assert publication is not None
    assert publication.settings.mode == "files"
    assert publication.collection is None


def _identity(requester_id: str, *, agent_name: str = "helper") -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )


def _file_memory_config(tmp_path: Path, *agent_names: str) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={name: AgentConfig(display_name=name.title()) for name in agent_names},
            memory={"backend": "file", "search": {"mode": "semantic"}},
        ),
        runtime_paths,
    )


def _scheduled_file_memory_overlay(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str, Config]:
    scheduler = _RecordingScheduler()

    resolution = resolve_agent_knowledge_access(
        agent_name,
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )

    assert resolution.knowledge is None
    scheduler.schedule_refresh.assert_called_once()
    call = scheduler.schedule_refresh.call_args
    assert call is not None
    base_id = call.args[0]
    effective_config = call.kwargs["config"]
    assert isinstance(base_id, str)
    assert isinstance(effective_config, Config)
    return base_id, effective_config


@pytest.mark.asyncio
async def test_file_memory_overlay_query_returns_memory_markdown_hit(tmp_path: Path) -> None:
    """A published file-memory overlay should be queryable through agent knowledge."""
    config = _file_memory_config(tmp_path, "helper")
    runtime_paths = runtime_paths_for(config)
    root = agent_workspace_root_path(runtime_paths.storage_root, "helper")
    memory_file = root / "memory" / "notes.md"
    memory_file.parent.mkdir(parents=True)
    query_marker = "issue256-query-marker"
    memory_file.write_text(f"{query_marker}\n", encoding="utf-8")

    base_id, effective_config = _scheduled_file_memory_overlay("helper", config, runtime_paths)
    await refresh_knowledge_binding(base_id, config=effective_config, runtime_paths=runtime_paths)

    knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert knowledge is not None
    documents = await knowledge.asearch(query_marker, 5)

    assert any(query_marker in document.content for document in documents)
    assert any(document.meta_data.get("source_path") == "memory/notes.md" for document in documents)


@pytest.mark.asyncio
async def test_only_bases_whose_files_workers_write_limit_the_vectors_a_search_loads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker code can grow private knowledge, file memory, and shared bases in workspaces, so their searches refuse."""
    monkeypatch.setattr(knowledge_registry, "_WORKER_WRITTEN_BASE_MATRIX_BYTES", 1)
    runtime_paths = test_runtime_paths(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("shared note", encoding="utf-8")
    exports = agent_workspace_root_path(runtime_paths.storage_root, "helper") / "thread_exports"
    exports.mkdir(parents=True)
    (exports / "note.md").write_text("exported note", encoding="utf-8")
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    knowledge_bases=["docs", "exports"],
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge"),
                    ),
                ),
            },
            knowledge_bases={
                "docs": KnowledgeBaseConfig(path=str(docs), watch=False),
                "exports": KnowledgeBaseConfig(path=str(exports), watch=False),
            },
            memory={"backend": "file", "search": {"mode": "semantic"}},
        ),
        runtime_paths,
    )
    identity = _identity("@alice:localhost")
    private_base_id = config.resolve_agent("helper").policy.private_knowledge_base_id
    assert private_base_id is not None
    private_key = resolve_published_index_key(
        private_base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
        create=True,
    )
    Path(private_key.knowledge_path).mkdir(parents=True, exist_ok=True)
    (Path(private_key.knowledge_path) / "note.md").write_text("private note", encoding="utf-8")
    memory = resolve_agent_file_memory_knowledge("helper", config, runtime_paths, identity)
    assert memory is not None
    (memory.root / "memory").mkdir(parents=True)
    (memory.root / "memory" / "note.md").write_text("memory note", encoding="utf-8")
    bases = {"docs": config, "exports": config, private_base_id: config, memory.base_id: memory.config}
    for base_id, base_config in bases.items():
        await refresh_knowledge_binding(
            base_id,
            config=base_config,
            runtime_paths=runtime_paths,
            execution_identity=identity,
        )
    readers = {
        base_id: get_published_index(
            base_id,
            config=base_config,
            runtime_paths=runtime_paths,
            execution_identity=identity,
        ).reader
        for base_id, base_config in bases.items()
    }
    assert all(reader is not None for reader in readers.values())
    shared = readers.pop("docs")
    assert shared is not None

    assert [hit.content.strip() for hit in await shared.asearch("note", limit=1)] == ["shared note"]
    for reader in readers.values():
        assert reader is not None
        with pytest.raises(_KnowledgeIndexTooLargeError):
            await reader.asearch("note", limit=1)


@pytest.mark.parametrize(
    ("relative_path", "limited"),
    [
        ("agents/helper/workspace/thread_exports", True),
        ("private_instances/scope/helper_data/knowledge", True),
        ("shared_directories/team/docs", True),
        ("workers/worker-a/notes", True),
        ("agents/helper", True),
        ("agents", True),
        (".", True),
        ("..", True),
        ("agents/helper/knowledge/docs", False),
        ("knowledge_docs", False),
        ("../outside", False),
    ],
)
def test_a_base_is_limited_when_its_path_lies_in_or_holds_storage_workers_write(
    tmp_path: Path,
    relative_path: str,
    limited: bool,
) -> None:
    """The limit follows where the base's files live, not how the base is configured."""
    storage_root = tmp_path / "storage"
    knowledge_path = Path(os.path.normpath(storage_root / relative_path))

    assert knowledge_registry._workers_write_sources(knowledge_path, storage_root) is limited


@pytest.mark.asyncio
async def test_cold_file_memory_overlay_waits_for_managed_content_before_publish(tmp_path: Path) -> None:
    """A cold runtime memory base must remain initializing until its corpus exists."""
    config = _file_memory_config(tmp_path, "helper")
    runtime_paths = runtime_paths_for(config)
    root = agent_workspace_root_path(runtime_paths.storage_root, "helper")
    resolution = resolve_file_memory_knowledge(
        scope_user_id=agent_scope_user_id("helper"),
        root=root,
        config=config,
        search_config=memory_search(config, "helper"),
    )

    empty_result = await refresh_knowledge_binding(
        resolution.base_id,
        config=resolution.config,
        runtime_paths=runtime_paths,
    )
    cold_lookup = get_published_index(
        resolution.base_id,
        config=resolution.config,
        runtime_paths=runtime_paths,
    )

    assert empty_result.index_published is False
    assert empty_result.availability is KnowledgeAvailability.INITIALIZING
    assert cold_lookup.reader is None

    memory_file = root / "memory" / "notes.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text("published-after-content-exists\n", encoding="utf-8")
    populated_result = await refresh_knowledge_binding(
        resolution.base_id,
        config=resolution.config,
        runtime_paths=runtime_paths,
    )
    populated_lookup = get_published_index(
        resolution.base_id,
        config=resolution.config,
        runtime_paths=runtime_paths,
    )

    assert populated_result.index_published is True
    assert populated_result.availability is KnowledgeAvailability.READY
    assert populated_lookup.reader is not None
    assert [document.content.strip() for document in await populated_lookup.reader.asearch("published", limit=5)] == [
        "published-after-content-exists",
    ]


@pytest.mark.asyncio
async def test_file_memory_knowledge_is_cross_agent_isolated(tmp_path: Path) -> None:
    """Distinct agent workspaces must never expose each other's file-memory index."""
    config = _file_memory_config(tmp_path, "alpha", "beta")
    runtime_paths = runtime_paths_for(config)
    alpha_runtime = resolve_agent_runtime("alpha", config, runtime_paths, execution_identity=None)
    beta_runtime = resolve_agent_runtime("beta", config, runtime_paths, execution_identity=None)
    assert alpha_runtime.workspace.file_memory_path is not None
    assert beta_runtime.workspace.file_memory_path is not None
    assert alpha_runtime.workspace.file_memory_path != beta_runtime.workspace.file_memory_path
    assert not alpha_runtime.workspace.file_memory_path.is_symlink()
    assert not beta_runtime.workspace.file_memory_path.is_symlink()
    alpha_marker = "issue256-alpha-only-marker"
    beta_marker = "issue256-beta-only-marker"
    alpha_file = alpha_runtime.workspace.file_memory_path / "memory" / "notes.md"
    beta_file = beta_runtime.workspace.file_memory_path / "memory" / "notes.md"
    alpha_file.parent.mkdir(parents=True)
    beta_file.parent.mkdir(parents=True)
    alpha_file.write_text(f"{alpha_marker}\n", encoding="utf-8")
    beta_file.write_text(f"{beta_marker}\n", encoding="utf-8")

    alpha_base_id, alpha_config = _scheduled_file_memory_overlay("alpha", config, runtime_paths)
    beta_base_id, beta_config = _scheduled_file_memory_overlay("beta", config, runtime_paths)
    assert alpha_base_id != beta_base_id
    assert alpha_config.knowledge_bases[alpha_base_id].path != beta_config.knowledge_bases[beta_base_id].path
    await refresh_knowledge_binding(alpha_base_id, config=alpha_config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding(beta_base_id, config=beta_config, runtime_paths=runtime_paths)

    alpha_knowledge = resolve_agent_knowledge_access("alpha", config, runtime_paths).knowledge
    beta_knowledge = resolve_agent_knowledge_access("beta", config, runtime_paths).knowledge
    assert alpha_knowledge is not None
    assert beta_knowledge is not None
    alpha_own = await alpha_knowledge.asearch(alpha_marker, 5)
    beta_own = await beta_knowledge.asearch(beta_marker, 5)
    alpha_cross = await alpha_knowledge.asearch(beta_marker, 5)
    beta_cross = await beta_knowledge.asearch(alpha_marker, 5)

    assert any(alpha_marker in document.content for document in alpha_own)
    assert any(beta_marker in document.content for document in beta_own)
    assert all(beta_marker not in document.content for document in alpha_cross)
    assert all(alpha_marker not in document.content for document in beta_cross)


def _published_result_index(
    base_id: str,
    *,
    runtime_paths: RuntimePaths,
    **_kwargs: object,
) -> knowledge_registry.PublishedIndexResolution:
    """Publish one "result from <base_id>" document, so merged search results name the bases searched."""
    storage = runtime_paths.storage_root / "published_results" / base_id
    settings = _test_indexing_settings(base_id)
    collection = KnowledgeCollection(path=storage, name=base_id)
    seed_collection(
        collection,
        [{"content": f"result from {base_id}", "metadata": {"source_path": f"{base_id}.md"}}],
        settings=settings.to_metadata(),
    )
    publication = new_publication(
        settings=settings,
        collection=collection.name,
        indexed_count=1,
        source_signature="seeded",
        published_revision=None,
    )
    save_publication(storage, publication)
    return knowledge_registry.PublishedIndexResolution(
        key=knowledge_registry.PublishedIndexKey(
            base_id=base_id,
            storage_root=str(runtime_paths.storage_root),
            knowledge_path=str(storage),
            indexing_settings=settings,
        ),
        reader=PublishedIndexReader(path=storage, embedder=_FakeEmbedder(), settings=settings),
        publication=publication,
        status=RefreshStatus(),
        availability=KnowledgeAvailability.READY,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authored_source_count", "expected_result_count", "memory_source_expected"),
    [
        pytest.param(10, 11, True, id="all-sources-fit"),
        pytest.param(25, 20, False, id="source-count-is-capped"),
    ],
)
async def test_default_merged_search_budget_is_bounded_while_representing_sources_that_fit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authored_source_count: int,
    expected_result_count: int,
    memory_source_expected: bool,
) -> None:
    """Merged defaults represent every source only until the bounded result budget is full."""
    authored_base_ids = [f"source_{index}" for index in range(authored_source_count)]
    config = _file_memory_config(tmp_path, "helper")
    config.agents["helper"].knowledge_bases = authored_base_ids
    config.knowledge_bases.update(
        {
            base_id: KnowledgeBaseConfig(path=str(tmp_path / base_id), description=f"Source {base_id}")
            for base_id in authored_base_ids
        },
    )
    runtime_paths = runtime_paths_for(config)

    monkeypatch.setattr(knowledge_utils, "get_published_index", _published_result_index)

    knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert knowledge is not None
    documents = await knowledge.asearch("anything")

    assert len(documents) == expected_result_count
    assert (
        any(document.content.startswith("result from file_memory_agent_helper_") for document in documents)
        is memory_source_expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("only", "expected_sources"),
    [
        pytest.param(None, {"docs", "faq", "file memory"}, id="all"),
        pytest.param(("faq",), {"faq"}, id="named"),
        pytest.param(("docs", "faq"), {"docs", "faq"}, id="every-base-named"),
        pytest.param(("faq", "wiki", "notes"), {"faq"}, id="only-its-own-semantic-bases"),
        pytest.param((), set(), id="none"),
    ],
)
async def test_narrowed_agent_knowledge_searches_only_the_named_bases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    asynchronous: bool,
    only: tuple[str, ...] | None,
    expected_sources: set[str],
) -> None:
    """A narrowed resolution searches only the named semantic bases the agent has, without the file-memory overlay."""
    config = _file_memory_config(tmp_path, "helper")
    config.agents["helper"].knowledge_bases = ["docs", "faq", "notes"]
    config.knowledge_bases.update(
        {base_id: KnowledgeBaseConfig(path=str(tmp_path / base_id)) for base_id in ("docs", "faq", "wiki")},
    )
    config.knowledge_bases["notes"] = KnowledgeBaseConfig(path=str(tmp_path / "notes"), mode="files")
    runtime_paths = runtime_paths_for(config)
    monkeypatch.setattr(knowledge_utils, "get_published_index", _published_result_index)

    resolution = (
        await knowledge_utils.resolve_agent_knowledge_access_async("helper", config, runtime_paths, only=only)
        if asynchronous
        else resolve_agent_knowledge_access("helper", config, runtime_paths, only=only)
    )

    knowledge = resolution.knowledge
    documents = await knowledge.asearch("anything", 10) if knowledge is not None else []
    searched = {document.content.removeprefix("result from ") for document in documents}
    assert {
        "file memory" if base_id.startswith("file_memory_") else base_id for base_id in searched
    } == expected_sources


def _record_git_sync(
    source: GitKnowledgeSource,
    result: GitSyncResult,
    *relative_paths: str,
) -> GitSyncResult:
    """Record a faked sync's outcome the way a real one would, then return it."""
    source._tracked_relative_paths = set(relative_paths)
    return result


def test_cold_git_status_with_existing_non_checkout_dir_returns_empty_files(tmp_path: Path) -> None:
    """Cold Git status should not run git ls-files before the checkout exists."""
    knowledge_path = tmp_path / "knowledge"
    knowledge_path.mkdir()
    config = _config(
        tmp_path,
        bases={"docs": knowledge_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url="https://example.com/org/repo.git")},
    )
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))

    assert manager.list_files() == []
    assert not (knowledge_path / ".git").exists()


@pytest.mark.asyncio
async def test_git_manager_construction_does_not_probe_checkout_on_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git checkout detection during construction must stay filesystem-only."""
    knowledge_path = tmp_path / "knowledge"
    (knowledge_path / ".git").mkdir(parents=True)
    config = _config(
        tmp_path,
        bases={"docs": knowledge_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url="https://example.com/org/repo.git")},
    )

    checkout_probe = MagicMock(return_value=True)
    monkeypatch.setattr(knowledge_git_source_module, "git_checkout_present", checkout_probe)

    await asyncio.sleep(0)
    KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))
    checkout_probe.assert_not_called()


def test_missing_shared_knowledge_schedules_refresh_and_returns_none(tmp_path: Path) -> None:
    """A missing published index schedules only the referenced base."""
    config = _config(
        tmp_path,
        bases={"docs": tmp_path / "docs", "unused": tmp_path / "unused"},
        agent_bases=["docs"],
    )
    scheduler = _RecordingScheduler(refreshing=False)

    knowledge = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths_for(config),
        refresh_scheduler=scheduler,
    ).knowledge

    assert knowledge is None
    scheduler.schedule_refresh.assert_called_once()
    assert scheduler.schedule_refresh.call_args.args == ("docs",)
    assert scheduler.schedule_refresh.call_args.kwargs["config"] is config


def test_unpublished_knowledge_logs_info_and_keeps_semantic_availability_notice(tmp_path: Path) -> None:
    """A new scope is awaiting publication, not a failed lookup or completed search."""
    config = _config(tmp_path, bases={"docs": tmp_path / "docs"}, agent_bases=["docs"])

    with capture_logs() as logs:
        resolution = resolve_agent_knowledge_access("helper", config, runtime_paths_for(config))

    assert resolution.knowledge is None
    assert resolution.unavailable == {
        "docs": KnowledgeAvailabilityDetail(availability=KnowledgeAvailability.INITIALIZING, search_available=False),
    }
    notice = knowledge_utils.format_knowledge_availability_notice(resolution.unavailable)
    assert notice is not None
    assert "unavailable for semantic search" in notice
    assert "Do not claim to have searched it" in notice
    assert not [entry for entry in logs if entry["log_level"] in {"warning", "error"}]
    assert any(entry["log_level"] == "info" and entry.get("knowledge_bases") == ["docs"] for entry in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_knowledge_binding_failure_is_not_reported_as_initializing(tmp_path: Path, asynchronous: bool) -> None:
    """A rejected private binding must not look like normal first-publication delay."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    private=AgentPrivateConfig(
                        per="user",
                        root="workspace",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge"),
                    ),
                ),
            },
        ),
        runtime_paths,
    )
    base_id = config.resolve_agent("helper").policy.private_knowledge_base_id
    assert base_id is not None
    with pytest.raises(ValueError, match="requires an active execution identity"):
        get_published_index(base_id, config=config, runtime_paths=runtime_paths)

    with capture_logs() as logs:
        resolution = (
            await knowledge_utils.resolve_agent_knowledge_access_async("helper", config, runtime_paths)
            if asynchronous
            else resolve_agent_knowledge_access("helper", config, runtime_paths)
        )

    assert resolution.knowledge is None
    assert resolution.unavailable[base_id].availability is KnowledgeAvailability.REFRESH_FAILED
    assert not resolution.unavailable[base_id].search_available
    assert not [entry for entry in logs if entry["log_level"] == "info" and "knowledge_bases" in entry]
    warning = next(entry for entry in logs if entry.get("event") == "Knowledge bases not available for agent")
    assert warning["log_level"] == "warning"
    assert warning["availability"] == {base_id: "refresh_failed"}
    notice = knowledge_utils.format_knowledge_availability_notice(resolution.unavailable)
    assert notice is not None
    assert "unavailable for semantic search" in notice
    assert "initializing" not in notice


@pytest.mark.parametrize("failure", ["refresh_failed", "config_mismatch", "corrupt_metadata"])
def test_failed_knowledge_still_warns_alongside_unpublished_scope(tmp_path: Path, failure: str) -> None:
    """A cold scope must not hide a real failure in another assigned knowledge base."""
    config = _config(
        tmp_path,
        bases={"cold": tmp_path / "cold", "broken": tmp_path / "broken"},
        agent_bases=["cold", "broken"],
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("broken", config=config, runtime_paths=runtime_paths)
    if failure == "corrupt_metadata":
        metadata_path = publication_path(published_index_storage_path(key))
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text("{", encoding="utf-8")
    elif failure == "config_mismatch":
        _publish(key, collection="broken_collection", settings=replace(key.indexing_settings, chunk_size="1"))
    else:
        record_refresh_failed(key, error="refresh failed")

    with capture_logs() as logs:
        resolution = resolve_agent_knowledge_access("helper", config, runtime_paths)

    expected_availability = (
        KnowledgeAvailability.CONFIG_MISMATCH if failure == "config_mismatch" else KnowledgeAvailability.REFRESH_FAILED
    )
    assert resolution.knowledge is None
    assert resolution.unavailable["broken"].availability is expected_availability
    warning = next(entry for entry in logs if entry.get("event") == "Knowledge bases not available for agent")
    assert warning["log_level"] == "warning"
    assert warning["knowledge_bases"] == ["broken"]
    assert warning["availability"] == {"broken": expected_availability.value}


def test_file_mode_knowledge_skips_semantic_lookup_and_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File-only knowledge should not look up vectors or schedule embedding refreshes."""
    config = _config(
        tmp_path,
        bases={"docs": tmp_path / "docs"},
        agent_bases=["docs"],
        modes={"docs": "files"},
    )
    get_published_index = MagicMock(side_effect=AssertionError("semantic index lookup should be skipped"))
    monkeypatch.setattr(knowledge_utils, "get_published_index", get_published_index)
    scheduler = _RecordingScheduler(refreshing=False)

    resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths_for(config),
        refresh_scheduler=scheduler,
    )

    assert resolution.knowledge is None
    assert resolution.unavailable == {}
    get_published_index.assert_not_called()
    scheduler.is_refreshing.assert_not_called()
    scheduler.schedule_refresh.assert_not_called()


def test_initializing_knowledge_skips_duplicate_initial_load_when_scheduler_is_active(tmp_path: Path) -> None:
    """An active scheduler refresh is enough for initializing knowledge."""
    config = _config(
        tmp_path,
        bases={"docs": tmp_path / "docs", "unused": tmp_path / "unused"},
        agent_bases=["docs"],
    )
    runtime_paths = runtime_paths_for(config)
    scheduler = _RecordingScheduler(refreshing=True)

    knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths, refresh_scheduler=scheduler).knowledge

    assert knowledge is None
    scheduler.is_refreshing.assert_called_with(
        knowledge_registry.resolve_refresh_target("docs", config=config, runtime_paths=runtime_paths),
    )
    scheduler.schedule_refresh.assert_not_called()


def test_real_refresh_scheduler_without_running_loop_does_not_mark_active(tmp_path: Path) -> None:
    """Synchronous callers should not leave a binding stuck refreshing when no event loop is running."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()

    assert (
        resolve_agent_knowledge_access("helper", config, runtime_paths, refresh_scheduler=scheduler).knowledge is None
    )
    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    refresh_target = knowledge_registry.resolve_refresh_target("docs", config=config, runtime_paths=runtime_paths)

    assert scheduler.is_refreshing(refresh_target) is False


def test_refresh_scheduler_module_exports_one_concrete_scheduler_name() -> None:
    """The refresh scheduler module should expose one concrete scheduler concept."""
    assert knowledge_refresh_scheduler.KnowledgeRefreshScheduler.__name__ == "KnowledgeRefreshScheduler"
    assert not hasattr(knowledge_refresh_scheduler, "StandaloneKnowledgeRefreshScheduler")
    assert not hasattr(knowledge_refresh_scheduler, "OrchestratorKnowledgeRefreshScheduler")
    assert not hasattr(knowledge_refresh_scheduler, "PerBindingKnowledgeRefreshScheduler")


@pytest.mark.asyncio
async def test_file_mode_refresh_publishes_source_metadata_without_vector_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refreshing file-only knowledge should avoid vector collections and embedders."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("Use grep for this source.", encoding="utf-8")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        modes={"docs": "files"},
    )
    runtime_paths = runtime_paths_for(config)
    embedder_factory = MagicMock(return_value=object())
    monkeypatch.setattr(knowledge_manager_module, "create_configured_embedder", embedder_factory)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths, force_reindex=True)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    publication = load_publication(published_index_storage_path(key))

    assert result.indexed_count == 0
    assert result.index_published is True
    assert result.availability is KnowledgeAvailability.READY
    assert publication is not None
    assert publication.collection is None
    assert publication.indexed_count == 0
    assert list_collection_names(published_index_storage_path(key)) == ()
    embedder_factory.assert_not_called()


@pytest.mark.asyncio
async def test_file_mode_git_refresh_marks_same_source_semantic_alias_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File-only Git sync should stale semantic indexes that read the same checkout."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("Use grep for this source.", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"semantic_docs": docs_path, "file_docs": docs_path},
        agent_bases=["semantic_docs", "file_docs"],
        git_configs={"semantic_docs": git_config, "file_docs": git_config},
        modes={"file_docs": "files"},
    )
    runtime_paths = runtime_paths_for(config)
    semantic_key = resolve_published_index_key("semantic_docs", config=config, runtime_paths=runtime_paths)
    file_key = resolve_published_index_key("file_docs", config=config, runtime_paths=runtime_paths)
    semantic_collection = KnowledgeManager(
        "semantic_docs",
        config=config,
        runtime_paths=runtime_paths,
    )._collections._default_collection
    seed_collection(
        KnowledgeCollection(path=published_index_storage_path(semantic_key), name=semantic_collection),
        [{"content": "Use grep for this source.", "metadata": {"source_path": "guide.md"}}],
    )
    _publish(semantic_key, collection=semantic_collection, source_signature="old-source-signature")

    async def _sync_updated(self: GitKnowledgeSource) -> GitSyncResult:
        assert self.base_id == "file_docs"
        return _record_git_sync(self, GitSyncResult(head="rev-updated", updated=True), "guide.md")

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync_updated)

    result = await refresh_knowledge_binding("file_docs", config=config, runtime_paths=runtime_paths)
    file_publication = load_publication(published_index_storage_path(file_key))

    assert result.availability is KnowledgeAvailability.READY
    assert _refresh_state_for_key(semantic_key) == "stale"
    assert file_publication is not None
    assert file_publication.settings.mode == "files"
    assert file_publication.published_revision == "rev-updated"
    assert _refresh_state_for_key(file_key) == "none"


@pytest.mark.asyncio
async def test_file_mode_refresh_cancelled_after_its_publication_records_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh cancelled after its child published counts as a success, with no vector state for file-only bases."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("Use grep for this source.", encoding="utf-8")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        modes={"docs": "files"},
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    mark_published_index_stale(key, reason="source_mutated")
    published = asyncio.Event()

    async def _publish_then_hang(
        key: knowledge_registry.PublishedIndexKey,
        **kwargs: object,
    ) -> _RefreshChildResult:
        await _run_refresh_child_in_process(key, **kwargs)
        published.set()
        await asyncio.Event().wait()
        raise AssertionError

    monkeypatch.setattr(knowledge_refresh_runner, "_run_refresh_child", _publish_then_hang)
    refresh = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await asyncio.wait_for(published.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    refresh.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresh
    publication = load_publication(published_index_storage_path(key))
    status = load_refresh_status(published_index_storage_path(key))

    assert publication is not None
    assert publication.settings.mode == "files"
    assert publication.collection is None
    assert _refresh_state_for_key(key) == "none"
    assert status.reason is None
    assert status.last_refresh_at is not None


def test_file_mode_source_signature_tracks_non_semantic_files(tmp_path: Path) -> None:
    """File-only metadata should cover every managed file agents can inspect."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("Use grep for this source.", encoding="utf-8")
    diagram = docs_path / "diagram.png"
    diagram.write_bytes(b"before")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
        modes={"docs": "files"},
    )
    git_dir = knowledge_git_dir(runtime_paths_for(config).storage_root, docs_path)

    def _signature() -> str:
        scanned = scan_corpus(
            config,
            "docs",
            docs_path,
            git_dir=git_dir,
            tracked_relative_paths={"guide.md", "diagram.png"},
        )
        assert set(scanned) == {"guide.md", "diagram.png"}
        return corpus_signature({relative_path: signature for relative_path, (signature, _path) in scanned.items()})

    before = _signature()
    diagram.write_bytes(b"after")

    assert _signature() != before


def test_failed_notice_without_index_says_unavailable() -> None:
    """Cold failed knowledge must not be described as stale when no index is attached."""
    notice = knowledge_utils.format_knowledge_availability_notice(
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.REFRESH_FAILED,
                search_available=False,
            ),
        },
    )

    assert notice is not None
    assert "unavailable for semantic search this turn" in notice
    assert "may be stale" not in notice
    assert "Do not claim to have searched it." in notice


def test_failed_notice_appends_classified_last_error_cause() -> None:
    """A refresh-failed notice extracts only the classified cause from the summary."""
    notice = knowledge_utils.format_knowledge_availability_notice(
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.REFRESH_FAILED,
                search_available=False,
                last_error="Indexed 0 of 3 managed knowledge files (first error: "
                "embedder authentication failed (HTTP 401))",
            ),
        },
    )

    assert notice is not None
    assert notice.endswith("Last error: embedder authentication failed (HTTP 401)")
    assert "Indexed 0 of 3" not in notice


def test_failed_notice_never_renders_unclassified_last_error() -> None:
    """Operator-grade free text in last_error stays out of model-facing prompts."""
    notice = knowledge_utils.format_knowledge_availability_notice(
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.REFRESH_FAILED,
                search_available=False,
                last_error="git sync failed: fatal: could not read from https://token@git.example.com/repo.git",
            ),
        },
    )

    assert notice is not None
    assert "Last error" not in notice
    assert "git sync failed" not in notice
    assert "token" not in notice


def test_stale_failed_notice_appends_last_error_cause() -> None:
    """A last-good-index refresh failure still appends the persisted cause."""
    notice = knowledge_utils.format_knowledge_availability_notice(
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.REFRESH_FAILED,
                search_available=True,
                last_error="embedder endpoint unreachable",
            ),
        },
    )

    assert notice is not None
    assert "may be stale this turn" in notice
    assert notice.endswith("Last error: embedder endpoint unreachable")


def test_config_mismatch_notice_without_index_says_unavailable() -> None:
    """Cold config-mismatched knowledge must not imply stale semantic search occurred."""
    notice = knowledge_utils.format_knowledge_availability_notice(
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.CONFIG_MISMATCH,
                search_available=False,
            ),
        },
    )

    assert notice is not None
    assert "unavailable for semantic search this turn" in notice
    assert "may be stale" not in notice
    assert "Do not claim to have searched it." in notice


def test_stale_notice_without_index_says_unavailable() -> None:
    """Stale metadata without a loadable index must not imply semantic search occurred."""
    notice = knowledge_utils.format_knowledge_availability_notice(
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.STALE,
                search_available=False,
            ),
        },
    )

    assert notice is not None
    assert "unavailable for semantic search this turn" in notice
    assert "may be stale" not in notice
    assert "Do not claim to have searched it." in notice


@pytest.mark.asyncio
async def test_ready_index_access_does_not_refresh_unchanged_sources(tmp_path: Path) -> None:
    """A ready index is returned immediately without churn when sources are unchanged."""
    docs_path = tmp_path / "docs"
    unused_path = tmp_path / "unused"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("ready index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path, "unused": unused_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    scheduler = _RecordingScheduler(refreshing=False)

    knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths, refresh_scheduler=scheduler).knowledge
    second_knowledge = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    ).knowledge

    assert knowledge is not None
    assert second_knowledge is not None
    assert [document.content for document in await knowledge.asearch("index", 5)] == ["ready index"]
    scheduler.schedule_refresh.assert_not_called()
    assert len(list_collection_names(_storage(config, runtime_paths))) == 1
    assert list_collection_names(_storage(config, runtime_paths, "unused")) == ()


@pytest.mark.asyncio
async def test_shared_local_watch_index_refreshes_on_access_without_blocking_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared local bases with watch=true schedule refresh on access while serving last-good content."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("shared local old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], watch=True)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("shared local new", encoding="utf-8")
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def held_refresh(key: knowledge_registry.PublishedIndexKey, **kwargs: object) -> _RefreshChildResult:
        # The refresh stays in flight until the read below returns, so that read cannot see its publication.
        refresh_started.set()
        await release_refresh.wait()
        return await _run_refresh_child_in_process(key, **kwargs)

    monkeypatch.setattr(knowledge_refresh_runner, "_run_refresh_child", held_refresh)
    scheduler = KnowledgeRefreshScheduler()

    try:
        knowledge = resolve_agent_knowledge_access(
            "helper",
            config,
            runtime_paths,
            refresh_scheduler=scheduler,
        ).knowledge
        assert knowledge is not None
        await asyncio.wait_for(refresh_started.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
        try:
            # Below the search's own 30 s deadline, so a read that waits for the held refresh fails here.
            first_read = await asyncio.wait_for(knowledge.asearch("shared", 5), timeout=10)
        except TimeoutError:
            pytest.fail("the read waited for the in-flight refresh")
        assert [document.content for document in first_read] == ["shared local old"]
        release_refresh.set()

        try:
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                while True:
                    await asyncio.sleep(0.01)
                    refreshed = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
                    if refreshed is not None and [
                        document.content for document in await refreshed.asearch("shared", 5)
                    ] == ["shared local new"]:
                        break
        except TimeoutError:
            pytest.fail("background on-access refresh did not publish the edited local source")
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_shared_local_watch_schedule_refresh_on_access_is_throttled(tmp_path: Path) -> None:
    """A freshly refreshed local watch=true base stays READY during refresh-on-access cooldown."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("shared local old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], watch=True)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    scheduler = _RecordingScheduler(refreshing=False)
    unavailable: dict[str, KnowledgeAvailability] = {}
    unavailable_details: dict[str, KnowledgeAvailabilityDetail] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    unavailable_details.update(_resolution.unavailable)
    assert _resolution.knowledge is not None
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    assert unavailable_details == {
        "docs": KnowledgeAvailabilityDetail(
            availability=KnowledgeAvailability.STALE,
            search_available=True,
        ),
    }

    doc.write_text("shared local new", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    unavailable.clear()
    unavailable_details.clear()
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    unavailable_details.update(_resolution.unavailable)
    refreshed_knowledge = _resolution.knowledge

    assert refreshed_knowledge is not None
    assert [document.content for document in await refreshed_knowledge.asearch("shared", 5)] == [
        "shared local new",
    ]
    assert unavailable == {}
    assert unavailable_details == {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    unavailable_details.update(_resolution.unavailable)
    assert _resolution.knowledge is not None
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    unavailable_details.update(_resolution.unavailable)
    assert _resolution.knowledge is not None
    assert unavailable == {}
    assert unavailable_details == {}

    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_config_mode_round_trip_marks_semantic_index_stale_after_file_mode_edits(tmp_path: Path) -> None:
    """Config-only mode transitions must not silently revive stale semantic indexes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("semantic old", encoding="utf-8")
    semantic_config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], watch=True)
    file_config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        watch=True,
        modes={"docs": "files"},
    )
    runtime_paths = test_runtime_paths(tmp_path)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, semantic_config)

    await refresh_knowledge_binding("docs", config=semantic_config, runtime_paths=runtime_paths)
    ready_lookup = get_published_index("docs", config=semantic_config, runtime_paths=runtime_paths)
    assert ready_lookup.availability is KnowledgeAvailability.READY

    client = TestClient(main.app, base_url="http://localhost")
    response = client.put("/api/config/save", json=file_config.authored_model_dump())
    assert response.status_code == 200
    doc.write_text("semantic new", encoding="utf-8")
    response = client.put("/api/config/save", json=semantic_config.authored_model_dump())
    assert response.status_code == 200

    current_config, current_runtime_paths = config_lifecycle.read_app_committed_runtime_config(main.app)
    stale_lookup = get_published_index("docs", config=current_config, runtime_paths=current_runtime_paths)

    assert stale_lookup.availability is KnowledgeAvailability.STALE
    assert _refresh_state_for_key(stale_lookup.key) == "stale"


@pytest.mark.asyncio
async def test_shared_local_watch_file_event_marks_stale_and_schedules_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem watch events should preserve last-good reads and refresh in the background."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("watch old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], watch=True)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    event_delivered = asyncio.Event()

    async def _fake_awatch(
        *_paths: Path,
        stop_event: asyncio.Event,
        **_kwargs: object,
    ) -> AsyncIterator[set[tuple[Change, str]]]:
        yield {(Change.modified, str(doc))}
        event_delivered.set()
        await stop_event.wait()

    monkeypatch.setattr("mindroom.knowledge.watch.awatch", _fake_awatch)
    refresh_scheduler = MagicMock()
    source_watcher = KnowledgeSourceWatcher(refresh_scheduler, runtime_paths)
    publication_bytes = publication_path(_storage(config, runtime_paths)).read_bytes()

    await source_watcher.sync(config)
    await asyncio.wait_for(event_delivered.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    await source_watcher.stop()

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    unavailable_details: dict[str, KnowledgeAvailabilityDetail] = {}

    # The watcher records the change beside the publication and never rewrites the publication itself.
    assert publication_path(_storage(config, runtime_paths)).read_bytes() == publication_bytes
    assert _refresh_state_for_key(key) == "stale"
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable_details.update(_resolution.unavailable)
    assert _resolution.knowledge is not None
    refresh_scheduler.schedule_refresh.assert_called_once()
    assert refresh_scheduler.schedule_refresh.call_args.args == ("docs",)
    assert unavailable_details == {
        "docs": KnowledgeAvailabilityDetail(
            availability=KnowledgeAvailability.STALE,
            search_available=True,
        ),
    }


def _assert_poller_lifecycle_logged(
    logs: list[dict[str, object]],
    *,
    base_id: str,
    poll_interval_seconds: float,
    git: bool,
) -> None:
    """Operators grep these events, so pin their names and fields."""
    expected_fields = {"base_id": base_id, "poll_interval_seconds": poll_interval_seconds, "git": git}
    for event in ("Knowledge poller started", "Knowledge poller stopped"):
        matching = [entry for entry in logs if entry["event"] == event]
        assert len(matching) == 1
        assert {field: matching[0][field] for field in expected_fields} == expected_fields


def _polled_docs_config(tmp_path: Path, docs_path: Path, *, git: bool) -> Config:
    """Configure one shared base polled every 5 seconds through its Git source or the local poll interval."""
    if git:
        git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", poll_interval_seconds=5)
        return _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], git_configs={"docs": git_config})
    return _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], poll_intervals={"docs": 5})


@pytest.mark.asyncio
@pytest.mark.parametrize("git", [True, False], ids=["git", "local"])
async def test_knowledge_polling_waits_before_startup_refresh(tmp_path: Path, git: bool) -> None:
    """Polled shared bases should not burst refresh work immediately when runtime support starts."""
    config = _polled_docs_config(tmp_path, tmp_path / "docs", git=git)
    runtime_paths = runtime_paths_for(config)
    refresh_scheduler = MagicMock()
    source_watcher = KnowledgeSourceWatcher(refresh_scheduler, runtime_paths)

    with capture_logs() as logs:
        await source_watcher.sync(config)
        try:
            await asyncio.sleep(0)
        finally:
            await source_watcher.stop()

    refresh_scheduler.schedule_refresh.assert_not_called()
    _assert_poller_lifecycle_logged(logs, base_id="docs", poll_interval_seconds=5.0, git=git)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("git", "docs_dir"),
    [(True, "docs"), (False, "docs"), (False, "not-yet-created/docs")],
    ids=["git", "local", "local-missing-directory"],
)
async def test_knowledge_polling_repeats_after_poll_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git: bool,
    docs_dir: str,
) -> None:
    """Polled shared bases keep scheduling refreshes on their interval, and a poll target does not require its folder to exist."""
    config = _polled_docs_config(tmp_path, tmp_path / docs_dir, git=git)
    runtime_paths = runtime_paths_for(config)
    second_schedule = asyncio.Event()
    refresh_scheduler = MagicMock()

    def _record_schedule(*_args: object, **_kwargs: object) -> None:
        if refresh_scheduler.schedule_refresh.call_count == 2:
            second_schedule.set()

    refresh_scheduler.schedule_refresh.side_effect = _record_schedule
    wait_calls = 0

    async def _fake_wait_for(awaitable: Coroutine[object, object, object], **kwargs: object) -> object:
        nonlocal wait_calls
        assert kwargs == {"timeout": 5.0}
        wait_calls += 1
        if wait_calls <= 2:
            awaitable.close()
            raise TimeoutError
        return await awaitable

    monkeypatch.setattr("mindroom.knowledge.watch.asyncio.wait_for", _fake_wait_for)
    source_watcher = KnowledgeSourceWatcher(refresh_scheduler, runtime_paths)

    await source_watcher.sync(config)
    try:
        await _wait_until(second_schedule.is_set, "Poller did not schedule refresh after interval")
    finally:
        await source_watcher.stop()

    assert refresh_scheduler.schedule_refresh.call_count == 2
    assert wait_calls >= 2
    assert [call.args for call in refresh_scheduler.schedule_refresh.call_args_list] == [("docs",), ("docs",)]


@pytest.mark.asyncio
async def test_source_watcher_skips_bases_whose_folder_was_replaced(tmp_path: Path) -> None:
    """A link or file planted at one shared base's folder must not keep the other bases' watchers from starting."""
    linked = tmp_path / "mindroom_data" / "agents" / "coder" / "workspace" / "repo"
    linked.parent.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    linked.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    planted = tmp_path / "planted"
    planted.write_text("not a folder", encoding="utf-8")
    config = _config(
        tmp_path,
        bases={"docs": tmp_path / "docs", "linked": linked, "planted": planted},
        agent_bases=["docs", "linked", "planted"],
        poll_intervals={"linked": 5},
        watch=True,
    )
    source_watcher = KnowledgeSourceWatcher(MagicMock(), runtime_paths_for(config))

    with capture_logs() as logs:
        await source_watcher.sync(config)
        await source_watcher.stop()

    started = [entry["base_ids"] for entry in logs if entry["event"] == "Knowledge filesystem watcher started"]
    skipped = [
        entry["base_ids"] for entry in logs if entry["event"] == "Knowledge source not watched: its path is unusable"
    ]
    assert started == [["docs"]]
    assert not [entry for entry in logs if entry["event"] == "Knowledge poller started"]
    assert sorted(skipped) == [["linked"], ["linked"], ["planted"]]


def test_local_knowledge_without_poll_interval_starts_no_poller(tmp_path: Path) -> None:
    """Without poll_interval_seconds a local base gets no poll target, so the watcher starts no poller for it."""
    config = _config(tmp_path, bases={"docs": tmp_path / "docs"}, agent_bases=["docs"])
    assert _shared_poll_targets(config, runtime_paths_for(config)) == {}


def test_chroma_era_shared_bases_convert_smallest_first(tmp_path: Path) -> None:
    """Startup conversions run smallest base first, so most bases are searchable soon; owned bases are not listed."""
    config = _config(
        tmp_path,
        bases={"big": tmp_path / "big", "owned": tmp_path / "owned", "small": tmp_path / "small"},
        agent_bases=["big", "owned", "small"],
    )
    runtime_paths = runtime_paths_for(config)
    for base_id, indexed_count in (("big", 500), ("owned", 1), ("small", 3)):
        manager = KnowledgeManager(base_id, config=config, runtime_paths=runtime_paths)
        settings = manager._indexing_settings
        save_publication(
            manager._base_storage_path,
            new_publication(
                settings=settings if base_id == "owned" else replace(settings, vector_store=""),
                collection=manager._collections._default_collection,
                indexed_count=indexed_count,
                source_signature="published",
                published_revision=None,
            ),
        )

    assert chroma_era_shared_base_ids(config, runtime_paths) == ["small", "big"]


@pytest.mark.asyncio
async def test_source_watcher_sync_converts_chroma_era_bases_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watcher sync schedules one refresh for a base Chroma published, so its conversion starts at boot; an owned base gets none."""
    config = _config(
        tmp_path,
        bases={"legacy": tmp_path / "legacy", "owned": tmp_path / "owned"},
        agent_bases=["legacy", "owned"],
    )
    runtime_paths = runtime_paths_for(config)
    for base_id in ("legacy", "owned"):
        manager = KnowledgeManager(base_id, config=config, runtime_paths=runtime_paths)
        settings = manager._indexing_settings
        save_publication(
            manager._base_storage_path,
            new_publication(
                settings=replace(settings, vector_store="") if base_id == "legacy" else settings,
                collection=manager._collections._default_collection,
                indexed_count=1,
                source_signature="published",
                published_revision=None,
            ),
        )
    refreshed: list[str] = []
    release = asyncio.Event()

    async def _fake_refresh(base_id: str, **_kwargs: object) -> None:
        refreshed.append(base_id)
        await release.wait()

    monkeypatch.setattr("mindroom.knowledge.refresh_runner.refresh_knowledge_binding_in_subprocess", _fake_refresh)
    scheduler = KnowledgeRefreshScheduler()
    source_watcher = KnowledgeSourceWatcher(scheduler, runtime_paths)

    await source_watcher.sync(config)
    await _wait_until(lambda: bool(refreshed), "the Chroma-era base was not refreshed")
    # A config reload syncs again while the conversion runs; that must not queue a second conversion.
    await source_watcher.sync(config)
    release.set()
    await _wait_until(lambda: not scheduler._tasks, "the conversion did not finish")
    await source_watcher.stop()
    await scheduler.shutdown()

    assert refreshed == ["legacy"]


@pytest.mark.asyncio
async def test_schedule_refresh_on_access_reports_stale_while_scheduler_is_active(tmp_path: Path) -> None:
    """Due refresh-on-access remains visible as STALE even when the scheduler already has work active."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("active refresh old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], watch=True)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    scheduler = _RecordingScheduler(refreshing=True)
    unavailable: dict[str, KnowledgeAvailability] = {}
    unavailable_details: dict[str, KnowledgeAvailabilityDetail] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    unavailable_details.update(_resolution.unavailable)
    knowledge = _resolution.knowledge

    assert knowledge is not None
    assert [document.content for document in await knowledge.asearch("active", 5)] == ["active refresh old"]
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    assert unavailable_details == {
        "docs": KnowledgeAvailabilityDetail(
            availability=KnowledgeAvailability.STALE,
            search_available=True,
        ),
    }
    scheduler.schedule_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_stale_index_metadata_schedules_refresh_without_source_scan(tmp_path: Path) -> None:
    """Ready access only uses persisted metadata/source change markers, not request-time source scans."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("ready index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("ready index changed", encoding="utf-8")
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge_registry.mark_published_index_stale(key, reason="test_stale")
    knowledge_registry._published_indexes.clear()
    scheduler = _RecordingScheduler(refreshing=False)
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    second_knowledge = _resolution.knowledge

    assert knowledge is not None
    assert second_knowledge is not None
    assert [document.content for document in await knowledge.asearch("index", 5)] == ["ready index"]
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    scheduler.schedule_refresh.assert_called_once()
    assert scheduler.schedule_refresh.call_args.args == ("docs",)


@pytest.mark.asyncio
async def test_stale_index_skips_duplicate_refresh_when_scheduler_is_active(tmp_path: Path) -> None:
    """A stale index should not queue another refresh while the scheduler is already active."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("ready index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("ready index changed", encoding="utf-8")
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge_registry.mark_published_index_stale(key, reason="test_stale")
    knowledge_registry._published_indexes.clear()
    scheduler = _RecordingScheduler(refreshing=True)
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is not None
    assert [document.content for document in await knowledge.asearch("index", 5)] == ["ready index"]
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    scheduler.is_refreshing.assert_called_with(knowledge_registry.refresh_target_for_published_index_key(key))
    scheduler.schedule_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_dashboard_delete_keeps_last_good_best_effort_until_refresh(tmp_path: Path) -> None:
    """A dashboard delete marks stale but old vectors can remain visible until refresh publishes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("deleted secret", encoding="utf-8")
    (docs_path / "keep.md").write_text("kept public", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    initial_knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert initial_knowledge is not None
    assert {document.content for document in await initial_knowledge.asearch("anything", 5)} == {
        "deleted secret",
        "kept public",
    }

    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    scheduler = _RecordingScheduler(refreshing=False)
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = scheduler
    try:
        response = TestClient(main.app, base_url="http://localhost").delete("/api/knowledge/bases/docs/files/guide.md")
    finally:
        config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = None
    assert response.status_code == 200

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    record_refresh_failed(key, error="refresh failed after delete")
    stale_knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge

    assert stale_knowledge is not None
    assert {document.content for document in await stale_knowledge.asearch("anything", 5)} == {
        "deleted secret",
        "kept public",
    }
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_dashboard_replacement_upload_keeps_last_good_best_effort_until_refresh(tmp_path: Path) -> None:
    """A replacement upload marks stale but old vectors can remain visible until refresh publishes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("replaced secret", encoding="utf-8")
    (docs_path / "keep.md").write_text("kept public", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    initial_knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert initial_knowledge is not None
    assert {document.content for document in await initial_knowledge.asearch("anything", 5)} == {
        "replaced secret",
        "kept public",
    }

    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    scheduler = _RecordingScheduler(refreshing=False)
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = scheduler
    try:
        response = TestClient(main.app, base_url="http://localhost").post(
            "/api/knowledge/bases/docs/upload",
            files=[("files", ("guide.md", b"replacement content", "text/markdown"))],
        )
    finally:
        config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = None
    assert response.status_code == 200

    pending_knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert pending_knowledge is not None
    assert {document.content for document in await pending_knowledge.asearch("anything", 5)} == {
        "replaced secret",
        "kept public",
    }

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    record_refresh_failed(key, error="refresh failed after replacement")
    filtered_knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge

    assert (docs_path / "guide.md").read_text(encoding="utf-8") == "replacement content"
    assert filtered_knowledge is not None
    assert {document.content for document in await filtered_knowledge.asearch("anything", 5)} == {
        "replaced secret",
        "kept public",
    }
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_dashboard_delete_stale_write_failure_keeps_best_effort_source_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale metadata failures schedule refresh instead of stranding source deletes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("restored public", encoding="utf-8")
    config = _config(tmp_path, bases={"research": docs_path, "summary": docs_path}, agent_bases=["research", "summary"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("research", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("summary", config=config, runtime_paths=runtime_paths)
    original_save_stale = knowledge_registry.mark_published_index_stale
    stale_write_count = 0

    def _fail_second_stale_write(*args: object, **kwargs: object) -> None:
        nonlocal stale_write_count
        stale_write_count += 1
        if stale_write_count == 2:
            msg = "same-source stale write failed"
            raise RuntimeError(msg)
        original_save_stale(*args, **kwargs)

    monkeypatch.setattr(knowledge_registry, "mark_published_index_stale", _fail_second_stale_write)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    scheduler = _RecordingScheduler(refreshing=False)
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = scheduler
    try:
        with pytest.raises(RuntimeError, match="same-source stale write failed"):
            TestClient(main.app, base_url="http://localhost").delete("/api/knowledge/bases/research/files/guide.md")
    finally:
        config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = None

    assert stale_write_count == 2
    assert not (docs_path / "guide.md").exists()
    for base_id in ("research", "summary"):
        lookup = get_published_index(base_id, config=config, runtime_paths=runtime_paths)
        assert lookup.reader is not None
        assert [document.content for document in await lookup.reader.asearch("anything", limit=5)] == [
            "restored public",
        ]
    assert scheduler.schedule_refresh.call_count == 2
    assert [call.args for call in scheduler.schedule_refresh.call_args_list] == [("research",), ("summary",)]


@pytest.mark.asyncio
async def test_ready_index_access_never_recomputes_source_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """READY request lookup must not walk the corpus to recompute source signatures."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("ready index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    publication = _publication(config, runtime_paths)
    assert publication is not None
    assert publication.source_signature

    _forbid_corpus_scans(monkeypatch, "READY request lookup must not recompute knowledge source signatures")

    assert resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge is not None
    assert resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge is not None


def test_knowledge_file_listing_rejects_symlink_file_escape(tmp_path: Path) -> None:
    """A symlinked file inside the KB must not expose files outside the knowledge root."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("secret outside root", encoding="utf-8")
    try:
        (docs_path / "leak.md").symlink_to(secret)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])

    assert list_knowledge_files(config, "docs", docs_path) == []


def test_knowledge_file_listing_rejects_symlinked_directory_escape(tmp_path: Path) -> None:
    """Traversal must not follow symlinked directories out of the knowledge root."""
    docs_path = tmp_path / "docs"
    outside = tmp_path / "outside"
    docs_path.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("secret through directory", encoding="utf-8")
    try:
        (docs_path / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])

    assert list_knowledge_files(config, "docs", docs_path) == []


def _corpus_hash(
    config: Config,
    docs_path: Path,
    *,
    git_dir: Path,
    tracked_relative_paths: Iterable[str] | None = None,
) -> str:
    """Return the corpus signature a publication of the ``docs`` base records for the files on disk now."""
    scanned = scan_corpus(config, "docs", docs_path, git_dir=git_dir, tracked_relative_paths=tracked_relative_paths)
    return corpus_signature({relative_path: signature for relative_path, (signature, _path) in scanned.items()})


def _signed(config: Config, docs_path: Path, known: Mapping[str, FileSignature] | None = None) -> FileSignature:
    [(signature, _path)] = scan_corpus(config, "docs", docs_path, git_dir=docs_path / ".git", known=known).values()
    return signature


def test_a_file_signed_right_after_a_write_records_a_stamp_no_stat_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signature taken within the racy window records stamp 0, so the next scan reads the file again."""
    monkeypatch.setattr(knowledge_corpus_signature, "_RACY_WINDOW_NS", 10**18)
    docs_path = (tmp_path / "docs").resolve()
    docs_path.mkdir()
    (docs_path / "note.md").write_text("one", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])

    assert _signed(config, docs_path)[0] == 0


def test_a_same_size_rewrite_that_restores_the_mtime_is_read_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stamp is the ctime, which restoring the mtime moves, so a timestamp-preserving rewrite is not missed."""
    monkeypatch.setattr(knowledge_corpus_signature, "_RACY_WINDOW_NS", 0)
    docs_path = (tmp_path / "docs").resolve()
    docs_path.mkdir()
    note = docs_path / "note.md"
    note.write_text("one", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    signature = _signed(config, docs_path)
    written = note.stat()
    # The rewrite must land in a later timestamp tick than the signed write, as any real later edit does.
    while time.time_ns() < written.st_ctime_ns + 50_000_000:
        time.sleep(0.01)
    note.write_text("two", encoding="utf-8")
    os.utime(note, ns=(written.st_atime_ns, written.st_mtime_ns))
    assert note.stat().st_ctime_ns != written.st_ctime_ns

    assert _signed(config, docs_path, known={"note.md": signature})[2] != signature[2]


def test_a_link_swapped_onto_a_listed_file_is_not_trusted_by_its_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link swapped onto a path after listing never inherits a signature, even one matching its target's stat."""
    monkeypatch.setattr(knowledge_corpus_signature, "_RACY_WINDOW_NS", 0)
    docs_path = (tmp_path / "docs").resolve()
    docs_path.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("secret", encoding="utf-8")
    note = docs_path / "note.md"
    note.write_text("public", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    [listed] = list_knowledge_files(config, "docs", docs_path)
    note.unlink()
    note.symlink_to(secret)

    scanned = list(
        knowledge_corpus_signature._scan_file_signatures(
            [("note.md", listed)],
            {"note.md": _descriptor_signature_of(secret)},
        ),
    )

    assert scanned == []


def _descriptor_signature_of(path: Path) -> FileSignature:
    with knowledge_file_listing_module.open_knowledge_file(path) as descriptor:
        return knowledge_corpus_signature._descriptor_signature(descriptor, path)


def test_tracked_path_listing_skips_per_file_strict_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chain-vetted candidates must not pay a strict resolve walk per file.

    ``resolve(strict=True)`` re-walks every path component and ignores the
    directory guard's symlink cache, so on a network filesystem it turns one
    listing pass into several round trips per file.
    """
    docs_path = tmp_path / "docs"
    nested = docs_path / "guide"
    nested.mkdir(parents=True)
    (docs_path / "root.md").write_text("root", encoding="utf-8")
    (nested / "deep.md").write_text("deep", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    original_resolve = Path.resolve

    def _resolve(self: Path, *args: object, **kwargs: object) -> Path:
        strict = bool(args[0]) if args else bool(kwargs.get("strict", False))
        if strict:
            msg = "chain-vetted candidates must not be strictly resolved per file"
            raise AssertionError(msg)
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)

    files = knowledge_files_from_relative_paths(config, "docs", docs_path, ["root.md", "guide/deep.md"])

    assert sorted(path.name for path in files) == ["deep.md", "root.md"]


def test_tracked_path_listing_rejects_symlinked_file_escape(tmp_path: Path) -> None:
    """A symlinked tracked path must not expose files outside the knowledge root."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("secret outside root", encoding="utf-8")
    try:
        (docs_path / "leak.md").symlink_to(secret)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])

    assert knowledge_files_from_relative_paths(config, "docs", docs_path, ["leak.md"]) == []


def test_tracked_path_listing_rejects_symlinked_directory_escape(tmp_path: Path) -> None:
    """A tracked path reached through a symlinked directory must stay excluded."""
    docs_path = tmp_path / "docs"
    outside = tmp_path / "outside"
    docs_path.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("secret through directory", encoding="utf-8")
    try:
        (docs_path / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])

    assert knowledge_files_from_relative_paths(config, "docs", docs_path, ["linked/secret.md"]) == []


def test_bases_endpoint_counts_files_without_building_the_file_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The base list reports a count, so it must not build the whole file payload.

    ``_list_file_info`` stats every managed file a second time (the listing itself
    already checked each one) and materializes a dict per file. Both scale with the
    corpus on every request, and ``/bases`` uses none of it beyond the count;
    ``/bases/{base_id}/files`` still serves the full listing.
    """
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    for index in range(3):
        (docs_path / f"doc{index}.md").write_text("body", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    async def _unexpected_list_file_info(*_args: object, **_kwargs: object) -> object:
        msg = "the base list must not build the full file listing"
        raise AssertionError(msg)

    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    monkeypatch.setattr(knowledge_api, "_list_file_info", _unexpected_list_file_info)
    response = TestClient(main.app, base_url="http://localhost").get("/api/knowledge/bases")

    assert response.status_code == 200
    entry = next(base for base in response.json()["bases"] if base["name"] == "docs")
    assert entry["file_count"] == 3
    assert entry["file_listing_degraded"] is False


def test_base_files_endpoint_still_returns_sizes_and_timestamps(tmp_path: Path) -> None:
    """The dedicated listing endpoint keeps the per-file detail the base list dropped."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("body", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    response = TestClient(main.app, base_url="http://localhost").get("/api/knowledge/bases/docs/files")

    assert response.status_code == 200
    payload = response.json()
    assert payload["file_count"] == 1
    assert payload["total_size"] == len(b"body")
    assert payload["files"][0]["path"] == "doc.md"
    assert payload["files"][0]["size"] == len(b"body")
    assert payload["files"][0]["modified"]


def _committed_git_checkout(path: Path, git_dir: Path) -> None:
    """Commit ``doc.md`` in a checkout whose Git directory lives apart, as MindRoom keeps one."""
    path.mkdir()
    git_dir.parent.mkdir(parents=True, exist_ok=True)
    (path / "doc.md").write_text("body", encoding="utf-8")
    env = {**os.environ, "GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(path)}
    for args in (
        ("init", "-b", "main"),
        ("add", "doc.md"),
        ("-c", "user.email=tests@example.com", "-c", "user.name=MindRoom Tests", "commit", "-m", "doc"),
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, env=env)


def test_dashboard_git_listing_never_runs_checkout_fsmonitor(tmp_path: Path) -> None:
    """Dashboard reads must not run a program a .git inside the checkout names.

    A shared checkout may live in an agent workspace, where agent tools can write
    a ``.git`` beside the knowledge files, while the dashboard lists it from the
    primary process.
    """
    docs_path = tmp_path / "docs"
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], git_configs={"docs": git_config})
    runtime_paths = runtime_paths_for(config)
    _committed_git_checkout(docs_path, knowledge_git_dir(runtime_paths.storage_root, docs_path))
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "init", "--quiet"], cwd=docs_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=docs_path, check=True, capture_output=True)

    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    client = TestClient(main.app, base_url="http://localhost")
    bases = client.get("/api/knowledge/bases")
    files = client.get("/api/knowledge/bases/docs/files")
    status = client.get("/api/knowledge/bases/docs/status")

    assert bases.status_code == files.status_code == status.status_code == 200
    assert [entry["path"] for entry in files.json()["files"]] == ["doc.md"]
    assert status.json()["git"]["repo_present"] is True
    assert not marker.exists()


def test_git_listing_runs_git_without_caller_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listing must not hand the primary process' secrets or relative PATH entries to Git."""
    docs_path = tmp_path / "docs"
    git_dir = tmp_path / "docs.git"
    _committed_git_checkout(docs_path, git_dir)
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], git_configs={"docs": git_config})
    monkeypatch.setenv("MINDROOM_API_KEY", "dashboard-secret")
    monkeypatch.setenv("PATH", os.pathsep.join(["relative-bin", os.environ["PATH"]]))
    original_run = subprocess.run
    envs: list[dict[str, str]] = []

    def _recording_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        envs.append(cast("dict[str, str]", kwargs["env"]))
        return original_run(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(knowledge_file_listing_module.subprocess, "run", _recording_run)

    assert list_git_tracked_knowledge_files(config, "docs", docs_path, git_dir) == [docs_path.resolve() / "doc.md"]
    assert len(envs) == 1
    env = envs[0]
    assert "dashboard-secret" not in str(env)
    assert env["GIT_DIR"] == str(git_dir)
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    assert all(Path(entry).is_absolute() for entry in env["PATH"].split(os.pathsep))


def test_read_only_git_refuses_transports_that_repository_config_allows(tmp_path: Path) -> None:
    """Repository config re-allowing a protocol must not open a transport for a local command."""
    remote = tmp_path / "remote"
    _committed_git_checkout(remote, remote / ".git")
    docs_path = tmp_path / "docs"
    git_dir = tmp_path / "docs.git"
    _committed_git_checkout(docs_path, git_dir)
    subprocess.run(["git", f"--git-dir={git_dir}", "config", "protocol.file.allow", "always"], check=True)

    result = subprocess.run(
        hardened_git_command(["ls-remote", remote.resolve().as_uri()]),
        cwd=docs_path,
        env=hardened_git_env(git_dir=git_dir, work_tree=docs_path),
        check=False,
        capture_output=True,
        text=True,
        timeout=SETTLE_TIMEOUT_SECONDS,
    )

    assert result.returncode != 0
    assert "not allowed" in result.stderr


def test_listing_targets_never_walk_out_through_parent_traversal(tmp_path: Path) -> None:
    """A ".." listing target, which pathlib's lexical ``relative_to`` lets through, yields nothing.

    Without this a "../*.md" include pattern that bypassed config validation yields a
    listing target at the parent directory.
    """
    root = tmp_path / "docs"
    root.mkdir()
    (tmp_path / "secret.md").write_text("secret outside root", encoding="utf-8")
    target = knowledge_file_listing_module._ListingTarget(root / "..", "dir")

    with knowledge_file_listing_module._pinned_directory(root) as root_fd:
        assert list(knowledge_file_listing_module._iter_target_files(root_fd, target, root)) == []


def test_tracked_path_listing_rejects_parent_traversal_escape(tmp_path: Path) -> None:
    """A tracked relative path that walks out of the knowledge root must stay excluded."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (tmp_path / "secret.md").write_text("secret outside root", encoding="utf-8")
    (docs_path / "kept.md").write_text("kept", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])

    files = knowledge_files_from_relative_paths(config, "docs", docs_path, ["kept.md", "../secret.md"])

    assert [path.name for path in files] == ["kept.md"]


def test_knowledge_base_config_rejects_parent_traversal_patterns() -> None:
    """Config validation is the first containment layer, so the guard is never reached this way."""
    with pytest.raises(ValidationError):
        KnowledgeBaseConfig(path="./docs", include_patterns=["../*.md"])


def test_local_poll_interval_is_rejected_on_git_bases() -> None:
    """Git bases keep their own interval, so the local field must not be set beside git."""
    with pytest.raises(ValidationError, match=r"use git\.poll_interval_seconds"):
        KnowledgeBaseConfig(
            poll_interval_seconds=60,
            git=KnowledgeGitConfig(repo_url="https://example.com/x.git"),
        )


def test_local_poll_interval_is_rejected_on_files_mode_bases() -> None:
    """Files-mode bases have no index to refresh, so a poller would only hash every file for nothing."""
    with pytest.raises(ValidationError, match="poll_interval_seconds applies to semantic local knowledge bases"):
        KnowledgeBaseConfig(mode="files", poll_interval_seconds=60)


def test_tracked_path_listing_rejects_directories_and_missing_paths(tmp_path: Path) -> None:
    """Only regular files survive the tracked-path safety checks."""
    docs_path = tmp_path / "docs"
    (docs_path / "directory.md").mkdir(parents=True)
    (docs_path / "kept.md").write_text("kept", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])

    files = knowledge_files_from_relative_paths(config, "docs", docs_path, ["kept.md", "directory.md", "gone.md"])

    assert [path.name for path in files] == ["kept.md"]


def test_knowledge_file_listing_skips_hidden_files_for_directory_bases(tmp_path: Path) -> None:
    """Dot-prefixed entries (e.g. in-place writers' atomic-write temp files) stay out of directory bases."""
    docs_path = tmp_path / "docs"
    (docs_path / ".staging").mkdir(parents=True)
    (docs_path / "kept.md").write_text("kept", encoding="utf-8")
    (docs_path / ".hidden.md").write_text("hidden", encoding="utf-8")
    (docs_path / ".staging" / "nested.md").write_text("nested", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])

    assert list_knowledge_files(config, "docs", docs_path) == [(docs_path / "kept.md").resolve()]

    config.knowledge_bases["docs"].skip_hidden = False
    assert list_knowledge_files(config, "docs", docs_path) == sorted(
        [
            (docs_path / "kept.md").resolve(),
            (docs_path / ".hidden.md").resolve(),
            (docs_path / ".staging" / "nested.md").resolve(),
        ],
    )


@pytest.mark.asyncio
async def test_file_indexer_notes_files_vanishing_during_refresh(tmp_path: Path) -> None:
    """A file deleted between listing and indexing is skipped instead of failing the refresh.

    Live source folders such as thread exports delete files while a refresh
    runs (stale-thread cleanup); the per-file stat used to raise
    FileNotFoundError and abort the whole reindex subprocess.
    """
    docs_path = (tmp_path / "docs").resolve()
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    collection = KnowledgeCollection(path=tmp_path / "store", name="candidate")
    collection.create(collection_meta())
    indexer = FileIndexer(
        base_id="docs",
        config=config,
        runtime_paths=runtime_paths_for(config),
        knowledge_root=docs_path,
        collection=collection,
        embedder=RefreshEmbedder(_FakeEmbedder()),
        max_concurrent_file_indexes=max_concurrent_file_indexes(runtime_paths_for(config)),
    )
    completed: dict[str, FileSignature] = {}
    failed: set[str] = set()
    vanished: set[str] = set()
    settled: list[None] = []

    indexed = await indexer.index_files(
        [docs_path / "gone.md"],
        completed=completed,
        failed=failed,
        vanished=vanished,
        on_file_done=lambda: settled.append(None),
    )

    assert indexed == 0
    assert (completed, failed, vanished) == ({}, set(), {"gone.md"})
    assert settled == []
    assert collection.claim_counts() == (0, 0)


@pytest.mark.asyncio
async def test_reindex_publishes_surviving_files_when_one_vanishes_mid_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file deleted between listing and indexing must not mark the refresh incomplete.

    The surviving corpus matches the live folder, so the refresh publishes it;
    only genuine indexing failures may abort the pass.
    """
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "kept.md").write_text("survives the refresh", encoding="utf-8")
    doomed = (docs_path / "doomed.md").resolve()
    doomed.write_text("deleted mid-refresh", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))

    original_signature = knowledge_file_indexer.file_signature

    def vanishing_signature(file_path: Path, snapshot: Path | None = None) -> FileSignature:
        if file_path == doomed:
            doomed.unlink(missing_ok=True)
        return original_signature(file_path, snapshot)

    monkeypatch.setattr(knowledge_file_indexer, "file_signature", vanishing_signature)

    assert await manager.reindex_all() == RefreshOutcome(indexed_count=1, published=True, error=None)
    assert _source_has_vectors(manager, "kept.md")
    assert not _source_has_vectors(manager, "doomed.md")


def test_knowledge_file_listing_filters_unsupported_extensions_before_filesystem_safety_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported files should not pay the per-file filesystem safety check."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    ignored_path = docs_path / "ignored.bin"
    ignored_path.write_bytes(b"not semantic")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])

    original_lstat = Path.lstat

    def _lstat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        if self.name == ignored_path.name:
            msg = "unsupported files should be filtered before the safety check"
            raise AssertionError(msg)
        return original_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", _lstat)

    assert list_knowledge_files(config, "docs", docs_path) == []


def test_local_knowledge_file_listing_prunes_literal_include_prefixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local include globs should avoid walking unrelated source trees."""
    docs_path = tmp_path / "workspace"
    memory_dir = docs_path / "memory"
    unrelated_dir = docs_path / "docs" / "deep"
    memory_dir.mkdir(parents=True)
    unrelated_dir.mkdir(parents=True)
    memory_file = memory_dir / "2026-06-02.md"
    unrelated_file = unrelated_dir / "runbook.md"
    memory_file.write_text("Indexed memory.\n", encoding="utf-8")
    unrelated_file.write_text("Unrelated markdown.\n", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    config.knowledge_bases["docs"] = KnowledgeBaseConfig(
        path=str(docs_path),
        include_extensions=[".md"],
        include_patterns=["memory/**/*.md"],
    )

    walked_roots: list[Path] = []
    original_walk = knowledge_file_listing_module._walk_relative_files

    def recording_walk(root_fd: int, base: Path) -> list[Path]:
        walked_roots.append(base)
        return original_walk(root_fd, base)

    monkeypatch.setattr(knowledge_file_listing_module, "_walk_relative_files", recording_walk)

    files = list_knowledge_files(config, "docs", docs_path)

    assert files == [memory_file.resolve()]
    assert walked_roots == [Path("memory")]


def test_extra_extensions_extend_default_semantic_set(tmp_path: Path) -> None:
    """extra_extensions add to the default text-like set without replacing it."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "notes.md").write_text("hello", encoding="utf-8")
    (docs_path / "slides.pptx").write_bytes(b"fake deck")
    (docs_path / "blob.bin").write_bytes(b"binary")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    config.knowledge_bases["docs"] = KnowledgeBaseConfig(
        path=str(docs_path),
        extra_extensions=[".pptx"],
    )

    files = list_knowledge_files(config, "docs", docs_path)

    assert files == [(docs_path / "notes.md").resolve(), (docs_path / "slides.pptx").resolve()]


def test_extra_extensions_compose_with_include_and_exclude_extensions(tmp_path: Path) -> None:
    """include_extensions stay an exact set, extras add on top, excludes win last."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "notes.md").write_text("hello", encoding="utf-8")
    (docs_path / "readme.txt").write_text("hello", encoding="utf-8")
    (docs_path / "slides.pptx").write_bytes(b"fake deck")
    (docs_path / "report.pdf").write_bytes(b"fake report")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    config.knowledge_bases["docs"] = KnowledgeBaseConfig(
        path=str(docs_path),
        include_extensions=[".md"],
        extra_extensions=[".pptx", ".pdf"],
        exclude_extensions=[".pdf"],
    )

    files = list_knowledge_files(config, "docs", docs_path)

    assert files == [(docs_path / "notes.md").resolve(), (docs_path / "slides.pptx").resolve()]


@pytest.mark.asyncio
async def test_reindex_skips_files_whose_reader_dependency_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing reader package skips the file loudly instead of failing the refresh."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "notes.md").write_text("indexed text", encoding="utf-8")
    (docs_path / "slides.pptx").write_text("fake deck", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    config.knowledge_bases["docs"] = KnowledgeBaseConfig(
        path=str(docs_path),
        extra_extensions=[".pptx"],
    )
    original_get_reader = knowledge_file_indexer.ReaderFactory.get_reader_for_extension

    def failing_get_reader(extension: str) -> object:
        if extension == ".pptx":
            msg = "The `python-pptx` package is not installed."
            raise ImportError(msg)
        return original_get_reader(extension)

    monkeypatch.setattr(knowledge_file_indexer.ReaderFactory, "get_reader_for_extension", failing_get_reader)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))

    assert await manager.reindex_all() == RefreshOutcome(
        indexed_count=1,
        published=False,
        error="Indexed 1 of 2 managed knowledge files",
    )


@pytest.mark.asyncio
async def test_index_metadata_without_source_signature_is_unavailable_and_schedules_refresh(
    tmp_path: Path,
) -> None:
    """Stale-format published metadata is treated as corrupt instead of interpreted."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("stale-format index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    publication_file = publication_path(_storage(config, runtime_paths))
    payload = json.loads(publication_file.read_text(encoding="utf-8"))
    payload.pop("source_signature", None)
    publication_file.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    knowledge_registry._published_indexes.clear()
    scheduler = _RecordingScheduler(refreshing=False)
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is None
    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_successful_publish_clears_stale_refresh_state(tmp_path: Path) -> None:
    """A successful publish clears stale refresh state."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge_registry.mark_published_index_stale(key, reason="test_stale")

    stale_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert stale_lookup.reader is not None
    assert stale_lookup.availability is KnowledgeAvailability.STALE

    (docs_path / "doc.md").write_text("index updated", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is not None
    assert _refresh_state_for_key(key) == "none"
    assert unavailable == {}


def _record_source_lock_entries(monkeypatch: pytest.MonkeyPatch) -> list[knowledge_registry.KnowledgeSourceRoot]:
    """Record each source-root lock the refresh runner asks for, just before it waits for it."""
    entries: list[knowledge_registry.KnowledgeSourceRoot] = []
    original = knowledge_refresh_runner.refresh_source_root_lock

    @asynccontextmanager
    async def _recording(key: knowledge_registry.KnowledgeSourceRoot) -> AsyncIterator[None]:
        entries.append(key)
        async with original(key):
            yield

    monkeypatch.setattr(knowledge_refresh_runner, "refresh_source_root_lock", _recording)
    return entries


def _block_reindex(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold every refresh child inside its reindex, under the source-root lock, until the returned release is set."""
    entered = asyncio.Event()
    release = asyncio.Event()
    original_reindex = KnowledgeManager.reindex_all

    async def _blocked_reindex(self: KnowledgeManager, *, force_reindex: bool = False) -> RefreshOutcome:
        entered.set()
        await release.wait()
        return await original_reindex(self, force_reindex=force_reindex)

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _blocked_reindex)
    return entered, release


@pytest.mark.asyncio
async def test_a_cancelled_scheduled_refresh_leaves_its_base_not_refreshing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a scheduled refresh mid-child must not leave the base reported as refreshing."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("guide", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    entered, _release = _block_reindex(monkeypatch)
    scheduler = KnowledgeRefreshScheduler()
    target = knowledge_registry.resolve_refresh_target("docs", config=config, runtime_paths=runtime_paths)

    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await asyncio.wait_for(entered.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    assert scheduler.is_refreshing(target)
    task = scheduler._tasks[target]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert scheduler.is_refreshing(target) is False
    assert _refresh_status(config, runtime_paths) == RefreshStatus()
    assert _publication(config, runtime_paths) is None


@pytest.mark.asyncio
async def test_a_refresh_cancelled_before_it_publishes_leaves_the_refresh_status_as_it_was(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled refresh records nothing: the source change it was publishing stays marked, with no failure."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "guide.md"
    doc.write_text("stable index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("changed index", encoding="utf-8")
    mark_published_index_stale(key, reason="source_changed")
    status_before = _refresh_status(config, runtime_paths)
    publication_before = _publication(config, runtime_paths)
    entered, _release = _block_reindex(monkeypatch)

    refresh_task = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await asyncio.wait_for(entered.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    refresh_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task

    assert _refresh_status(config, runtime_paths) == status_before
    assert status_before.reason == "source_changed"
    assert _refresh_state_for_key(key) == "stale"
    assert _publication(config, runtime_paths) == publication_before


@pytest.mark.asyncio
async def test_cancelled_refresh_waiting_for_source_lock_does_not_touch_refresh_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation while queued behind another refresh of the same source must not change any state file."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("locked refresh", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    lock_entries = _record_source_lock_entries(monkeypatch)
    entered, release = _block_reindex(monkeypatch)

    first_task = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await asyncio.wait_for(entered.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    second_task = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await _wait_until(lambda: len(lock_entries) == 2, "the second refresh did not queue on the source lock")

    second_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second_task

    assert _refresh_status(config, runtime_paths) == RefreshStatus()
    assert _publication(config, runtime_paths) is None

    release.set()
    result = await first_task
    assert result.index_published is True
    assert _refresh_state_for_key(resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)) == (
        "none"
    )


@pytest.mark.asyncio
async def test_cancelled_source_lock_waiter_does_not_wedge_later_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled queued waiter must not acquire and leak the source lock."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    lock_entries = _record_source_lock_entries(monkeypatch)
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()
    waiter_entered = asyncio.Event()

    async def _hold_lock() -> None:
        async with knowledge_binding_mutation_lock("docs", config=config, runtime_paths=runtime_paths):
            holder_entered.set()
            await release_holder.wait()

    async def _queued_waiter() -> None:
        async with knowledge_binding_mutation_lock("docs", config=config, runtime_paths=runtime_paths):
            waiter_entered.set()

    holder_task = asyncio.create_task(_hold_lock())
    await holder_entered.wait()
    waiter_task = asyncio.create_task(_queued_waiter())
    await _wait_until(lambda: len(lock_entries) == 2, "the waiter did not queue on the source lock")

    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task

    release_holder.set()
    await holder_task

    async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
        async with knowledge_binding_mutation_lock("docs", config=config, runtime_paths=runtime_paths):
            pass
    assert not waiter_entered.is_set()


@pytest.mark.asyncio
async def test_an_idle_source_lock_is_dropped_but_a_queued_waiter_keeps_its_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source's in-loop lock lives while anything holds or waits for it, and is dropped with its last user."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    refresh_target = knowledge_registry.resolve_refresh_target("docs", config=config, runtime_paths=runtime_paths)
    source_root = knowledge_registry.source_root_for_refresh_target(refresh_target)
    lock_entries = _record_source_lock_entries(monkeypatch)
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()
    waiter_entered = asyncio.Event()

    async def _hold_lock() -> None:
        async with knowledge_binding_mutation_lock("docs", config=config, runtime_paths=runtime_paths):
            holder_entered.set()
            await release_holder.wait()

    async def _queued_waiter() -> None:
        async with knowledge_binding_mutation_lock("docs", config=config, runtime_paths=runtime_paths):
            waiter_entered.set()

    holder_task = asyncio.create_task(_hold_lock())
    await holder_entered.wait()
    waiter_task = asyncio.create_task(_queued_waiter())
    await _wait_until(lambda: len(lock_entries) == 2, "the waiter did not queue on the source lock")
    original_lock = knowledge_refresh_locks._refresh_locks[source_root]
    others = [
        knowledge_registry.KnowledgeSourceRoot(
            storage_root=str(tmp_path / f"other-{index}"),
            knowledge_path=str(tmp_path / f"other-{index}" / "docs"),
        )
        for index in range(5)
    ]
    for other in others:
        async with knowledge_refresh_locks.refresh_source_root_lock(other):
            pass
    gc.collect()

    assert knowledge_refresh_locks._refresh_locks.get(source_root) is original_lock
    assert [other for other in others if other in knowledge_refresh_locks._refresh_locks] == []

    release_holder.set()
    async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
        await asyncio.gather(holder_task, waiter_task)
    assert waiter_entered.is_set()


@pytest.mark.asyncio
async def test_source_root_lock_takes_the_in_loop_half_before_the_cross_process_half(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-loop half must nest outside the file lock, so the two unwind in reverse."""
    source_root = knowledge_registry.KnowledgeSourceRoot(
        storage_root=str(tmp_path),
        knowledge_path=str(tmp_path / "docs"),
    )
    events: list[tuple[str, bool]] = []
    original_file_lock = knowledge_refresh_locks.async_exclusive_file_lock

    @asynccontextmanager
    async def _recording_file_lock(lock_path: Path, **kwargs: bool | float) -> AsyncIterator[object]:
        in_loop = knowledge_refresh_locks._refresh_locks[source_root]
        events.append(("acquire file", in_loop.locked()))
        async with original_file_lock(lock_path, **kwargs) as lock_file:
            yield lock_file
        events.append(("release file", in_loop.locked()))

    monkeypatch.setattr(knowledge_refresh_locks, "async_exclusive_file_lock", _recording_file_lock)

    async with knowledge_refresh_locks.refresh_source_root_lock(source_root):
        in_loop = knowledge_refresh_locks._refresh_locks[source_root]
        events.append(("body", in_loop.locked()))

    assert events == [("acquire file", True), ("body", True), ("release file", True)]
    assert not in_loop.locked()


@pytest.mark.asyncio
async def test_source_root_lock_capability_expires_in_inherited_task_context(tmp_path: Path) -> None:
    """A task created under the lock cannot retain cleanup authority after release."""
    source_path = (tmp_path / "docs").resolve()
    source_root = knowledge_registry.KnowledgeSourceRoot(
        storage_root=str(tmp_path.resolve()),
        knowledge_path=str(source_path),
    )
    inspect_after_release = asyncio.Event()
    observed_fds: list[int | None] = []

    async def _inspect_inherited_context() -> None:
        capability = file_locks.current_inherited_file_lock()
        assert capability is not None
        await inspect_after_release.wait()
        observed_fds.append(capability.fileno_for(source_path))

    async with knowledge_refresh_locks.refresh_source_root_lock(source_root):
        inherited_task = asyncio.create_task(_inspect_inherited_context())

    inspect_after_release.set()
    await inherited_task

    assert observed_fds == [None]


@pytest.mark.asyncio
async def test_cancelling_while_the_cross_process_half_is_pending_frees_the_in_loop_half(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Taking the pair in two stages must not strand the first half when the second is cancelled."""
    source_root = knowledge_registry.KnowledgeSourceRoot(
        storage_root=str(tmp_path),
        knowledge_path=str(tmp_path / "docs"),
    )
    file_lock_reached = asyncio.Event()
    release_file_lock = asyncio.Event()

    @asynccontextmanager
    async def _blocked_file_lock(_lock_path: Path, **_kwargs: bool | float) -> AsyncIterator[None]:
        file_lock_reached.set()
        await release_file_lock.wait()
        yield

    monkeypatch.setattr(knowledge_refresh_locks, "async_exclusive_file_lock", _blocked_file_lock)

    async def _take_the_pair() -> None:
        async with knowledge_refresh_locks.refresh_source_root_lock(source_root):
            pass

    blocked_task = asyncio.create_task(_take_the_pair())
    await file_lock_reached.wait()
    in_loop = knowledge_refresh_locks._refresh_locks[source_root]
    assert in_loop.locked()

    blocked_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked_task

    assert not in_loop.locked()


def test_source_changed_updates_refresh_state_without_changing_index(tmp_path: Path) -> None:
    """Source mutation records source changes without mutating published index data."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("published old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
    default_collection = manager._collections._default_collection
    published = open_collection(manager._collections, default_collection)
    seed_collection(published, [{"content": "published old", "metadata": {"source_path": "guide.md"}}])
    publication = _publish(key, collection=default_collection, source_signature="test-source-signature")

    marked_base_ids = knowledge_registry._mark_knowledge_source_changed(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
    )

    assert marked_base_ids == ("docs",)
    assert stored_rows(published) == [
        {"id": "seeded-0", "content": "published old", "metadata": {"source_path": "guide.md"}},
    ]
    assert load_publication(published_index_storage_path(key)) == publication
    assert _refresh_state_for_key(key) == "stale"
    assert load_refresh_status(published_index_storage_path(key)).reason == "source_mutated"


@pytest.mark.asyncio
async def test_mark_stale_fans_out_to_duplicate_physical_sources(tmp_path: Path) -> None:
    """Mutating one base should stale every published index that reads the same source folder."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "guide.md"
    doc.write_text("shared source old", encoding="utf-8")
    config = _config(tmp_path, bases={"alpha": docs_path, "beta": docs_path}, agent_bases=["alpha", "beta"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("beta", config=config, runtime_paths=runtime_paths)
    beta_lookup = get_published_index("beta", config=config, runtime_paths=runtime_paths)
    assert beta_lookup.reader is not None
    assert beta_lookup.availability is KnowledgeAvailability.READY
    doc.write_text("shared source new", encoding="utf-8")

    marked_base_ids = knowledge_registry._mark_knowledge_source_changed(
        "alpha",
        config=config,
        runtime_paths=runtime_paths,
    )
    beta_key = resolve_published_index_key("beta", config=config, runtime_paths=runtime_paths)
    refreshed_beta_lookup = get_published_index("beta", config=config, runtime_paths=runtime_paths)

    assert marked_base_ids == ("alpha", "beta")
    assert _refresh_state_for_key(beta_key) == "stale"
    assert refreshed_beta_lookup.availability is KnowledgeAvailability.STALE


@pytest.mark.asyncio
async def test_mark_stale_skips_file_mode_duplicate_physical_sources(tmp_path: Path) -> None:
    """File-mode aliases do not maintain semantic indexes that need stale marking."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "guide.md"
    doc.write_text("shared source old", encoding="utf-8")
    config = _config(
        tmp_path,
        bases={"alpha": docs_path, "beta": docs_path},
        agent_bases=["alpha", "beta"],
        modes={"alpha": "semantic", "beta": "files"},
    )
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("beta", config=config, runtime_paths=runtime_paths)
    beta_key = resolve_published_index_key("beta", config=config, runtime_paths=runtime_paths)
    beta_publication = load_publication(published_index_storage_path(beta_key))
    assert beta_publication is not None

    doc.write_text("shared source new", encoding="utf-8")
    marked_base_ids = knowledge_registry._mark_knowledge_source_changed(
        "alpha",
        config=config,
        runtime_paths=runtime_paths,
    )

    assert marked_base_ids == ("alpha",)
    assert load_publication(published_index_storage_path(beta_key)) == beta_publication
    assert beta_publication.collection is None
    assert _refresh_state_for_key(beta_key) == "none"


@pytest.mark.asyncio
async def test_mark_stale_from_file_mode_alias_marks_semantic_duplicate_sources(tmp_path: Path) -> None:
    """File-mode source mutations should stale semantic aliases that read the same folder."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "guide.md"
    doc.write_text("shared source old", encoding="utf-8")
    config = _config(
        tmp_path,
        bases={"alpha": docs_path, "beta": docs_path},
        agent_bases=["alpha", "beta"],
        modes={"alpha": "semantic", "beta": "files"},
    )
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("beta", config=config, runtime_paths=runtime_paths)
    alpha_lookup = get_published_index("alpha", config=config, runtime_paths=runtime_paths)
    assert alpha_lookup.reader is not None
    assert alpha_lookup.availability is KnowledgeAvailability.READY

    doc.write_text("shared source new", encoding="utf-8")
    marked_base_ids = knowledge_registry._mark_knowledge_source_changed(
        "beta",
        config=config,
        runtime_paths=runtime_paths,
    )
    alpha_key = resolve_published_index_key("alpha", config=config, runtime_paths=runtime_paths)
    beta_key = resolve_published_index_key("beta", config=config, runtime_paths=runtime_paths)
    beta_publication = load_publication(published_index_storage_path(beta_key))
    refreshed_alpha_lookup = get_published_index("alpha", config=config, runtime_paths=runtime_paths)

    assert marked_base_ids == ("alpha",)
    assert _refresh_state_for_key(alpha_key) == "stale"
    assert refreshed_alpha_lookup.availability is KnowledgeAvailability.STALE
    assert beta_publication is not None
    assert beta_publication.collection is None
    assert _refresh_state_for_key(beta_key) == "none"


@pytest.mark.asyncio
async def test_async_source_changed_cancellation_waits_for_state_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during stale writes must wait for the metadata commit before propagating."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("cached old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    ready_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert ready_lookup.reader is not None
    assert ready_lookup.availability is KnowledgeAvailability.READY

    loop = asyncio.get_running_loop()
    stale_written = asyncio.Event()
    release_stale_write = Event()
    original_mark = knowledge_registry.mark_published_index_stale

    def _block_after_stale_write(matching_key: knowledge_registry.PublishedIndexKey, *, reason: str) -> None:
        original_mark(matching_key, reason=reason)
        loop.call_soon_threadsafe(stale_written.set)
        assert release_stale_write.wait(timeout=SETTLE_TIMEOUT_SECONDS)

    monkeypatch.setattr(knowledge_registry, "mark_published_index_stale", _block_after_stale_write)

    mark_task = asyncio.create_task(
        knowledge_registry.mark_knowledge_source_changed_async(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
        ),
    )
    await stale_written.wait()
    mark_task.cancel()
    release_stale_write.set()
    with pytest.raises(asyncio.CancelledError):
        await mark_task

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    refreshed_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert _refresh_state_for_key(key) == "stale"
    assert refreshed_lookup.availability is KnowledgeAvailability.STALE


@pytest.mark.asyncio
async def test_async_source_changed_cancellation_finishes_same_source_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after one alias write must still mark every same-source alias stale."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "guide.md"
    doc.write_text("shared cached old", encoding="utf-8")
    config = _config(tmp_path, bases={"alpha": docs_path, "beta": docs_path}, agent_bases=["alpha", "beta"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("beta", config=config, runtime_paths=runtime_paths)
    doc.write_text("shared cached new", encoding="utf-8")

    loop = asyncio.get_running_loop()
    first_alias_written = asyncio.Event()
    release_remaining_writes = Event()
    original_mark = knowledge_registry.mark_published_index_stale
    written_base_ids: list[str] = []

    def _block_after_first_alias(matching_key: knowledge_registry.PublishedIndexKey, *, reason: str) -> None:
        original_mark(matching_key, reason=reason)
        written_base_ids.append(matching_key.base_id)
        if len(written_base_ids) == 1:
            loop.call_soon_threadsafe(first_alias_written.set)
            assert release_remaining_writes.wait(timeout=SETTLE_TIMEOUT_SECONDS)

    monkeypatch.setattr(knowledge_registry, "mark_published_index_stale", _block_after_first_alias)

    mark_task = asyncio.create_task(
        knowledge_registry.mark_knowledge_source_changed_async(
            "alpha",
            config=config,
            runtime_paths=runtime_paths,
        ),
    )
    await first_alias_written.wait()
    mark_task.cancel()
    release_remaining_writes.set()
    with pytest.raises(asyncio.CancelledError):
        await mark_task

    alpha_key = resolve_published_index_key("alpha", config=config, runtime_paths=runtime_paths)
    beta_key = resolve_published_index_key("beta", config=config, runtime_paths=runtime_paths)

    assert tuple(written_base_ids) == ("alpha", "beta")
    assert _refresh_state_for_key(alpha_key) == "stale"
    assert _refresh_state_for_key(beta_key) == "stale"


@pytest.mark.asyncio
async def test_async_source_changed_recached_index_reports_refresh_state_after_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readers may keep last-good handles while refresh state changes to stale after commit."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("recache old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    ready_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert ready_lookup.reader is not None
    assert ready_lookup.availability is KnowledgeAvailability.READY

    loop = asyncio.get_running_loop()
    stale_write_started = asyncio.Event()
    release_stale_write = Event()
    original_mark = knowledge_registry.mark_published_index_stale

    def _block_before_stale_write(matching_key: knowledge_registry.PublishedIndexKey, *, reason: str) -> None:
        loop.call_soon_threadsafe(stale_write_started.set)
        assert release_stale_write.wait(timeout=SETTLE_TIMEOUT_SECONDS)
        original_mark(matching_key, reason=reason)

    monkeypatch.setattr(knowledge_registry, "mark_published_index_stale", _block_before_stale_write)

    mark_task = asyncio.create_task(
        knowledge_registry.mark_knowledge_source_changed_async(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
        ),
    )
    await stale_write_started.wait()
    recached_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert recached_lookup.reader is not None
    assert recached_lookup.availability is KnowledgeAvailability.READY

    release_stale_write.set()
    assert await mark_task == ("docs",)
    final_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert final_lookup.availability is KnowledgeAvailability.STALE


@pytest.mark.asyncio
async def test_local_refresh_marks_duplicate_source_sibling_stale_after_source_change(tmp_path: Path) -> None:
    """Refreshing one local alias after an external source edit should stale sibling aliases."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "guide.md"
    doc.write_text("shared local old", encoding="utf-8")
    config = _config(tmp_path, bases={"alpha": docs_path, "beta": docs_path}, agent_bases=["alpha", "beta"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("beta", config=config, runtime_paths=runtime_paths)
    beta_lookup = get_published_index("beta", config=config, runtime_paths=runtime_paths)
    assert beta_lookup.reader is not None
    assert beta_lookup.availability is KnowledgeAvailability.READY

    doc.write_text("shared local new", encoding="utf-8")
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    alpha_lookup = get_published_index("alpha", config=config, runtime_paths=runtime_paths)
    beta_key = resolve_published_index_key("beta", config=config, runtime_paths=runtime_paths)
    refreshed_beta_lookup = get_published_index("beta", config=config, runtime_paths=runtime_paths)

    assert alpha_lookup.reader is not None
    assert [document.content for document in await alpha_lookup.reader.asearch("local", limit=5)] == [
        "shared local new",
    ]
    assert _refresh_state_for_key(beta_key) == "stale"
    assert refreshed_beta_lookup.reader is not None
    assert refreshed_beta_lookup.availability is KnowledgeAvailability.STALE
    assert [document.content for document in await refreshed_beta_lookup.reader.asearch("local", limit=5)] == [
        "shared local old",
    ]


@pytest.mark.asyncio
async def test_a_forced_reindex_of_a_local_base_does_not_mark_its_aliases_stale(tmp_path: Path) -> None:
    """Rebuilding one alias's vectors changes no source, so the aliases over the same folder stay ready."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("shared local", encoding="utf-8")
    config = _config(tmp_path, bases={"alpha": docs_path, "beta": docs_path}, agent_bases=["alpha", "beta"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("beta", config=config, runtime_paths=runtime_paths)

    result = await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths, force_reindex=True)

    assert result.index_published is True
    assert _refresh_state_for_key(resolve_published_index_key("beta", config=config, runtime_paths=runtime_paths)) == (
        "none"
    )
    assert get_published_index("beta", config=config, runtime_paths=runtime_paths).availability is (
        KnowledgeAvailability.READY
    )


def test_config_knowledge_overlap_checks_scale_with_root_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrelated roots must not cause quadratic path ancestry checks during validation."""
    bases = {f"kb-{index}": tmp_path / f"kb-{index}" for index in range(55)}
    original = Path.is_relative_to
    comparisons = 0

    def count_comparisons(self: Path, other: Path) -> bool:
        nonlocal comparisons
        comparisons += 1
        return original(self, other)

    monkeypatch.setattr(Path, "is_relative_to", count_comparisons)
    config = _config(tmp_path, bases=bases, agent_bases=["kb-0"])

    assert len(config.knowledge_bases) == len(bases)
    assert comparisons <= 2 * len(bases)


@pytest.mark.parametrize(
    "root_names",
    [["parent", "child"], ["child", "parent"], ["child", "sibling", "parent"], ["alias", "child", "parent"]],
)
def test_config_rejects_parent_child_knowledge_roots(tmp_path: Path, root_names: list[str]) -> None:
    """Configured local knowledge roots may be exact aliases, but not overlapping subtrees."""
    parent = tmp_path / "docs"
    child = parent / "nested"
    roots = {"parent": parent, "child": child, "sibling": tmp_path / "docs-archive", "alias": parent}

    with pytest.raises(ValueError, match="knowledge_bases paths must not overlap"):
        _config(
            tmp_path,
            bases={name: roots[name] for name in root_names},
            agent_bases=["parent"],
        )


def test_config_rejects_knowledge_overlap_through_symlink(tmp_path: Path) -> None:
    """Compare resolved roots so differently named symlink paths cannot conceal overlap."""
    parent = tmp_path / "docs"
    parent.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(parent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="knowledge_bases paths must not overlap"):
        _config(tmp_path, bases={"child": link / "nested", "parent": parent}, agent_bases=["parent"])


def test_config_rejects_exact_duplicate_roots_with_mixed_git_ownership(tmp_path: Path) -> None:
    """Exact duplicate knowledge roots must agree on local vs Git source ownership."""
    docs = tmp_path / "docs"

    with pytest.raises(ValueError, match="exact duplicate aliases must use compatible source configuration"):
        _config(
            tmp_path,
            bases={"local": docs, "git": docs},
            agent_bases=["local"],
            git_configs={"git": KnowledgeGitConfig(repo_url="https://example.com/org/repo.git")},
        )


def test_config_rejects_exact_duplicate_git_roots_with_different_source_semantics(tmp_path: Path) -> None:
    """Exact duplicate Git roots must not share one checkout across incompatible source config."""
    docs = tmp_path / "docs"

    with pytest.raises(ValueError, match="exact duplicate aliases must use compatible source configuration"):
        _config(
            tmp_path,
            bases={"main": docs, "release": docs},
            agent_bases=["main"],
            git_configs={
                "main": KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main"),
                "release": KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="release"),
            },
        )


def test_config_rejects_exact_duplicate_git_roots_with_different_passwordless_ssh_usernames(
    tmp_path: Path,
) -> None:
    """Passwordless SSH usernames are part of duplicate-root Git source identity."""
    docs = tmp_path / "docs"

    with pytest.raises(ValueError, match="exact duplicate aliases must use compatible source configuration"):
        _config(
            tmp_path,
            bases={"git_user": docs, "deploy_user": docs},
            agent_bases=["git_user"],
            git_configs={
                "git_user": KnowledgeGitConfig(repo_url="ssh://git@example.com/org/repo.git"),
                "deploy_user": KnowledgeGitConfig(repo_url="ssh://deploy@example.com/org/repo.git"),
            },
        )


def test_config_allows_exact_duplicate_roots_with_compatible_source_semantics(tmp_path: Path) -> None:
    """Exact duplicate aliases remain valid when their source ownership semantics match."""
    docs = tmp_path / "docs"
    git_config = KnowledgeGitConfig(
        repo_url="https://token:secret@example.com/org/repo.git?token=query-secret#fragment-secret",
        branch="main",
        include_patterns=["docs/**"],
        exclude_patterns=["docs/private/**"],
    )

    config = _config(
        tmp_path,
        bases={"alpha": docs, "beta": docs},
        agent_bases=["alpha", "beta"],
        git_configs={
            "alpha": git_config,
            "beta": git_config.model_copy(deep=True),
        },
    )

    assert sorted(config.knowledge_bases) == ["alpha", "beta"]


def test_config_allows_exact_duplicate_git_roots_with_different_filters(tmp_path: Path) -> None:
    """One Git checkout may back multiple filtered knowledge views."""
    docs = tmp_path / "docs"

    config = _config(
        tmp_path,
        bases={"docs": docs, "source": docs},
        agent_bases=["docs", "source"],
        git_configs={
            "docs": KnowledgeGitConfig(
                repo_url="https://example.com/org/repo.git",
                branch="main",
                include_patterns=["docs/**"],
            ),
            "source": KnowledgeGitConfig(
                repo_url="https://example.com/org/repo.git",
                branch="main",
                include_patterns=["src/**"],
            ),
        },
    )

    assert sorted(config.knowledge_bases) == ["docs", "source"]


def test_config_allows_exact_duplicate_local_roots_with_different_filters(tmp_path: Path) -> None:
    """One local folder may back multiple filtered knowledge views."""
    docs = tmp_path / "docs"
    runtime_paths = test_runtime_paths(tmp_path)

    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    knowledge_bases=["markdown", "python"],
                ),
            },
            knowledge_bases={
                "markdown": KnowledgeBaseConfig(path=str(docs), include_extensions=[".md"]),
                "python": KnowledgeBaseConfig(path=str(docs), include_extensions=[".py"]),
            },
        ),
        runtime_paths,
    )

    assert sorted(config.knowledge_bases) == ["markdown", "python"]


def test_raw_git_url_index_metadata_is_config_mismatch(tmp_path: Path) -> None:
    """Raw Git URLs in persisted settings are stale-format metadata, not a compatible identity."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("raw git metadata", encoding="utf-8")
    raw_repo_url = "https://token:secret@example.com/org/repo.git"
    git_config = KnowledgeGitConfig(repo_url=raw_repo_url)
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    stale_settings = key.indexing_settings.to_metadata()
    stale_settings["repo_identity"] = raw_repo_url
    collection = "raw_git_metadata_collection"
    seed_collection(
        KnowledgeCollection(path=published_index_storage_path(key), name=collection),
        [{"content": "raw git metadata", "metadata": {"source_path": "doc.md"}}],
    )
    stale = IndexingSettings.from_metadata(stale_settings)
    assert stale is not None
    _publish(key, collection=collection, settings=stale)

    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert lookup.reader is None
    assert lookup.availability is KnowledgeAvailability.CONFIG_MISMATCH


def test_passwordless_ssh_username_change_invalidates_published_index(tmp_path: Path) -> None:
    """Passwordless SSH usernames are part of the persisted index identity."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git user index", encoding="utf-8")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url="ssh://git@example.com/org/repo.git")},
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    collection = "ssh_git_user_collection"
    seed_collection(
        KnowledgeCollection(path=published_index_storage_path(key), name=collection),
        [{"content": "git user index", "metadata": {"source_path": "doc.md"}}],
    )
    _publish(key, collection=collection)
    changed_config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url="ssh://deploy@example.com/org/repo.git")},
    )
    scheduler = _RecordingScheduler(refreshing=False)
    unavailable: dict[str, KnowledgeAvailability] = {}

    lookup = get_published_index("docs", config=changed_config, runtime_paths=runtime_paths)
    _resolution = resolve_agent_knowledge_access(
        "helper",
        changed_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert lookup.reader is None
    assert lookup.availability is KnowledgeAvailability.CONFIG_MISMATCH
    assert knowledge is None
    assert unavailable == {"docs": KnowledgeAvailability.CONFIG_MISMATCH}
    scheduler.schedule_refresh.assert_called_once()


def test_a_publication_alone_serves_its_index(tmp_path: Path) -> None:
    """A publication serves its collection with no refresh status recorded yet."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("metadata index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    collection = "metadata_collection"
    seed_collection(
        KnowledgeCollection(path=published_index_storage_path(key), name=collection),
        [{"content": "metadata index", "metadata": {"source_path": "doc.md"}}],
    )
    _publish(key, collection=collection)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert _refresh_state_for_key(key) == "none"
    assert lookup.reader is not None
    assert lookup.availability is KnowledgeAvailability.READY


def test_indexing_settings_key_uses_named_settings(tmp_path: Path) -> None:
    """Compatibility helpers must use explicit indexing setting names."""
    docs_path = tmp_path / "docs"
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url="https://example.com/org/repo.git")},
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)

    assert key.indexing_settings.base_id == "docs"
    assert key.indexing_settings.chunk_size == "5000"
    assert key.indexing_settings.chunk_overlap == "0"
    assert key.indexing_settings.repo_identity == credential_free_url_identity("https://example.com/org/repo.git")
    assert IndexingSettings.from_metadata(key.indexing_settings.to_metadata()) == key.indexing_settings
    changed_repo_identity = replace(key.indexing_settings, repo_identity="https://example.com/other/repo.git")
    assert not knowledge_indexing_config.published_index_settings_compatible(
        key.indexing_settings,
        changed_repo_identity,
    )


def test_embedder_prefixes_make_indexing_settings_incompatible(tmp_path: Path) -> None:
    """An index embedded with other prefixes is neither queried nor reused."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    plain = indexing_settings_key(config, runtime_paths.storage_root, "docs", docs_path)
    config.memory.embedder.config.document_prefix = "d: "

    prefixed = indexing_settings_key(config, runtime_paths.storage_root, "docs", docs_path)

    assert not knowledge_indexing_config.published_index_settings_compatible(plain, prefixed)


def test_legacy_empty_optional_filter_metadata_remains_compatible() -> None:
    """Legacy empty semantic filter keys normalize once when metadata is parsed."""
    current = _test_indexing_settings()
    legacy_metadata = current.to_metadata()
    legacy_metadata["include_patterns"] = ""
    del legacy_metadata["exclude_patterns"]
    legacy_metadata["extra_extensions"] = ""
    legacy = IndexingSettings.from_metadata(legacy_metadata)

    assert legacy is not None
    assert legacy == current
    assert knowledge_indexing_config.published_index_settings_compatible(legacy, current)
    assert not knowledge_indexing_config.published_index_settings_compatible(
        legacy,
        replace(current, extra_extensions="('.pdf',)"),
    )


def test_knowledge_file_indexing_parallelism_default_is_conservative(tmp_path: Path) -> None:
    """One refresh subprocess should not fan out into dozens of concurrent file embeds by default."""
    assert knowledge_file_indexer.max_concurrent_file_indexes(test_runtime_paths(tmp_path)) == 4


@pytest.mark.parametrize(
    ("process_env", "env_file", "expected"),
    [
        ({}, "MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=16\n", 16),
        ({"MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY": "8"}, "MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=16\n", 8),
        ({}, "", 4),
    ],
    ids=["env_file", "process_env_wins", "live_environ_ignored"],
)
def test_knowledge_file_indexing_parallelism_reads_the_runtime_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_env: dict[str, str],
    env_file: str,
    expected: int,
) -> None:
    """Large corpora can raise file-level indexing concurrency in the runtime env or the `.env` beside `config.yaml`."""
    monkeypatch.setenv("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY", "2")
    (tmp_path / ".env").write_text(env_file, encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env=process_env)

    assert knowledge_file_indexer.max_concurrent_file_indexes(runtime_paths) == expected


@pytest.mark.asyncio
async def test_knowledge_file_indexing_parallelism_is_validated_before_any_file_is_indexed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad override in the `.env` beside `config.yaml` fails the refresh before it reads a file or publishes."""
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=bad\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("guide", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    monkeypatch.setattr(FileIndexer, "_read_file", MagicMock(side_effect=AssertionError("no file may be read")))

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths_for(config))

    assert not result.index_published
    assert "MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY must be an integer" in (result.last_error or "")
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths_for(config))
    assert load_publication(published_index_storage_path(key)) is None


@pytest.mark.parametrize("raw_value", ["bad", "0", "129"])
def test_knowledge_file_indexing_parallelism_rejects_invalid_env(raw_value: str, tmp_path: Path) -> None:
    """Invalid file-level indexing concurrency should fail loudly."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY": raw_value},
    )

    with pytest.raises(ValueError, match="MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY"):
        knowledge_file_indexer.max_concurrent_file_indexes(runtime_paths)


def test_indexing_settings_filter_keys_are_order_insensitive(tmp_path: Path) -> None:
    """Reordered filters should not change indexing compatibility settings."""
    docs_path = tmp_path / "docs"
    git_config = KnowledgeGitConfig(
        repo_url="https://example.com/org/repo.git",
        include_patterns=["z/*.md", "a/*.md"],
        exclude_patterns=["drafts/*", "archive/*"],
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    config.knowledge_bases["docs"].include_extensions = [".py", ".md"]
    config.knowledge_bases["docs"].exclude_extensions = [".png", ".jpg"]
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)

    reordered_config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={
            "docs": KnowledgeGitConfig(
                repo_url="https://example.com/org/repo.git",
                include_patterns=["a/*.md", "z/*.md"],
                exclude_patterns=["archive/*", "drafts/*"],
            ),
        },
    )
    reordered_config.knowledge_bases["docs"].include_extensions = [".md", ".py"]
    reordered_config.knowledge_bases["docs"].exclude_extensions = [".jpg", ".png"]
    reordered_key = resolve_published_index_key("docs", config=reordered_config, runtime_paths=runtime_paths)

    assert reordered_key.indexing_settings == key.indexing_settings


def test_file_mode_indexing_settings_ignore_semantic_only_settings(tmp_path: Path) -> None:
    """File-only metadata compatibility should not depend on semantic scan settings."""
    docs_path = tmp_path / "docs"
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        modes={"docs": "files"},
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)

    assert key.indexing_settings.embedder_provider == ""
    assert key.indexing_settings.embedder_model == ""
    assert key.indexing_settings.embedder_host == ""
    assert key.indexing_settings.embedder_dimensions == ""
    assert key.indexing_settings.chunk_size == ""
    assert key.indexing_settings.chunk_overlap == ""
    assert key.indexing_settings.include_extensions == ""
    assert key.indexing_settings.exclude_extensions == ""

    config.knowledge_bases["docs"].include_extensions = [".md"]
    config.knowledge_bases["docs"].exclude_extensions = [".png"]
    changed_key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)

    assert changed_key.indexing_settings == key.indexing_settings


@pytest.mark.asyncio
async def test_git_ready_index_schedules_refresh_after_poll_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale Git access can schedule refresh without scanning the local checkout."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git index", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", poll_interval_seconds=5)
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)

    async def _sync_success(self: GitKnowledgeSource) -> GitSyncResult:
        return _record_git_sync(self, GitSyncResult(head="rev-a", updated=False), "doc.md")

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync_success)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    _backdate_last_refresh(key)
    knowledge_registry._published_indexes.clear()
    scheduler = _RecordingScheduler(refreshing=False)
    unavailable: dict[str, KnowledgeAvailability] = {}
    _forbid_corpus_scans(monkeypatch, "git ready access should not scan the local corpus")
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is not None
    assert [document.content for document in await knowledge.asearch("git", 5)] == ["git index"]
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_private_git_schedule_refresh_on_access_honors_poll_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requester-local Git knowledge should not poll before its configured interval has elapsed."""
    runtime_paths = test_runtime_paths(tmp_path)
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", poll_interval_seconds=60)
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge", git=git_config),
                    ),
                ),
            },
        ),
        runtime_paths,
    )
    base_id = config.resolve_agent("helper").policy.private_knowledge_base_id
    assert base_id is not None
    identity = _identity("@alice:localhost")
    key = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
        create=True,
    )
    knowledge_path = Path(key.knowledge_path)
    knowledge_path.mkdir(parents=True, exist_ok=True)
    (knowledge_path / "note.md").write_text("alice private git note", encoding="utf-8")

    async def _sync_success(self: GitKnowledgeSource) -> GitSyncResult:
        return _record_git_sync(self, GitSyncResult(head="rev-a", updated=False), "note.md")

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync_success)
    await refresh_knowledge_binding(base_id, config=config, runtime_paths=runtime_paths, execution_identity=identity)
    scheduler = _RecordingScheduler(refreshing=False)
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        execution_identity=identity,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is not None
    assert unavailable == {}
    scheduler.schedule_refresh.assert_not_called()

    _backdate_last_refresh(key)
    knowledge_registry._published_indexes.clear()
    scheduler = _RecordingScheduler(refreshing=False)
    unavailable = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        execution_identity=identity,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    stale_knowledge = _resolution.knowledge

    assert stale_knowledge is not None
    assert unavailable == {base_id: KnowledgeAvailability.STALE}
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_private_git_updated_refresh_preserves_execution_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private Git updates must mark stale and rebuild through the requester binding."""
    runtime_paths = test_runtime_paths(tmp_path)
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge", git=git_config),
                    ),
                ),
            },
        ),
        runtime_paths,
    )
    base_id = config.resolve_agent("helper").policy.private_knowledge_base_id
    assert base_id is not None
    identity = _identity("@alice:localhost")
    key = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
        create=True,
    )
    knowledge_path = Path(key.knowledge_path)
    knowledge_path.mkdir(parents=True, exist_ok=True)
    (knowledge_path / "note.md").write_text("alice private git updated", encoding="utf-8")

    async def _sync_updated(self: GitKnowledgeSource) -> GitSyncResult:
        return _record_git_sync(self, GitSyncResult(head="rev-private", updated=True), "note.md")

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync_updated)

    result = await refresh_knowledge_binding(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )
    lookup = get_published_index(base_id, config=config, runtime_paths=runtime_paths, execution_identity=identity)

    assert result.index_published is True
    assert lookup.reader is not None
    assert lookup.availability is KnowledgeAvailability.READY
    assert [document.content for document in await lookup.reader.asearch("updated", limit=5)] == [
        "alice private git updated",
    ]


@pytest.mark.asyncio
async def test_existing_published_index_is_used_while_refresh_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow refresh builds a candidate while readers continue using the last-good index."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (docs_path / "doc.md").write_text("new index", encoding="utf-8")

    started = asyncio.Event()
    release = asyncio.Event()
    original_index_file = FileIndexer._index_file

    async def _block_candidate(self: FileIndexer, prepared: _PreparedFile, **kwargs: object) -> FileSignature | None:
        if not started.is_set():
            started.set()
            await release.wait()
        return await original_index_file(self, prepared, **kwargs)

    monkeypatch.setattr(FileIndexer, "_index_file", _block_candidate)
    refresh_task = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await started.wait()

    knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert knowledge is not None
    assert [document.content for document in await knowledge.asearch("index", 5)] == ["old index"]

    release.set()
    await refresh_task
    knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert knowledge is not None
    assert [document.content for document in await knowledge.asearch("index", 5)] == ["new index"]


@pytest.mark.asyncio
async def test_cancelled_refresh_keeps_unpublished_candidate_for_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a candidate refresh keeps its progress without publishing it, and the next refresh resumes it."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("cancel stable", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    storage_path = _storage(config, runtime_paths)
    existing_collections = set(list_collection_names(storage_path))
    published_before = _publication(config, runtime_paths)
    doc.write_text("cancel candidate", encoding="utf-8")
    candidate_started = asyncio.Event()
    original_index_file = FileIndexer._index_file

    async def _block_candidate(self: FileIndexer, prepared: _PreparedFile, **kwargs: object) -> FileSignature | None:
        candidate_started.set()
        await asyncio.Event().wait()
        return await original_index_file(self, prepared, **kwargs)

    monkeypatch.setattr(FileIndexer, "_index_file", _block_candidate)
    refresh_task = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await candidate_started.wait()
    cancelled_candidate_collections = set(list_collection_names(storage_path)) - existing_collections
    assert any("_candidate_" in collection for collection in cancelled_candidate_collections)

    refresh_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task

    # The candidate survives so the next refresh continues it instead of
    # restarting from zero, but it is still private: readers keep last-good.
    [candidate] = _working_collections(config, runtime_paths)
    assert candidate.name in cancelled_candidate_collections
    assert candidate.meta().stage == "building"
    assert _publication(config, runtime_paths) == published_before
    knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert knowledge is not None
    assert [document.content for document in await knowledge.asearch("cancel", 5)] == ["cancel stable"]

    monkeypatch.setattr(FileIndexer, "_index_file", original_index_file)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    publication = _publication(config, runtime_paths)
    assert result.index_published is True
    assert publication is not None
    assert publication.collection == candidate.name


@pytest.mark.asyncio
async def test_a_refresh_cancelled_while_it_publishes_keeps_the_publication_and_records_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling during the publication write finishes it, keeps the candidate it names, and counts as a success."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("stable metadata", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    cached_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert cached_lookup.reader is not None
    assert [document.content for document in await cached_lookup.reader.asearch("metadata", limit=5)] == [
        "stable metadata",
    ]
    doc.write_text("candidate metadata", encoding="utf-8")
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    mark_published_index_stale(key, reason="source_mutated")
    loop = asyncio.get_running_loop()
    publication_saved = asyncio.Event()
    release_publication_save = Event()
    original_save = knowledge_manager_module.save_publication

    def _block_after_candidate_publication(storage_path: Path, publication: Publication) -> None:
        original_save(storage_path, publication)
        loop.call_soon_threadsafe(publication_saved.set)
        assert release_publication_save.wait(timeout=SETTLE_TIMEOUT_SECONDS)

    monkeypatch.setattr(knowledge_manager_module, "save_publication", _block_after_candidate_publication)

    refresh_task = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await publication_saved.wait()
    refresh_task.cancel()
    await asyncio.sleep(0)
    release_publication_save.set()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task

    publication = load_publication(published_index_storage_path(key))
    assert publication is not None
    assert publication.collection is not None
    assert "_candidate_" in publication.collection
    assert KnowledgeCollection(path=published_index_storage_path(key), name=publication.collection).exists()
    assert _refresh_state_for_key(key) == "none"

    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.reader is not None
    assert lookup.availability is KnowledgeAvailability.READY
    assert [document.content for document in await lookup.reader.asearch("metadata", limit=5)] == [
        "candidate metadata",
    ]


@pytest.mark.asyncio
async def test_a_publication_write_finishes_before_repeated_cancellation_escapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated cancellation must not interrupt the publication write."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("published once", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
    loop = asyncio.get_running_loop()
    save_started = asyncio.Event()
    release_save = Event()
    original_save = knowledge_manager_module.save_publication

    def _blocked_save(storage_path: Path, publication: Publication) -> None:
        loop.call_soon_threadsafe(save_started.set)
        assert release_save.wait(timeout=SETTLE_TIMEOUT_SECONDS)
        original_save(storage_path, publication)

    monkeypatch.setattr(knowledge_manager_module, "save_publication", _blocked_save)
    reindex = asyncio.create_task(manager.reindex_all())
    await save_started.wait()
    try:
        reindex.cancel()
        await asyncio.sleep(0)
        reindex.cancel()
        await asyncio.sleep(0)
        assert not reindex.done(), "repeated cancellation escaped before the publication write finished"
    finally:
        release_save.set()

    with pytest.raises(asyncio.CancelledError):
        await reindex
    publication = load_publication(manager._base_storage_path)
    assert publication is not None
    assert publication.indexed_count == 1


@pytest.mark.asyncio
async def test_a_refresh_cancelled_while_its_publication_write_fails_records_no_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled refresh whose publication write failed surfaces the failure and is not recorded as published."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("never published", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    loop = asyncio.get_running_loop()
    save_started = asyncio.Event()
    release_save = Event()

    def _failed_save(*_args: object, **_kwargs: object) -> None:
        loop.call_soon_threadsafe(save_started.set)
        assert release_save.wait(timeout=SETTLE_TIMEOUT_SECONDS)
        msg = "publication write failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(knowledge_manager_module, "save_publication", _failed_save)
    refresh = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await save_started.wait()
    refresh.cancel()
    await asyncio.sleep(0)
    release_save.set()

    with pytest.raises(asyncio.CancelledError) as cancelled:
        await refresh
    cause = cancelled.value.__cause__
    assert isinstance(cause, RuntimeError)
    assert str(cause) == "publication write failed"
    assert _publication(config, runtime_paths) is None
    assert _refresh_status(config, runtime_paths) == RefreshStatus()


@pytest.mark.asyncio
async def test_the_refresh_child_publishes_without_touching_the_refresh_status(tmp_path: Path) -> None:
    """The child writes only the publication; the runtime's record of its success clears the failure it superseded."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("published", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    storage = published_index_storage_path(key)
    earlier = "2026-01-02T03:04:05+00:00"
    for _attempt in range(3):
        record_refresh_failed(key, error="boom")
    update_refresh_status(storage, lambda status: replace(status, last_refresh_at=earlier))
    status_file = storage / "refresh_status.json"
    status_bytes = status_file.read_bytes()

    child_result = await run_refresh_child("docs", config=config, runtime_paths=runtime_paths)

    # A first build compares no corpus, so it reports no source change to the base's aliases.
    assert replace(child_result, publication_made_at=None) == _RefreshChildResult(indexed_count=1, published=True)
    assert status_file.read_bytes() == status_bytes
    publication = load_publication(storage)
    assert publication is not None
    assert (publication.indexed_count, publication.settings) == (1, key.indexing_settings)

    (docs_path / "doc.md").write_text("published again", encoding="utf-8")
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    status = load_refresh_status(storage)

    assert result.index_published is True
    assert (status.reason, status.last_error, status.consecutive_refresh_failures) == (None, None, 0)
    assert status.last_refresh_at is not None
    assert status.last_refresh_at > earlier


@pytest.mark.asyncio
async def test_refresh_never_publishes_while_source_keeps_changing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source that changes on every pass keeps last-good and its candidate work.

    Published metadata stays bound to the exact corpus that was indexed, and a
    source mutating faster than the refresh converges must not cost the
    candidate the vectors it already built.
    """
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("stable index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("candidate index", encoding="utf-8")
    original_index_files = FileIndexer.index_files
    late_additions = 0

    async def _mutate_after_candidate_index(self: FileIndexer, files: Sequence[Path], **kwargs: object) -> int:
        nonlocal late_additions
        indexed_count = await original_index_files(self, files, **kwargs)
        late_additions += 1
        (docs_path / f"late{late_additions}.md").write_text("late addition", encoding="utf-8")
        return indexed_count

    monkeypatch.setattr(FileIndexer, "index_files", _mutate_after_candidate_index)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is False
    assert result.availability is KnowledgeAvailability.REFRESH_FAILED
    assert result.last_error == (
        "Knowledge source kept changing during refresh; candidate progress was kept for the next refresh"
    )
    assert lookup.reader is not None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED
    assert [document.content for document in await lookup.reader.asearch("index", limit=5)] == [
        "stable index",
    ]
    [candidate] = _working_collections(config, runtime_paths)
    assert "doc.md" in candidate.files()
    assert candidate.meta().stage == "building"


@pytest.mark.asyncio
async def test_refresh_reconciles_one_source_change_and_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single mid-refresh change is reconciled in the next pass instead of discarded."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("stable index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    original_index_files = FileIndexer.index_files
    mutated = False

    async def _mutate_once(self: FileIndexer, files: Sequence[Path], **kwargs: object) -> int:
        nonlocal mutated
        indexed_count = await original_index_files(self, files, **kwargs)
        if not mutated:
            mutated = True
            (docs_path / "late.md").write_text("late addition", encoding="utf-8")
        return indexed_count

    monkeypatch.setattr(FileIndexer, "index_files", _mutate_once)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert result.availability is KnowledgeAvailability.READY
    assert lookup.reader is not None
    assert sorted(document.meta_data["source_path"] for document in await lookup.reader.asearch("index", limit=5)) == [
        "doc.md",
        "late.md",
    ]
    # The first publication retires nothing, so no working collection outlives it.
    assert _working_collections(config, runtime_paths) == []


@pytest.mark.asyncio
async def test_same_physical_binding_refreshes_are_serialized_across_config_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh writes are serialized by physical storage target, not settings-sensitive index key."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    runtime_paths = runtime_paths_for(config)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    active_refreshes = 0
    max_active_refreshes = 0
    call_count = 0

    async def _blocked_reindex(self: KnowledgeManager, *, force_reindex: bool = False) -> RefreshOutcome:
        _ = force_reindex
        _ = self
        nonlocal active_refreshes, max_active_refreshes, call_count
        active_refreshes += 1
        max_active_refreshes = max(max_active_refreshes, active_refreshes)
        call_count += 1
        try:
            if call_count == 1:
                first_entered.set()
                await release_first.wait()
            else:
                second_entered.set()
            return RefreshOutcome(indexed_count=0, published=False, error=None)
        finally:
            active_refreshes -= 1

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _blocked_reindex)

    first_task = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await first_entered.wait()
    second_task = asyncio.create_task(
        refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths),
    )
    await asyncio.sleep(0)

    assert not second_entered.is_set()
    assert max_active_refreshes == 1

    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert second_entered.is_set()
    assert max_active_refreshes == 1


@pytest.mark.asyncio
async def test_shared_source_mutation_waits_for_duplicate_base_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate bases sharing one source folder must serialize refreshes and source mutations."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("index", encoding="utf-8")
    config = _config(tmp_path, bases={"alpha": docs_path, "beta": docs_path}, agent_bases=["alpha", "beta"])
    runtime_paths = runtime_paths_for(config)
    refresh_entered = asyncio.Event()
    release_refresh = asyncio.Event()
    mutation_entered = asyncio.Event()

    async def _blocked_reindex(self: KnowledgeManager, *, force_reindex: bool = False) -> RefreshOutcome:
        _ = force_reindex
        _ = self
        refresh_entered.set()
        await release_refresh.wait()
        return RefreshOutcome(indexed_count=0, published=False, error=None)

    async def _mutate_shared_source() -> None:
        async with knowledge_binding_mutation_lock("beta", config=config, runtime_paths=runtime_paths):
            mutation_entered.set()
            doc.write_text("mutated", encoding="utf-8")
            knowledge_registry._mark_knowledge_source_changed(
                "beta",
                config=config,
                runtime_paths=runtime_paths,
            )

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _blocked_reindex)

    refresh_task = asyncio.create_task(refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths))
    await refresh_entered.wait()
    mutation_task = asyncio.create_task(_mutate_shared_source())
    await asyncio.sleep(0)

    assert not mutation_entered.is_set()

    release_refresh.set()
    await asyncio.gather(refresh_task, mutation_task)

    assert mutation_entered.is_set()
    assert doc.read_text(encoding="utf-8") == "mutated"


def _record_source_file_locks(monkeypatch: pytest.MonkeyPatch) -> list[knowledge_registry.KnowledgeSourceRoot]:
    """Record each source root whose cross-process file lock is taken."""
    locked_roots: list[knowledge_registry.KnowledgeSourceRoot] = []
    original_path = knowledge_refresh_locks._refresh_file_lock_path

    def _record(source_root: knowledge_registry.KnowledgeSourceRoot) -> Path:
        locked_roots.append(source_root)
        return original_path(source_root)

    monkeypatch.setattr(knowledge_refresh_locks, "_refresh_file_lock_path", _record)
    return locked_roots


@pytest.mark.asyncio
async def test_refresh_uses_cross_process_source_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct refreshes should participate in the same source-root file lock as subprocess refreshes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    expected_source_root = knowledge_registry.source_root_for_published_index_key(key)
    locked_roots = _record_source_file_locks(monkeypatch)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert locked_roots == [expected_source_root]


@pytest.mark.asyncio
async def test_mutation_lock_uses_cross_process_source_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source mutations should participate in the same source-root file lock as refreshes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    refresh_target = knowledge_registry.resolve_refresh_target("docs", config=config, runtime_paths=runtime_paths)
    expected_source_root = knowledge_registry.source_root_for_refresh_target(refresh_target)
    locked_roots = _record_source_file_locks(monkeypatch)

    async with knowledge_binding_mutation_lock("docs", config=config, runtime_paths=runtime_paths):
        pass

    assert locked_roots == [expected_source_root]


@pytest.mark.asyncio
async def test_cancelled_async_file_lock_waiter_closes_unacquired_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled file-lock waiter must not leak a handle that can later acquire the lock."""
    lock_path = Path("/storage/docs.lock")
    opened = asyncio.Event()
    closed: list[FakeLockFile] = []
    released: list[int] = []

    class FakeLockFile:
        def fileno(self) -> int:
            return 123

        def close(self) -> None:
            closed.append(self)

    handle = FakeLockFile()

    def _open(_lock_path: Path) -> FakeLockFile:
        opened.set()
        return handle

    def _flock(file_descriptor: int, flags: int) -> None:
        assert file_descriptor == 123
        if flags & file_locks.fcntl.LOCK_EX:
            raise BlockingIOError
        released.append(flags)

    async def _wait_for_file_lock() -> None:
        async with file_locks.async_exclusive_file_lock(lock_path, poll_seconds=0.001):
            pytest.fail("lock waiter unexpectedly acquired the file lock")

    monkeypatch.setattr(file_locks, "_open_lock_file", _open)
    monkeypatch.setattr(file_locks.fcntl, "flock", _flock)

    waiter = asyncio.create_task(_wait_for_file_lock())
    await opened.wait()
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert closed == [handle]
    assert released == []


@pytest.mark.asyncio
async def test_refresh_generations_keep_latest_index_without_protecting_old_handles(tmp_path: Path) -> None:
    """Old read handles are best effort; refresh cleanup only guarantees the active index and the one it retired."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("generation one", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    first_publication = _publication(config, runtime_paths)
    assert first_publication is not None
    first_collection = first_publication.collection

    for generation in range(2, 7):
        doc.write_text(f"generation {generation}", encoding="utf-8")
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    latest = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert latest.reader is not None
    assert latest.publication is not None
    # Two collections take turns: each publication keeps the one it retired, the next refresh takes that one over,
    # and no third survives.
    assert set(list_collection_names(_storage(config, runtime_paths))) == {
        first_collection,
        latest.publication.collection,
    }
    assert latest.publication.retired == first_collection
    assert [document.content for document in await latest.reader.asearch("generation", limit=5)] == [
        "generation 6",
    ]


@pytest.mark.asyncio
async def test_a_cached_read_handle_serves_a_publication_another_process_wrote(tmp_path: Path) -> None:
    """A child-process publish is served by the parent's cached read handle, which reads the current publication."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("parent cached", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    cached_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert cached_lookup.reader is not None
    assert cached_lookup.publication is not None

    child_collection = "external_child_collection"
    seed_collection(
        KnowledgeCollection(path=published_index_storage_path(cached_lookup.key), name=child_collection),
        [{"content": "child published", "metadata": {"path": "doc.md"}}],
    )
    child_publication = replace(
        cached_lookup.publication,
        collection=child_collection,
        last_published_at="2026-04-27T00:00:00+00:00",
        source_signature="child-source-signature",
    )
    save_publication(published_index_storage_path(cached_lookup.key), child_publication)

    refreshed_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert refreshed_lookup.reader is cached_lookup.reader
    assert refreshed_lookup.publication == child_publication
    assert [document.content for document in await refreshed_lookup.reader.asearch("child", limit=5)] == [
        "child published",
    ]


@pytest.mark.asyncio
async def test_publish_invalidates_cached_indexes_for_same_physical_binding(tmp_path: Path) -> None:
    """A config transition and revert must not resurrect an older cached handle for the same path."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("config a", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    cached_a = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert cached_a.reader is not None
    assert [document.content for document in await cached_a.reader.asearch("config", limit=5)] == [
        "config a",
    ]

    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    doc.write_text("config b", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)
    reverted_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert reverted_lookup.reader is not None
    assert reverted_lookup.availability is KnowledgeAvailability.CONFIG_MISMATCH
    assert [document.content for document in await reverted_lookup.reader.asearch("config", limit=5)] == [
        "config b",
    ]


@pytest.mark.asyncio
async def test_successful_refreshes_keep_the_published_index_and_the_one_it_retired(tmp_path: Path) -> None:
    """Repeated publishes keep the published index and the one it retired, and clean older generations."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("generation 0", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    for generation in range(6):
        doc.write_text(f"generation {generation}", encoding="utf-8")
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    publication = load_publication(published_index_storage_path(key))
    assert publication is not None
    stored = set(list_collection_names(published_index_storage_path(key)))
    assert stored == {publication.collection, publication.retired}, (
        "only the published index and the one the next refresh resumes may stay"
    )
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.reader is not None
    assert [document.content for document in await lookup.reader.asearch("generation", limit=5)] == [
        "generation 5",
    ]


@pytest.mark.asyncio
async def test_refresh_rebuilds_malformed_metadata_without_serving_old_collection(tmp_path: Path) -> None:
    """Malformed metadata forces a fresh publish without serving the old collection."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("stale list old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
    default_collection = manager._collections._default_collection
    seed_collection(
        open_collection(manager._collections, default_collection),
        [{"content": "stale list old", "metadata": {"source_path": "doc.md"}}],
    )
    metadata_path = publication_path(published_index_storage_path(key))
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(["malformed"]), encoding="utf-8")
    doc.write_text("stale list new", encoding="utf-8")

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert not open_collection(manager._collections, default_collection).exists()
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.reader is not None
    assert [document.content for document in await lookup.reader.asearch("stale", limit=5)] == [
        "stale list new",
    ]


@pytest.mark.asyncio
async def test_superseded_collection_listing_failure_is_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup listing failures must not turn an already-committed publish into a refresh failure."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("cleanup old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("cleanup new", encoding="utf-8")

    published_before = _publication(config, runtime_paths)
    original_list = knowledge_collections_module.list_collection_names

    def _raise_list_collections_after_publication(storage_path: Path) -> tuple[str, ...]:
        if load_publication(storage_path) != published_before:
            msg = "list failed"
            raise RuntimeError(msg)
        return original_list(storage_path)

    monkeypatch.setattr(
        knowledge_collections_module,
        "list_collection_names",
        _raise_list_collections_after_publication,
    )
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert result.availability is KnowledgeAvailability.READY
    assert lookup.reader is not None
    assert lookup.availability is KnowledgeAvailability.READY
    assert [document.content for document in await lookup.reader.asearch("cleanup", limit=5)] == [
        "cleanup new",
    ]


@pytest.mark.asyncio
async def test_failed_refresh_preserves_last_good_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed candidate build marks stale availability but keeps serving the old collection."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("stable index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    (docs_path / "doc.md").write_text("broken refresh", encoding="utf-8")

    async def _fail_candidate(self: FileIndexer, prepared: _PreparedFile, **kwargs: object) -> NoReturn:
        del self, prepared, kwargs
        msg = "candidate failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(FileIndexer, "_index_file", _fail_candidate)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert result.index_published is False
    assert result.last_error == "candidate failed"

    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    assert knowledge is not None
    assert [document.content for document in await knowledge.asearch("index", 5)] == ["stable index"]


@pytest.mark.asyncio
async def test_metadata_save_failure_after_candidate_index_keeps_serving_last_good(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate whose metadata did not commit must not replace the published read handle."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("stable metadata index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("uncommitted candidate index", encoding="utf-8")

    def _fail_candidate_publication(_storage_path: Path, _publication: Publication) -> NoReturn:
        msg = "metadata commit failed"
        raise OSError(msg)

    monkeypatch.setattr(knowledge_manager_module, "save_publication", _fail_candidate_publication)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert result.index_published is False
    assert result.last_error == "metadata commit failed"

    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    assert knowledge is not None
    assert [document.content for document in await knowledge.asearch("index", 5)] == [
        "stable metadata index",
    ]


@pytest.mark.asyncio
async def test_partial_refresh_after_cached_index_updates_failed_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial refresh must not leave the process-local READY index hiding failure metadata."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "good.md").write_text("last good", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge is not None
    (docs_path / "bad.md").write_text("bad candidate", encoding="utf-8")
    original_index_file = FileIndexer._index_file

    async def _skip_bad_file(self: FileIndexer, prepared: _PreparedFile, **kwargs: object) -> FileSignature | None:
        if prepared.path.name == "bad.md":
            return None
        return await original_index_file(self, prepared, **kwargs)

    monkeypatch.setattr(FileIndexer, "_index_file", _skip_bad_file)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert result.index_published is False
    assert result.availability is KnowledgeAvailability.REFRESH_FAILED
    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    assert knowledge is not None
    assert [document.content for document in await knowledge.asearch("good", 5)] == ["last good"]


@pytest.mark.asyncio
async def test_embedder_config_mismatch_returns_no_incompatible_index(tmp_path: Path) -> None:
    """An embedder-changing config mismatch should not query old vectors with the new embedder."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old embedder index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    changed_config = config.model_copy(deep=True)
    changed_config.memory.embedder.config.model = "text-embedding-3-large"
    scheduler = _RecordingScheduler(refreshing=False)
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        changed_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is None
    assert unavailable == {"docs": KnowledgeAvailability.CONFIG_MISMATCH}
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_config_mismatch_refresh_cooldown_is_settings_aware(tmp_path: Path) -> None:
    """A newer config mismatch for the same binding must not be dropped by the request cooldown."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    newer_config = config.model_copy(deep=True)
    newer_config.knowledge_bases["docs"].chunk_size = 2048
    scheduler = _RecordingScheduler(refreshing=False)

    assert (
        resolve_agent_knowledge_access("helper", changed_config, runtime_paths, refresh_scheduler=scheduler).knowledge
        is not None
    )
    assert (
        resolve_agent_knowledge_access("helper", newer_config, runtime_paths, refresh_scheduler=scheduler).knowledge
        is not None
    )

    assert scheduler.schedule_refresh.call_count == 2
    assert scheduler.schedule_refresh.call_args_list[0].kwargs["config"] is changed_config
    assert scheduler.schedule_refresh.call_args_list[1].kwargs["config"] is newer_config


@pytest.mark.asyncio
async def test_initializing_refresh_cooldown_is_settings_aware(tmp_path: Path) -> None:
    """A cold initial load under old settings must not suppress a newer config's initial load."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    newer_config = config.model_copy(deep=True)
    newer_config.knowledge_bases["docs"].chunk_size = 2048
    scheduler = _RecordingScheduler(refreshing=False)

    assert (
        resolve_agent_knowledge_access("helper", changed_config, runtime_paths, refresh_scheduler=scheduler).knowledge
        is None
    )
    assert (
        resolve_agent_knowledge_access("helper", newer_config, runtime_paths, refresh_scheduler=scheduler).knowledge
        is None
    )

    assert scheduler.schedule_refresh.call_count == 2
    assert scheduler.schedule_refresh.call_args_list[0].kwargs["config"] is changed_config
    assert scheduler.schedule_refresh.call_args_list[1].kwargs["config"] is newer_config


@pytest.mark.asyncio
async def test_cold_failed_refresh_cooldown_is_settings_aware(tmp_path: Path) -> None:
    """A failed cold refresh under old settings must not suppress a newer config's retry."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    record_refresh_failed(key, error="cold failure")
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    newer_config = config.model_copy(deep=True)
    newer_config.knowledge_bases["docs"].chunk_size = 2048
    scheduler = _RecordingScheduler(refreshing=False)
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        changed_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    _resolution = resolve_agent_knowledge_access(
        "helper",
        newer_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})

    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    assert scheduler.schedule_refresh.call_count == 2
    assert scheduler.schedule_refresh.call_args_list[0].kwargs["config"] is changed_config
    assert scheduler.schedule_refresh.call_args_list[1].kwargs["config"] is newer_config


@pytest.mark.asyncio
async def test_refresh_failed_detail_carries_persisted_last_error(tmp_path: Path) -> None:
    """The availability detail exposes the persisted refresh failure cause."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    record_refresh_failed(
        key,
        error="Indexed 0 of 1 managed knowledge files (first error: embedder authentication failed (HTTP 401))",
    )
    scheduler = _RecordingScheduler(refreshing=False)

    resolution = resolve_agent_knowledge_access("helper", config, runtime_paths, refresh_scheduler=scheduler)

    detail = resolution.unavailable["docs"]
    assert detail.availability is KnowledgeAvailability.REFRESH_FAILED
    assert detail.last_error == (
        "Indexed 0 of 1 managed knowledge files (first error: embedder authentication failed (HTTP 401))"
    )


@pytest.mark.asyncio
async def test_a_refresh_in_flight_hides_the_failure_it_retries(tmp_path: Path) -> None:
    """While the scheduler refreshes a base, its last failure is not reported: a published base reads as stale."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("last good", encoding="utf-8")
    config = _config(
        tmp_path,
        bases={"docs": docs_path, "cold": tmp_path / "cold"},
        agent_bases=["docs", "cold"],
    )
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    for base_id in ("docs", "cold"):
        record_refresh_failed(
            resolve_published_index_key(base_id, config=config, runtime_paths=runtime_paths, create=True),
            error="previous failure",
        )

    idle = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=_RecordingScheduler(refreshing=False),
    )
    refreshing = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=_RecordingScheduler(refreshing=True),
    )

    assert idle.unavailable == {
        "docs": KnowledgeAvailabilityDetail(
            availability=KnowledgeAvailability.REFRESH_FAILED,
            search_available=True,
            last_error="previous failure",
        ),
        "cold": KnowledgeAvailabilityDetail(
            availability=KnowledgeAvailability.REFRESH_FAILED,
            search_available=False,
            last_error="previous failure",
        ),
    }
    assert refreshing.unavailable == {
        "docs": KnowledgeAvailabilityDetail(availability=KnowledgeAvailability.STALE, search_available=True),
        "cold": KnowledgeAvailabilityDetail(availability=KnowledgeAvailability.INITIALIZING, search_available=False),
    }
    assert refreshing.knowledge is not None
    assert [document.content for document in await refreshing.knowledge.asearch("good", 5)] == ["last good"]


@pytest.mark.asyncio
async def test_failed_git_refresh_cooldown_is_credentials_service_aware(tmp_path: Path) -> None:
    """Changing Git auth service config should bypass the failed-refresh retry cooldown."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    git_config = KnowledgeGitConfig(
        repo_url="https://example.com/org/private.git",
        credentials_service="old_service",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    record_refresh_failed(key, error="auth failed")
    changed_config = config.model_copy(deep=True)
    changed_git_config = changed_config.knowledge_bases["docs"].git
    assert changed_git_config is not None
    changed_git_config.credentials_service = "new_service"
    scheduler = _RecordingScheduler(refreshing=False)

    assert (
        resolve_agent_knowledge_access("helper", config, runtime_paths, refresh_scheduler=scheduler).knowledge is None
    )
    assert (
        resolve_agent_knowledge_access("helper", changed_config, runtime_paths, refresh_scheduler=scheduler).knowledge
        is None
    )

    assert scheduler.schedule_refresh.call_count == 2
    assert scheduler.schedule_refresh.call_args_list[0].kwargs["config"] is config
    assert scheduler.schedule_refresh.call_args_list[1].kwargs["config"] is changed_config


@pytest.mark.asyncio
async def test_failed_git_refresh_cooldown_is_embedded_userinfo_aware(tmp_path: Path) -> None:
    """Changing embedded Git URL auth should bypass cooldown without storing the secret."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    git_config = KnowledgeGitConfig(
        repo_url="https://git-user:old-secret@example.com/org/private.git",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    record_refresh_failed(key, error="auth failed")
    changed_config = config.model_copy(deep=True)
    changed_git_config = changed_config.knowledge_bases["docs"].git
    assert changed_git_config is not None
    changed_git_config.repo_url = "https://git-user:new-secret@example.com/org/private.git"
    scheduler = _RecordingScheduler(refreshing=False)

    assert (
        resolve_agent_knowledge_access("helper", config, runtime_paths, refresh_scheduler=scheduler).knowledge is None
    )
    assert (
        resolve_agent_knowledge_access("helper", changed_config, runtime_paths, refresh_scheduler=scheduler).knowledge
        is None
    )

    assert scheduler.schedule_refresh.call_count == 2
    assert scheduler.schedule_refresh.call_args_list[0].kwargs["config"] is config
    assert scheduler.schedule_refresh.call_args_list[1].kwargs["config"] is changed_config
    cooldown_keys = repr(tuple(scheduler._scheduled_at))
    assert "old-secret" not in cooldown_keys
    assert "new-secret" not in cooldown_keys


@pytest.mark.asyncio
@pytest.mark.parametrize("availability", [KnowledgeAvailability.STALE, KnowledgeAvailability.REFRESH_FAILED])
async def test_stale_or_failed_index_reports_chunking_config_mismatch_before_cooldown(
    tmp_path: Path,
    availability: KnowledgeAvailability,
) -> None:
    """Stale/failed metadata must not suppress refreshes for newer chunking settings."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    if availability is KnowledgeAvailability.STALE:
        knowledge_registry.mark_published_index_stale(key, reason="test_stale")
    else:
        record_refresh_failed(key, error="previous failure")
    knowledge_registry._published_indexes.clear()
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    newer_config = config.model_copy(deep=True)
    newer_config.knowledge_bases["docs"].chunk_size = 2048
    scheduler = _RecordingScheduler(refreshing=False)
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        changed_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    assert _resolution.knowledge is not None
    _resolution = resolve_agent_knowledge_access(
        "helper",
        newer_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    assert _resolution.knowledge is not None

    assert unavailable == {"docs": KnowledgeAvailability.CONFIG_MISMATCH}
    assert scheduler.schedule_refresh.call_count == 2
    assert scheduler.schedule_refresh.call_args_list[0].kwargs["config"] is changed_config
    assert scheduler.schedule_refresh.call_args_list[1].kwargs["config"] is newer_config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda config: setattr(config.knowledge_bases["docs"].git, "repo_url", "https://example.com/other/repo.git"),
        lambda config: setattr(config.knowledge_bases["docs"].git, "branch", "release"),
        lambda config: setattr(config.knowledge_bases["docs"].git, "include_patterns", ["other/**"]),
        lambda config: setattr(config.knowledge_bases["docs"].git, "exclude_patterns", ["doc.md"]),
        lambda config: setattr(config.knowledge_bases["docs"].git, "skip_hidden", False),
        lambda config: setattr(config.knowledge_bases["docs"], "include_extensions", [".txt"]),
        lambda config: setattr(config.knowledge_bases["docs"], "exclude_extensions", [".md"]),
    ],
)
async def test_corpus_changing_config_mismatch_returns_no_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: object,
) -> None:
    """Source identity and membership filter changes must not serve old content."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old corpus index", encoding="utf-8")
    git_config = KnowledgeGitConfig(
        repo_url="https://example.com/org/repo.git",
        include_patterns=["**/*.md"],
        skip_hidden=True,
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)

    async def _sync_success(self: GitKnowledgeSource) -> GitSyncResult:
        return _record_git_sync(self, GitSyncResult(head="rev-a", updated=True), "doc.md")

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync_success)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    changed_config = config.model_copy(deep=True)
    mutate(changed_config)
    scheduler = _RecordingScheduler(refreshing=False)
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        changed_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is None
    assert unavailable == {"docs": KnowledgeAvailability.CONFIG_MISMATCH}
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_skip_hidden_change_on_directory_base_returns_no_index(tmp_path: Path) -> None:
    """Toggling skip_hidden on a directory base changes corpus membership, so old content must not be served."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old corpus index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].skip_hidden = False
    scheduler = _RecordingScheduler(refreshing=False)
    resolution = resolve_agent_knowledge_access(
        "helper",
        changed_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )

    assert resolution.knowledge is None
    assert {base_id: detail.availability for (base_id, detail) in resolution.unavailable.items()} == {
        "docs": KnowledgeAvailability.CONFIG_MISMATCH,
    }
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_failed_refresh_after_config_change_preserves_published_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed candidate refresh must not rewrite last-good metadata to the attempted settings."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("stable index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    old_publication = _publication(config, runtime_paths)
    assert old_publication is not None

    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024

    async def _fail_candidate(self: FileIndexer, prepared: _PreparedFile, **kwargs: object) -> NoReturn:
        del self, prepared, kwargs
        msg = "candidate failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(FileIndexer, "_index_file", _fail_candidate)
    result = await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)
    assert result.last_error == "candidate failed"

    changed_key = resolve_published_index_key("docs", config=changed_config, runtime_paths=runtime_paths)
    assert _publication(changed_config, runtime_paths) == old_publication
    assert _refresh_state_for_key(changed_key) == "refresh_failed"
    assert _refresh_status(changed_config, runtime_paths).last_error == "candidate failed"

    lookup = get_published_index("docs", config=changed_config, runtime_paths=runtime_paths)
    assert lookup.reader is not None
    assert lookup.availability is KnowledgeAvailability.CONFIG_MISMATCH


def test_stale_metadata_without_collection_returns_unavailable_index(tmp_path: Path) -> None:
    """Metadata alone must not create or expose an empty ready collection."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    _publish(key, collection="missing_collection")

    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert lookup.reader is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED
    assert not KnowledgeCollection(path=published_index_storage_path(key), name="missing_collection").exists()


def test_lookup_failure_after_binding_resolution_schedules_repair_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved binding with a broken read handle should still queue a repair refresh."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    _publish(key, collection="broken_collection")
    scheduler = _RecordingScheduler(refreshing=False)
    unavailable: dict[str, KnowledgeAvailability] = {}

    def _broken_reader(*_args: object, **_kwargs: object) -> object:
        msg = "cannot open collection"
        raise RuntimeError(msg)

    monkeypatch.setattr("mindroom.knowledge.registry._build_published_index_reader", _broken_reader)
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is None
    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    scheduler.schedule_refresh.assert_called_once()


def test_published_index_handle_open_failure_degrades_and_schedules_repair_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken read handle should not break reply-path knowledge resolution."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    collection = "broken_collection"
    _publish(key, collection=collection)
    seed_collection(
        KnowledgeCollection(path=published_index_storage_path(key), name=collection),
        [{"content": "last good content", "metadata": {"path": "guide.md"}}],
    )
    scheduler = _RecordingScheduler(refreshing=False)

    def _broken_reader(*_args: object, **_kwargs: object) -> object:
        msg = "cannot open collection"
        raise RuntimeError(msg)

    monkeypatch.setattr("mindroom.knowledge.registry._build_published_index_reader", _broken_reader)

    resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )

    assert resolution.knowledge is None
    assert resolution.unavailable == {
        "docs": KnowledgeAvailabilityDetail(
            availability=KnowledgeAvailability.REFRESH_FAILED,
            search_available=False,
        ),
    }
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_first_time_partial_refresh_does_not_publish_ready_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold refresh with incomplete file indexing must not become a last-good index."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "good.md").write_text("good", encoding="utf-8")
    (docs_path / "bad.md").write_text("bad", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    original_index_file = FileIndexer._index_file

    async def _skip_bad_file(self: FileIndexer, prepared: _PreparedFile, **kwargs: object) -> FileSignature | None:
        if prepared.path.name == "bad.md":
            return None
        return await original_index_file(self, prepared, **kwargs)

    monkeypatch.setattr(FileIndexer, "_index_file", _skip_bad_file)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.indexed_count == 1
    assert result.index_published is False
    assert _publication(config, runtime_paths) is None
    assert _refresh_state_for_key(key) == "refresh_failed"
    assert _refresh_status(config, runtime_paths).last_error == "Indexed 1 of 2 managed knowledge files"
    assert lookup.reader is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED
    # The partial candidate stays private but durable: "good.md" must not be
    # embedded again on the next attempt just because "bad.md" failed.
    [candidate] = _working_collections(config, runtime_paths)
    assert "_candidate_" in candidate.name
    assert set(candidate.files()) == {"good.md"}
    assert candidate.claim_counts() == (1, 1)
    candidate_status = get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths).candidate
    assert candidate_status is not None
    assert candidate_status.status == "failed"
    assert (candidate_status.completed_count, candidate_status.failed_count) == (1, 1)


def _embedder_rejection_error() -> EmbedderRequestError:
    """Return what the embedder raises for a rejected input: the SDK error classified at its boundary."""
    request = httpx.Request("POST", "http://embeddings.local/v1/embeddings")
    response = httpx.Response(422, request=request, json={"error": {"message": "Input could not be embedded"}})
    return openai_embedder_error(UnprocessableEntityError("Error code: 422", response=response, body=None))


@pytest.mark.skipif(os.name == "nt", reason="flock probe requires POSIX")
@pytest.mark.asyncio
async def test_a_candidate_cleanup_during_a_status_read_does_not_fail_the_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced rebuild deleting the failed candidate a status read just listed leaves that read's counts intact."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "good.md").write_text("good", encoding="utf-8")
    (docs_path / "bad.md").write_text("bad", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    original_index_file = FileIndexer._index_file

    async def _skip_bad_file(self: FileIndexer, prepared: _PreparedFile, **kwargs: object) -> FileSignature | None:
        if prepared.path.name == "bad.md":
            return None
        return await original_index_file(self, prepared, **kwargs)

    monkeypatch.setattr(FileIndexer, "_index_file", _skip_bad_file)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert len(_working_collections(config, runtime_paths)) == 1

    def _list_then_clean_up(space: CollectionSpace, *, published: str | None, settings: IndexingSettings) -> object:
        listed = working_collections(space, published=published, settings=settings)
        with (space.storage_path / "collection_lifetime.lock").open("a", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # The read holds the collections, so the cleanup would wait for it to finish.
                return listed
            fcntl.flock(handle, fcntl.LOCK_UN)
        cleanup_superseded_collections(space, preserved=frozenset(), candidates_only=True)
        return listed

    monkeypatch.setattr(knowledge_status_module, "working_collections", _list_then_clean_up)

    candidate_status = get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths).candidate

    assert candidate_status is not None
    assert (candidate_status.completed_count, candidate_status.failed_count) == (1, 1)


@pytest.mark.asyncio
async def test_partial_refresh_error_includes_first_classified_file_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persisted refresh summary carries the first classified per-file indexing error past a later success.

    The rejection is per-file on purpose: a rejected credential stops every file not yet started, so "good.md"
    would never get its turn.
    """
    # One file at a time, in name order, so "good.md" succeeds after the rejection is recorded.
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=1\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "aaa-broken.md").write_text("cannot embed", encoding="utf-8")
    (docs_path / "good.md").write_text("indexed text", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])

    class _RejectingEmbedder(_FakeEmbedder):
        def embed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
            if "cannot embed" in texts:
                raise _embedder_rejection_error()
            return super().embed(texts, purpose=purpose)

    monkeypatch.setattr(
        "mindroom.knowledge.manager.create_configured_embedder",
        lambda *_a, **_k: _RejectingEmbedder(),
    )
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))
    assert knowledge_file_indexer.max_concurrent_file_indexes(runtime_paths_for(config)) == 1

    assert await manager.reindex_all() == RefreshOutcome(
        indexed_count=1,
        published=False,
        error="Indexed 1 of 2 managed knowledge files (first error: embedder request failed (HTTP 422))",
    )


@pytest.mark.asyncio
async def test_vectorless_file_does_not_inherit_process_global_embedder_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vectorless file is not blamed for a stale failure from another request."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "broken.md").write_text("cannot embed", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])

    def _no_chunks(documents: object) -> list[object]:
        # Simulate an unrelated request recording health while this file's
        # read yields nothing to embed.
        del documents
        embedder_health.capture_embedder_health_recorder().record("embedder authentication failed (HTTP 401)")
        return []

    monkeypatch.setattr(knowledge_file_indexer, "_knowledge_chunks", _no_chunks)
    embedder_health.capture_embedder_health_recorder().record(None)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))
    try:
        outcome = await manager.reindex_all()
    finally:
        embedder_health.capture_embedder_health_recorder().record(None)

    assert outcome == RefreshOutcome(
        indexed_count=0,
        published=False,
        error="Indexed 0 of 1 managed knowledge files",
    )


def test_refresh_failure_counter_increments_and_resets_preserving_last_good(tmp_path: Path) -> None:
    """The failure counter climbs across failures, leaves the publication alone, and resets on success."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    storage = published_index_storage_path(key)
    publication = _publish(key, collection="docs_live", source_signature="sig")

    record_refresh_failed(key, error="boom 1")
    first = load_refresh_status(storage)
    assert first.consecutive_refresh_failures == 1
    assert load_publication(storage) == publication

    record_refresh_failed(key, error="boom 2")
    second = load_refresh_status(storage)
    assert second.consecutive_refresh_failures == 2
    assert second.last_error == "boom 2"

    record_refresh_succeeded(key, reason_generation=knowledge_registry.refresh_reason_generation(key))
    recovered = load_refresh_status(storage)
    assert recovered.consecutive_refresh_failures == 0
    assert recovered.last_error is None
    assert load_publication(storage) == publication


def test_refresh_failure_threshold_logs_error_at_three_and_beyond(tmp_path: Path) -> None:
    """The third consecutive failure and every later one log at ERROR level."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)

    with capture_logs() as logs:
        for attempt in range(1, 5):
            record_refresh_failed(key, error=f"boom {attempt}")

    repeated = [entry for entry in logs if entry["event"] == "knowledge_refresh_failing_repeatedly"]
    assert [entry["consecutive_refresh_failures"] for entry in repeated] == [3, 4]
    assert all(entry["log_level"] == "error" for entry in repeated)


def test_a_refresh_status_without_a_failure_counter_parses_as_zero(tmp_path: Path) -> None:
    """A refresh status missing its failure counter loads as zero and increments from there."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    storage = published_index_storage_path(key)
    status_path = storage / "refresh_status.json"
    record_refresh_failed(key, error="boom 1")
    record_refresh_failed(key, error="boom 2")
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    del payload["consecutive_refresh_failures"]
    status_path.write_text(json.dumps(payload), encoding="utf-8")

    status = load_refresh_status(storage)
    assert status.consecutive_refresh_failures == 0
    assert status.last_error == "boom 2"

    record_refresh_failed(key, error="boom 3")
    assert load_refresh_status(storage).consecutive_refresh_failures == 1


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_source_change_it_did_not_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that died without publishing records its failure beside the stale mark, which stays."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    publication = _publish(key, collection="old", source_signature="old-source")
    mark_published_index_stale(key, reason="source_changed")

    async def _died(*_args: object, **_kwargs: object) -> NoReturn:
        msg = "Knowledge refresh subprocess failed for 'docs' with exit code -9"
        raise _RefreshChildFailedError(msg, started=True)

    monkeypatch.setattr(knowledge_refresh_runner, "_run_refresh_child", _died)
    with pytest.raises(_RefreshChildFailedError):
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    status = load_refresh_status(published_index_storage_path(key))

    assert load_publication(published_index_storage_path(key)) == publication
    assert status.reason == "source_changed"
    assert status.last_error == "Knowledge refresh subprocess failed for 'docs' with exit code -9"
    assert _refresh_state_for_key(key) == "refresh_failed"


@pytest.mark.asyncio
async def test_cold_refresh_publishes_when_empty_file_produces_no_vectors(tmp_path: Path) -> None:
    """Empty managed files should count as scanned without blocking cold index publication."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "useful.md").write_text("useful vectors", encoding="utf-8")
    (docs_path / "empty.md").write_text("", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    publication = load_publication(published_index_storage_path(key))
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.indexed_count == 2
    assert result.index_published is True
    assert result.availability is KnowledgeAvailability.READY
    assert publication is not None
    assert publication.indexed_count == 2
    assert _refresh_state_for_key(key) == "none"
    assert lookup.reader is not None
    assert [document.content for document in await lookup.reader.asearch("useful", limit=5)] == [
        "useful vectors",
    ]


@pytest.mark.asyncio
async def test_embedder_changing_partial_refresh_does_not_publish_old_index_under_new_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial refresh cannot cache old incompatible vectors under a new index key."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("old embedder index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("new embedder candidate", encoding="utf-8")
    changed_config = config.model_copy(deep=True)
    changed_config.memory.embedder.config.model = "text-embedding-3-large"

    async def _partial_candidate(self: FileIndexer, prepared: _PreparedFile, **kwargs: object) -> None:
        _ = (self, prepared, kwargs)

    monkeypatch.setattr(FileIndexer, "_index_file", _partial_candidate)

    result = await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=changed_config, runtime_paths=runtime_paths)

    assert result.indexed_count == 0
    assert result.index_published is False
    assert lookup.reader is None
    assert lookup.availability is KnowledgeAvailability.CONFIG_MISMATCH


@pytest.mark.asyncio
async def test_cold_refresh_exception_surfaces_failed_availability_and_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold refresh failures remain visible and do not reschedule on every access."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("broken", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    async def _raise_reindex(self: KnowledgeManager, *, force_reindex: bool = False) -> RefreshOutcome:
        _ = force_reindex
        _ = self
        msg = "cold refresh failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _raise_reindex)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert result.last_error == "cold refresh failed"
    assert result.availability is KnowledgeAvailability.REFRESH_FAILED

    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.reader is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED

    scheduler = _RecordingScheduler(refreshing=False)
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    first = _resolution.knowledge
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    second = _resolution.knowledge

    assert first is None
    assert second is None
    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_refresh_setup_failure_records_failed_availability(tmp_path: Path) -> None:
    """Manager construction failures are persisted instead of leaving cold metadata initializing."""
    docs_path = tmp_path / "docs"
    docs_path.write_text("not a directory", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    last_error = _refresh_status(config, runtime_paths).last_error
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is False
    assert _publication(config, runtime_paths) is None
    assert last_error is not None
    assert "must be a directory" in last_error
    assert _refresh_state_for_key(key) == "refresh_failed"
    assert lookup.reader is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED


@pytest.mark.asyncio
async def test_api_delete_marks_index_stale_and_keeps_last_good_best_effort(tmp_path: Path) -> None:
    """DELETE success schedules refresh while old vectors remain usable until refresh publishes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("delete me now", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    before_delete = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert before_delete is not None
    assert [document.content for document in await before_delete.asearch("delete", 5)] == ["delete me now"]

    scheduler = _RecordingScheduler(refreshing=False)
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = scheduler
    client = TestClient(main.app, base_url="http://localhost")

    response = client.delete("/api/knowledge/bases/docs/files/guide.md")
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    after_delete = _resolution.knowledge

    assert response.status_code == 200
    assert after_delete is not None
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    assert [document.content for document in await after_delete.asearch("delete", 5)] == ["delete me now"]
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_api_replacement_upload_marks_index_stale_and_keeps_last_good_best_effort(
    tmp_path: Path,
) -> None:
    """Replacement uploads leave old vectors usable until refresh publishes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("old upload", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    scheduler = _RecordingScheduler(refreshing=False)
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = scheduler
    client = TestClient(main.app, base_url="http://localhost")

    response = client.post(
        "/api/knowledge/bases/docs/upload",
        files=[("files", ("guide.md", b"new upload", "text/markdown"))],
    )
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert response.status_code == 200
    assert knowledge is not None
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    assert [document.content for document in await knowledge.asearch("old upload", 5)] == ["old upload"]
    assert [document.content for document in await knowledge.asearch("new upload", 5)] == ["old upload"]
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_api_upload_failure_does_not_commit_earlier_staged_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed upload batch leaves the source tree and published index unchanged."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("existing upload", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    scheduler = _RecordingScheduler(refreshing=False)
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = scheduler
    monkeypatch.setattr("mindroom.api.knowledge._MAX_UPLOAD_BYTES", 5)
    client = TestClient(main.app, base_url="http://localhost")

    response = client.post(
        "/api/knowledge/bases/docs/upload",
        files=[
            ("files", ("guide.md", b"small", "text/markdown")),
            ("files", ("new.md", b"too large", "text/markdown")),
        ],
    )

    assert response.status_code == 413
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge
    assert knowledge is not None
    assert unavailable == {}
    assert (docs_path / "guide.md").read_text(encoding="utf-8") == "existing upload"
    assert not (docs_path / "new.md").exists()
    assert [document.content for document in await knowledge.asearch("existing upload", 5)] == [
        "existing upload",
    ]
    scheduler.schedule_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_api_status_reports_a_refresh_of_the_process_scheduler_and_hides_the_failure_it_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status polling sees any refresh the process's scheduler runs, and a failure it retries is not reported."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("refreshing status", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    scheduler = runtime_refresh_scheduler(runtime_paths)
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = scheduler
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    record_refresh_failed(key, error="previous failure")
    started, release = _block_reindex(monkeypatch)
    refresh_task = asyncio.create_task(
        scheduler.refresh_now("docs", config=config, runtime_paths=runtime_paths, force_reindex=True),
    )
    await asyncio.wait_for(started.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    try:
        client = TestClient(main.app, base_url="http://localhost")
        response = client.get("/api/knowledge/bases/docs/status")
    finally:
        release.set()
        await refresh_task
        config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = None
        await scheduler.shutdown()

    assert response.status_code == 200
    payload = response.json()
    assert payload["refreshing"] is True
    assert payload["refresh_state"] == "refreshing"
    assert payload["last_error"] is None


@pytest.mark.asyncio
async def test_refresh_scheduler_runs_independent_per_binding_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduling one binding does not replace, cancel, or wait for another binding."""
    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    config = _config(tmp_path, bases={"a": docs_a, "b": docs_b}, agent_bases=["a", "b"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler(max_concurrent_refreshes=2)
    started: list[str] = []
    release: dict[str, asyncio.Event] = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def _fake_refresh(base_id: str, **_kwargs: object) -> object:
        started.append(base_id)
        await release[base_id].wait()
        if base_id == "a":
            msg = "a failed"
            raise RuntimeError(msg)
        return object()

    monkeypatch.setattr("mindroom.knowledge.refresh_runner.refresh_knowledge_binding_in_subprocess", _fake_refresh)

    scheduler.schedule_refresh("a", config=config, runtime_paths=runtime_paths)
    scheduler.schedule_refresh("a", config=config, runtime_paths=runtime_paths)
    scheduler.schedule_refresh("b", config=config, runtime_paths=runtime_paths)
    await asyncio.sleep(0)

    assert sorted(started) == ["a", "b"]
    assert len(scheduler._tasks) == 2
    release["b"].set()
    await asyncio.sleep(0)
    assert any(key.base_id == "a" for key in scheduler._tasks)
    release["a"].set()
    await scheduler.shutdown()


_TURN_OWNER: contextvars.ContextVar[object | None] = contextvars.ContextVar("test_refresh_turn_owner", default=None)


@pytest.mark.asyncio
async def test_refresh_scheduled_during_a_turn_does_not_retain_the_turn_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh can outlive the tool call that schedules it, whose turn context holds the turn's Agent."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _fake_refresh(_base_id: str, **_kwargs: object) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr("mindroom.knowledge.refresh_runner.refresh_knowledge_binding_in_subprocess", _fake_refresh)

    class _TurnAgent:
        pass

    agent = _TurnAgent()
    agent_ref = weakref.ref(agent)
    token = _TURN_OWNER.set(agent)
    try:
        scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    finally:
        _TURN_OWNER.reset(token)
    del agent
    await asyncio.wait_for(started.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    gc.collect()

    try:
        assert agent_ref() is None
    finally:
        release.set()
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_refresh_queued_behind_a_running_refresh_does_not_retain_the_turn_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh a turn queues behind one already running must not keep the scheduling turn's Agent alive."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def _fake_refresh(_base_id: str, **_kwargs: object) -> None:
        if first_started.is_set():
            second_started.set()
            return
        first_started.set()
        await release_first.wait()

    monkeypatch.setattr("mindroom.knowledge.refresh_runner.refresh_knowledge_binding_in_subprocess", _fake_refresh)

    class _TurnAgent:
        pass

    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await asyncio.wait_for(first_started.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    agent = _TurnAgent()
    agent_ref = weakref.ref(agent)
    token = _TURN_OWNER.set(agent)
    try:
        scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    finally:
        _TURN_OWNER.reset(token)
    del agent

    try:
        assert not second_started.is_set()
        gc.collect()
        assert agent_ref() is None
        release_first.set()
        await asyncio.wait_for(second_started.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    finally:
        release_first.set()
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_refresh_scheduler_does_not_deep_copy_config_on_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduling a refresh must not deep-copy the full config on the event loop."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    refreshed = asyncio.Event()

    async def _fake_refresh(_base_id: str, **_kwargs: object) -> None:
        refreshed.set()

    def _unexpected_copy(_self: Config, **_kwargs: object) -> Config:
        pytest.fail("schedule_refresh deep-copied the config")

    monkeypatch.setattr("mindroom.knowledge.refresh_runner.refresh_knowledge_binding_in_subprocess", _fake_refresh)
    monkeypatch.setattr(Config, "model_copy", _unexpected_copy)

    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await asyncio.wait_for(refreshed.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    await scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_refresh_scheduler_probes_embedder_after_persisted_refresh_failure(
    tmp_path: Path,
    manual: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh that persisted REFRESH_FAILED triggers one embedder health probe."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    probe_reasons: list[str] = []

    async def _fake_refresh(_key: knowledge_registry.PublishedIndexKey, **_kwargs: object) -> _RefreshChildResult:
        return _RefreshChildResult(
            error="Indexed 0 of 3 managed knowledge files (first error: embedder authentication failed (HTTP 401))",
        )

    async def _fake_check(
        _config: Config,
        _runtime_paths: object,
        *,
        reason: str,
        health_recorder: object | None = None,
    ) -> None:
        assert health_recorder is not None
        probe_reasons.append(reason)

    monkeypatch.setattr(knowledge_refresh_runner, "_run_refresh_child", _fake_refresh)
    monkeypatch.setattr("mindroom.knowledge.refresh_runner.check_embedder_health", _fake_check)

    await scheduler.refresh_now(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
    ) if manual else scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await _wait_until(lambda: bool(probe_reasons), "the failed refresh never probed the embedder")
    await scheduler.shutdown()

    assert probe_reasons == ["knowledge_refresh_failed"]


@pytest.mark.asyncio
async def test_refresh_scheduler_skips_probe_for_non_embedder_subprocess_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subprocess crash without causal evidence does not bill an embedding probe."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    probe_reasons: list[str] = []

    async def _fake_refresh(_key: knowledge_registry.PublishedIndexKey, **_kwargs: object) -> NoReturn:
        msg = "subprocess exited 1"
        raise _RefreshChildFailedError(msg, started=True)

    async def _fake_check(
        _config: Config,
        _runtime_paths: object,
        *,
        reason: str,
        health_recorder: object | None = None,
    ) -> None:
        del health_recorder
        probe_reasons.append(reason)

    monkeypatch.setattr(knowledge_refresh_runner, "_run_refresh_child", _fake_refresh)
    monkeypatch.setattr("mindroom.knowledge.refresh_runner.check_embedder_health", _fake_check)

    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await _wait_until(lambda: not scheduler._tasks, "the refresh task did not finish")
    await scheduler.shutdown()

    assert probe_reasons == []


@pytest.mark.asyncio
async def test_refresh_scheduler_does_not_probe_after_successful_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh with no persisted failure never triggers a probe."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    refreshed = asyncio.Event()

    async def _fake_refresh(_key: knowledge_registry.PublishedIndexKey, **_kwargs: object) -> _RefreshChildResult:
        refreshed.set()
        return _RefreshChildResult(published=True)

    async def _fake_check(
        _config: Config,
        _runtime_paths: object,
        *,
        reason: str,
        health_recorder: object | None = None,
    ) -> None:
        del health_recorder
        msg = f"unexpected embedder probe: {reason}"
        raise AssertionError(msg)

    monkeypatch.setattr(knowledge_refresh_runner, "_run_refresh_child", _fake_refresh)
    monkeypatch.setattr("mindroom.knowledge.refresh_runner.check_embedder_health", _fake_check)

    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await asyncio.wait_for(refreshed.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS)
    await scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_successful_subprocess_refresh_probes_to_clear_stale_health(
    tmp_path: Path,
    manual: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful child refresh repairs stale main-process health."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    probe_reasons: list[str] = []
    embedder_health.capture_embedder_health_recorder().record("embedder authentication failed (HTTP 401)")

    async def _fake_refresh(_key: knowledge_registry.PublishedIndexKey, **_kwargs: object) -> _RefreshChildResult:
        return _RefreshChildResult(published=True)

    async def _fake_check(
        _config: Config,
        _runtime_paths: object,
        *,
        reason: str,
        health_recorder: object | None = None,
    ) -> None:
        assert health_recorder is not None
        probe_reasons.append(reason)

    monkeypatch.setattr(knowledge_refresh_runner, "_run_refresh_child", _fake_refresh)
    monkeypatch.setattr("mindroom.knowledge.refresh_runner.check_embedder_health", _fake_check)

    try:
        await scheduler.refresh_now(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
        ) if manual else scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
        await _wait_until(lambda: bool(probe_reasons), "the recovered refresh never probed the embedder")
        await scheduler.shutdown()
    finally:
        embedder_health.capture_embedder_health_recorder().record(None)

    assert probe_reasons == ["knowledge_refresh_recovery"]


@pytest.mark.asyncio
async def test_refresh_scheduled_before_reload_cannot_probe_old_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh carries the generation captured when it was queued."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    started = asyncio.Event()
    release = asyncio.Event()
    probe_reasons: list[str] = []

    async def _fake_refresh(_key: knowledge_registry.PublishedIndexKey, **_kwargs: object) -> _RefreshChildResult:
        started.set()
        await release.wait()
        return _RefreshChildResult(published=True)

    async def _fake_check(
        _config: Config,
        _runtime_paths: object,
        *,
        reason: str,
        health_recorder: object | None = None,
    ) -> None:
        del health_recorder
        probe_reasons.append(reason)

    monkeypatch.setattr(knowledge_refresh_runner, "_run_refresh_child", _fake_refresh)
    monkeypatch.setattr("mindroom.knowledge.refresh_runner.check_embedder_health", _fake_check)

    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await started.wait()
    embedder_health._reset_embedder_health_generation()
    embedder_health.capture_embedder_health_recorder().record("embedder authentication failed (HTTP 401)")
    release.set()
    await _wait_until(lambda: not scheduler._tasks, "the refresh task did not finish")
    await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS)
    await scheduler.shutdown()
    embedder_health.capture_embedder_health_recorder().record(None)

    assert probe_reasons == []


@pytest.mark.parametrize(
    ("process_env", "env_file", "expected"),
    [
        ({}, "MINDROOM_KNOWLEDGE_REFRESH_CONCURRENCY=3\n", 3),
        ({"MINDROOM_KNOWLEDGE_REFRESH_CONCURRENCY": "2"}, "MINDROOM_KNOWLEDGE_REFRESH_CONCURRENCY=3\n", 2),
        ({}, "", 1),
    ],
    ids=["env_file", "process_env_wins", "live_environ_ignored"],
)
def test_orchestrator_refresh_scheduler_reads_concurrency_from_the_runtime_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_env: dict[str, str],
    env_file: str,
    expected: int,
) -> None:
    """The background refresh limit can be tuned in the runtime env or the `.env` beside `config.yaml`."""
    monkeypatch.setenv("MINDROOM_KNOWLEDGE_REFRESH_CONCURRENCY", "5")
    (tmp_path / ".env").write_text(env_file, encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env=process_env)

    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)

    assert orchestrator.knowledge_refresh_scheduler.max_concurrent_refreshes == expected


def test_the_process_has_one_refresh_scheduler_with_the_runtime_limit(tmp_path: Path) -> None:
    """Bots, the API, and semantic file memory share one scheduler, built with the limit of the first runtime."""
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_REFRESH_CONCURRENCY=3\n", encoding="utf-8")

    scheduler = runtime_refresh_scheduler(test_runtime_paths(tmp_path))

    assert scheduler.max_concurrent_refreshes == 3
    assert runtime_refresh_scheduler(test_runtime_paths(tmp_path)) is scheduler
    assert _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path)).knowledge_refresh_scheduler is scheduler


@pytest.mark.parametrize(
    ("raw_value", "match"),
    [
        ("not-an-int", "must be an integer"),
        ("0", "must be at least 1"),
        ("-2", "must be at least 1"),
    ],
)
def test_refresh_scheduler_env_concurrency_fails_fast(raw_value: str, match: str, tmp_path: Path) -> None:
    """Malformed refresh concurrency env values fail startup instead of hiding typos."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"MINDROOM_KNOWLEDGE_REFRESH_CONCURRENCY": raw_value},
    )

    with pytest.raises(ValueError, match=match):
        _MultiAgentOrchestrator(runtime_paths=runtime_paths)


@pytest.mark.asyncio
async def test_refresh_scheduler_coalesces_duplicate_schedule_while_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort schedules run one follow-up refresh with the latest request."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    older_pending_config = config.model_copy(deep=True)
    older_pending_config.knowledge_bases["docs"].chunk_size = 2048
    latest_pending_config = config.model_copy(deep=True)
    latest_pending_config.knowledge_bases["docs"].chunk_size = 4096
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    seen_chunk_sizes: list[int] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def _fake_refresh(_key: knowledge_registry.PublishedIndexKey, **kwargs: object) -> _RefreshChildResult:
        refresh_config = kwargs["config"]
        assert isinstance(refresh_config, Config)
        seen_chunk_sizes.append(refresh_config.knowledge_bases["docs"].chunk_size)
        if len(seen_chunk_sizes) == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return _RefreshChildResult()

    monkeypatch.setattr(knowledge_refresh_runner, "_run_refresh_child", _fake_refresh)

    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await first_started.wait()
    scheduler.schedule_refresh("docs", config=older_pending_config, runtime_paths=runtime_paths)
    scheduler.schedule_refresh("docs", config=latest_pending_config, runtime_paths=runtime_paths)
    await asyncio.sleep(0)

    assert seen_chunk_sizes == [5000]

    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    await _wait_until(lambda: not scheduler._tasks, "coalesced refresh task did not finish")

    assert seen_chunk_sizes == [5000, 4096]


@pytest.mark.asyncio
async def test_refresh_scheduler_refresh_now_runs_the_shared_refresh_with_force_reindex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit refresh runs through the scheduler, forced, and returns the refresh's result."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    seen_force_reindex: list[bool] = []

    async def _fake_refresh(base_id: str, **kwargs: object) -> object:
        assert base_id == "docs"
        refresh_config = kwargs["config"]
        assert isinstance(refresh_config, Config)
        seen_force_reindex.append(bool(kwargs.get("force_reindex", False)))
        return knowledge_refresh_runner.KnowledgeRefreshResult(
            indexed_count=1,
            index_published=True,
            availability=KnowledgeAvailability.READY,
        )

    monkeypatch.setattr("mindroom.knowledge.refresh_runner.refresh_knowledge_binding_in_subprocess", _fake_refresh)

    result = await scheduler.refresh_now("docs", config=config, runtime_paths=runtime_paths, force_reindex=True)

    assert result.indexed_count == 1
    assert seen_force_reindex == [True]


def _write_subprocess_result(kwargs: dict[str, object]) -> None:
    """Model the small worker result alongside the existing process lifecycle fakes."""
    output = kwargs["stdout"]
    assert isinstance(output, BufferedIOBase)
    output.write(
        b'{"indexed_count":0,"published":true,"error":null,"deferred":false,"source_changed":null}',
    )
    output.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_runtime_path", [False, True], ids=["launcher-fallback", "runtime-override"])
@pytest.mark.usefixtures("real_refresh_child")
async def test_scheduled_refresh_subprocess_receives_config_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_runtime_path: bool,
) -> None:
    """The subprocess gets its config snapshot and the correct executable path."""
    launcher_bin = tmp_path / "nix-profile" / "bin"
    runtime_bin = tmp_path / "runtime-bin"
    monkeypatch.setenv("PATH", str(launcher_bin))
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    config.knowledge_bases["docs"].chunk_size = 1234
    runtime_paths = runtime_paths_for(config)
    runtime_overrides = {"PATH": str(runtime_bin), "TMPDIR": str(tmp_path / "runtime-tmp")}
    path_overrides = ({}, runtime_overrides)[explicit_runtime_path]
    runtime_paths = replace(
        runtime_paths,
        process_env={**runtime_paths.process_env, **path_overrides},
    )
    captured_request: dict[str, object] = {}
    captured_args: tuple[object, ...] = ()
    captured_env: dict[str, str] = {}
    captured_stdin: _Stdin | None = None

    class _Stdin:
        def __init__(self) -> None:
            self.payload = bytearray()

        def write(self, payload: bytes) -> None:
            self.payload.extend(payload)

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    class _Process:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdin = _Stdin()

        async def wait(self) -> int:
            return 0

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> _Process:
        nonlocal captured_args, captured_env, captured_stdin
        _write_subprocess_result(kwargs)
        captured_args = args
        raw_env = kwargs["env"]
        assert isinstance(raw_env, dict)
        captured_env = raw_env
        assert kwargs["stdin"] is asyncio.subprocess.PIPE
        process = _Process()
        captured_stdin = process.stdin
        return process

    async def _fake_terminate(_process: _Process) -> None:
        pass

    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(knowledge_refresh_runner, "_subprocess_session_kwargs", dict)
    monkeypatch.setattr(knowledge_refresh_runner, "_terminate_refresh_subprocess", _fake_terminate)

    await knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=_identity("@alice:localhost"),
    )

    assert captured_args[:3] == (sys.executable, "-m", "mindroom.knowledge.refresh_entrypoint")
    assert "--request-path" not in captured_args
    assert captured_env["MINDROOM_KNOWLEDGE_REFRESH_SUBPROCESS"] == "1"
    assert captured_env["TMPDIR"] == tempfile.gettempdir()
    assert captured_env.items() >= subprocess_logging_env().items()
    assert captured_env["PATH"] == str((launcher_bin, runtime_bin)[explicit_runtime_path])
    assert captured_stdin is not None
    captured_request.update(json.loads(bytes(captured_stdin.payload).decode()))
    assert captured_request["base_id"] == "docs"
    assert captured_request["config_path"] == str(runtime_paths.config_path)
    assert captured_request["storage_root"] == str(runtime_paths.storage_root)
    assert "runtime_paths" not in captured_request
    assert captured_request["config_data"]["knowledge_bases"]["docs"]["chunk_size"] == 1234
    assert captured_request["runtime_knowledge_base"] is None
    assert captured_request["execution_identity"]["requester_id"] == "@alice:localhost"


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_subprocess_refresh_serializes_request_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large config serialization must not block the control-plane event loop."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    event_loop_thread = get_ident()
    serialization_threads: list[int] = []
    process = MagicMock(returncode=0)
    process.wait = AsyncMock(return_value=0)
    process.stdin.drain = AsyncMock()
    process.stdin.wait_closed = AsyncMock()

    def _fake_serialize(*_args: object, **_kwargs: object) -> bytes:
        serialization_threads.append(get_ident())
        return b"{}"

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> MagicMock:
        _write_subprocess_result(_kwargs)
        return process

    async def _fake_terminate(_process: object) -> None:
        pass

    monkeypatch.setattr(knowledge_refresh_runner, "_serialize_subprocess_refresh_request", _fake_serialize)
    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(knowledge_refresh_runner, "_subprocess_session_kwargs", dict)
    monkeypatch.setattr(knowledge_refresh_runner, "_terminate_refresh_subprocess", _fake_terminate)

    await knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
    )

    assert serialization_threads
    assert serialization_threads[0] != event_loop_thread


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_scheduled_refreshes_reuse_parent_github_app_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consecutive refresh workers must receive one runtime-cached App token."""
    docs_path = tmp_path / "docs"
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={
            "docs": KnowledgeGitConfig(
                repo_url="https://github.com/example/private.git",
                credentials_service="github_app",
            ),
        },
    )
    runtime_paths = runtime_paths_for(config)
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials(
        "github_app",
        {
            "auth_type": "github_app",
            "app_id": 12345,
            "installation_id": 67890,
            "private_key_file": str(tmp_path / "github-app.pem"),
        },
    )
    mint_count = 0
    payloads: list[dict[str, object]] = []

    async def _fake_mint(
        _self: GitHubAppTokenProvider,
        _credentials: object,
        *,
        repository: str,
        now: datetime,
    ) -> object:
        nonlocal mint_count
        assert repository == "private"
        assert now.tzinfo is not None
        mint_count += 1
        return SimpleNamespace(
            token="parent-cached-token",  # noqa: S106
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )

    process = MagicMock(returncode=0)
    process.wait = AsyncMock(return_value=0)

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> MagicMock:
        _write_subprocess_result(_kwargs)
        return process

    async def _fake_send(_process: object, payload: bytes) -> None:
        payloads.append(json.loads(payload))

    async def _fake_terminate(_process: object) -> None:
        pass

    monkeypatch.setattr(GitHubAppTokenProvider, "_mint_token", _fake_mint)
    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(knowledge_refresh_runner, "_subprocess_session_kwargs", dict)
    monkeypatch.setattr(knowledge_refresh_runner, "_send_subprocess_refresh_request", _fake_send)
    monkeypatch.setattr(knowledge_refresh_runner, "_terminate_refresh_subprocess", _fake_terminate)

    await knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
    )
    await knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
    )

    assert mint_count == 1
    assert [payload["github_app_token"]["token"] for payload in payloads] == [
        "parent-cached-token",
        "parent-cached-token",
    ]


@pytest.mark.asyncio
async def test_subprocess_refresh_primes_parent_github_app_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh child must use its parent-provided token without reminting."""
    docs_path = tmp_path / "docs"
    git_config = KnowledgeGitConfig(
        repo_url="https://github.com/example/private.git",
        credentials_service="github_app",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    credentials = {
        "auth_type": "github_app",
        "app_id": 12345,
        "installation_id": 67890,
        "private_key_file": str(tmp_path / "github-app.pem"),
    }
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials("github_app", credentials)
    raw_payload = json.loads(
        knowledge_refresh_runner._serialize_subprocess_refresh_request(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=None,
            force_reindex=False,
        ),
    )
    raw_payload["github_app_token"] = {
        "token": "parent-cached-token",
        "expires_at_epoch": 4_070_908_800,
        "binding": {
            "app_id": 12345,
            "installation_id": 67890,
            "owner": "example",
            "repository": "private",
            "private_key_file": str(tmp_path / "github-app.pem"),
        },
    }
    mint_count = 0

    async def _fake_mint(
        _self: GitHubAppTokenProvider,
        _credentials: object,
        *,
        repository: str,
        now: datetime,
    ) -> object:
        nonlocal mint_count
        assert repository == "private"
        assert now.tzinfo is not None
        mint_count += 1
        return SimpleNamespace(
            token="unexpected-remint",  # noqa: S106
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )

    async def _fake_refresh(
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        **_kwargs: object,
    ) -> knowledge_refresh_runner.KnowledgeRefreshResult:
        manager = KnowledgeManager(base_id, config=config, runtime_paths=runtime_paths)
        resolved = await manager.git_source._github_app_token_provider.resolve(
            git_config.repo_url,
            credentials,
        )
        assert resolved == ("x-access-token", "parent-cached-token")
        return knowledge_refresh_runner.KnowledgeRefreshResult(
            indexed_count=0,
            index_published=False,
            availability=KnowledgeAvailability.READY,
        )

    monkeypatch.setattr(GitHubAppTokenProvider, "_mint_token", _fake_mint)
    monkeypatch.setattr(knowledge_refresh_runner, "_refresh_knowledge_binding", _fake_refresh)

    result = await knowledge_refresh_runner._run_subprocess_refresh_request(json.dumps(raw_payload).encode())

    assert result.availability is KnowledgeAvailability.READY
    assert mint_count == 0


@pytest.mark.asyncio
async def test_subprocess_refresh_rejects_parent_token_after_credentials_rotate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child must not bind an App token to credentials changed after minting."""
    docs_path = tmp_path / "docs"
    git_config = KnowledgeGitConfig(
        repo_url="https://github.com/example/private.git",
        credentials_service="github_app",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    parent_credentials = {
        "auth_type": "github_app",
        "app_id": 12345,
        "installation_id": 67890,
        "private_key_file": str(tmp_path / "parent.pem"),
    }
    current_credentials = {
        "auth_type": "github_app",
        "app_id": 54321,
        "installation_id": 9876,
        "private_key_file": str(tmp_path / "current.pem"),
    }
    credentials_manager = get_runtime_shared_credentials_manager(runtime_paths)
    credentials_manager.save_credentials("github_app", parent_credentials)
    raw_payload = json.loads(
        knowledge_refresh_runner._serialize_subprocess_refresh_request(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=None,
            force_reindex=False,
        ),
    )
    raw_payload["github_app_token"] = {
        "token": "parent-cached-token",
        "expires_at_epoch": 4_070_908_800,
        "binding": {
            "app_id": 12345,
            "installation_id": 67890,
            "owner": "example",
            "repository": "private",
            "private_key_file": str(tmp_path / "parent.pem"),
        },
    }
    credentials_manager.save_credentials("github_app", current_credentials)
    mint_count = 0

    async def _fake_mint(
        _self: GitHubAppTokenProvider,
        _credentials: object,
        *,
        repository: str,
        now: datetime,
    ) -> object:
        nonlocal mint_count
        assert repository == "private"
        assert now.tzinfo is not None
        mint_count += 1
        return SimpleNamespace(
            token="current-token",  # noqa: S106
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )

    async def _fake_refresh(
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        **_kwargs: object,
    ) -> knowledge_refresh_runner.KnowledgeRefreshResult:
        manager = KnowledgeManager(base_id, config=config, runtime_paths=runtime_paths)
        resolved = await manager.git_source._github_app_token_provider.resolve(
            git_config.repo_url,
            current_credentials,
        )
        assert resolved == ("x-access-token", "current-token")
        return knowledge_refresh_runner.KnowledgeRefreshResult(
            indexed_count=0,
            index_published=False,
            availability=KnowledgeAvailability.READY,
        )

    monkeypatch.setattr(GitHubAppTokenProvider, "_mint_token", _fake_mint)
    monkeypatch.setattr(knowledge_refresh_runner, "_refresh_knowledge_binding", _fake_refresh)

    result = await knowledge_refresh_runner._run_subprocess_refresh_request(json.dumps(raw_payload).encode())

    assert result.availability is KnowledgeAvailability.READY
    assert mint_count == 1


@pytest.mark.asyncio
async def test_subprocess_applies_runtime_knowledge_base_after_authored_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synthetic workspace index may coexist with an authored nested knowledge root."""
    workspace = tmp_path / "workspace"
    thread_exports = workspace / "thread_exports"
    thread_exports.mkdir(parents=True)
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {},
            "knowledge_bases": {"threads": {"path": str(thread_exports)}},
        },
        runtime_paths,
    )
    base_id = "file_memory_agent_openclaw_test"
    runtime_base = KnowledgeBaseConfig(
        mode="semantic",
        path=str(workspace),
        include_patterns=["memory/**/*.md"],
    )
    effective_config = config.with_runtime_knowledge_base_overlay(base_id, runtime_base)
    payload = knowledge_refresh_runner._serialize_subprocess_refresh_request(
        base_id,
        config=effective_config,
        runtime_paths=runtime_paths,
        execution_identity=None,
        force_reindex=False,
    )
    raw_payload = json.loads(payload)
    assert set(raw_payload["config_data"]["knowledge_bases"]) == {"threads"}
    assert raw_payload["runtime_knowledge_base"]["path"] == str(workspace)

    async def _fake_refresh(refresh_base_id: str, *, config: Config, **_kwargs: object) -> _RefreshChildResult:
        assert refresh_base_id == base_id
        assert set(config.knowledge_bases) == {"threads", base_id}
        assert config.knowledge_bases[base_id] == runtime_base
        return _RefreshChildResult(published=True)

    monkeypatch.setattr(knowledge_refresh_runner, "_refresh_knowledge_binding", _fake_refresh)

    result = await knowledge_refresh_runner._run_subprocess_refresh_request(payload)

    assert result == _RefreshChildResult(published=True)


@pytest.mark.asyncio
async def test_subprocess_refresh_tolerates_broken_unused_plugin(tmp_path: Path) -> None:
    """Child refresh config validation should match startup's broken-plugin tolerance."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("plugin-tolerant refresh", encoding="utf-8")
    plugin_root = tmp_path / "plugins" / "broken"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "broken_plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text("import definitely_missing_refresh_plugin_dependency\n", encoding="utf-8")
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "agents": {"helper": {"display_name": "Helper", "knowledge_bases": ["docs"]}},
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "plugins": ["./plugins/broken"],
            "knowledge_bases": {"docs": {"path": str(docs_path)}},
        },
        runtime_paths,
        tolerate_plugin_load_errors=True,
    )
    payload = knowledge_refresh_runner._serialize_subprocess_refresh_request(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=None,
        force_reindex=False,
    )

    result = await knowledge_refresh_runner._run_subprocess_refresh_request(payload)

    assert result.published is True
    assert result.error is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_a_cancelled_refresh_subprocess_is_terminated_and_records_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent cancellation terminates the child and, since it published nothing, leaves the refresh status alone."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("refresh me", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    mark_published_index_stale(key, reason="test_stale")
    status_before = load_refresh_status(published_index_storage_path(key))
    wait_entered = asyncio.Event()
    release_wait = asyncio.Event()
    terminated = asyncio.Event()

    class _Stdin:
        def write(self, _payload: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    class _Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = _Stdin()

        async def wait(self) -> int:
            wait_entered.set()
            await release_wait.wait()
            return self.returncode or 0

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _Process:
        return _Process()

    async def _fake_terminate(process: _Process) -> None:
        process.returncode = -15
        release_wait.set()
        terminated.set()

    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(knowledge_refresh_runner, "_terminate_refresh_subprocess", _fake_terminate)

    refresh_task = asyncio.create_task(
        knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
        ),
    )
    await wait_entered.wait()

    refresh_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task

    assert terminated.is_set()
    assert load_refresh_status(published_index_storage_path(key)) == status_before
    assert status_before.reason == "test_stale"
    assert _refresh_state_for_key(key) == "stale"


@pytest.mark.skipif(os.name == "nt", reason="process groups require POSIX")
@pytest.mark.asyncio
async def test_process_group_exit_wait_warns_when_group_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded drain must explain any process group it cannot retire."""
    monkeypatch.setattr(knowledge_refresh_runner, "_process_group_exists", lambda _process_group_id: True)

    with capture_logs() as logs:
        await knowledge_refresh_runner._wait_for_process_group_exit(12345, wait_seconds=0)

    assert any(log.get("event") == "Knowledge refresh process group survived termination wait" for log in logs)


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_successful_refresh_subprocess_drains_its_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful worker must not leave a lock-holding Git helper behind."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("refresh me", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    terminated = asyncio.Event()

    class _Stdin:
        def write(self, _payload: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    class _Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = _Stdin()

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _Process:
        _write_subprocess_result(_kwargs)
        return _Process()

    async def _fake_terminate(_process: _Process) -> None:
        terminated.set()

    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(knowledge_refresh_runner, "_terminate_refresh_subprocess", _fake_terminate)

    await knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
    )

    assert terminated.is_set()


@pytest.mark.skipif(os.name == "nt", reason="process groups require POSIX")
@pytest.mark.asyncio
async def test_terminate_refresh_subprocess_kills_surviving_process_group_member(tmp_path: Path) -> None:
    """A Git-like grandchild that ignores SIGTERM must not outlive its refresh leader."""
    descendant_lock_path = tmp_path / "descendant.lock"
    ready_read_fd, ready_write_fd = os.pipe()
    script = f"""
import fcntl
import os
import signal
import time

child_pid = os.fork()
if child_pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    lock_file = open({str(descendant_lock_path)!r}, "w")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    os.write({ready_write_fd}, b"1")
    os.close({ready_write_fd})
    time.sleep(60)
    os._exit(0)

os.close({ready_write_fd})
signal.signal(signal.SIGTERM, lambda *_args: os._exit(0))
time.sleep(60)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        start_new_session=True,
        pass_fds=(ready_write_fd,),
    )
    os.close(ready_write_fd)
    try:
        assert (
            await asyncio.wait_for(asyncio.to_thread(os.read, ready_read_fd, 1), timeout=SETTLE_TIMEOUT_SECONDS) == b"1"
        )
        assert file_locks.file_lock_is_held(descendant_lock_path) is True

        await knowledge_refresh_runner._terminate_refresh_subprocess(process)

        assert file_locks.file_lock_is_held(descendant_lock_path) is False
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        os.close(ready_read_fd)


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
@pytest.mark.parametrize("pending_reason", [None, "source_changed", "git_source_updated", "manual_reindex"])
async def test_wedged_subprocess_refresh_is_terminated_after_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pending_reason: str | None,
) -> None:
    """A wedged child is killed and recorded as failed; a source change marked while it ran stays marked."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("refresh me", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    mark_published_index_stale(key, reason="test_stale")
    terminated = asyncio.Event()

    class _Stdin:
        def write(self, _payload: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    class _Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = _Stdin()

        async def wait(self) -> int:
            if pending_reason is not None:
                mark_published_index_stale(key, reason=pending_reason)
            await asyncio.Event().wait()
            raise AssertionError

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _Process:
        _write_subprocess_result(_kwargs)
        return _Process()

    async def _fake_terminate(process: _Process) -> None:
        process.returncode = -9
        terminated.set()

    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(knowledge_refresh_runner, "_terminate_refresh_subprocess", _fake_terminate)
    monkeypatch.setattr(knowledge_refresh_runner, "_refresh_subprocess_timeout_seconds", lambda _runtime_paths: 0.05)

    with pytest.raises(RuntimeError, match=r"timed out after 0\.05s"):
        await knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
        )

    assert terminated.is_set()
    status = load_refresh_status(published_index_storage_path(key))
    assert status.reason == (pending_reason or "test_stale")
    assert status.last_error is not None
    assert "timed out after 0.05s" in status.last_error
    assert status.consecutive_refresh_failures == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_blocked_refresh_request_is_terminated_after_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog includes request delivery when a child never reads stdin."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("refresh me", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    mark_published_index_stale(key, reason="test_stale")
    terminated = asyncio.Event()

    class _Stdin:
        def write(self, _payload: bytes) -> None:
            pass

        async def drain(self) -> None:
            await asyncio.Event().wait()

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    class _Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = _Stdin()

        async def wait(self) -> int:
            msg = "process.wait() must not start before request delivery finishes"
            raise AssertionError(msg)

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _Process:
        _write_subprocess_result(_kwargs)
        return _Process()

    async def _fake_terminate(process: _Process) -> None:
        process.returncode = -9
        terminated.set()

    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(knowledge_refresh_runner, "_terminate_refresh_subprocess", _fake_terminate)
    monkeypatch.setattr(
        knowledge_refresh_runner,
        "_refresh_subprocess_timeout_seconds",
        lambda _runtime_paths: 0.05,
    )

    with pytest.raises(RuntimeError, match=r"timed out after 0\.05s"):
        await asyncio.wait_for(
            knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
                "docs",
                config=config,
                runtime_paths=runtime_paths,
            ),
            timeout=SETTLE_TIMEOUT_SECONDS,
        )

    assert terminated.is_set()
    status = load_refresh_status(published_index_storage_path(key))
    assert status.reason == "test_stale"
    assert status.last_error is not None
    assert "timed out after 0.05s" in status.last_error


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_timeout_cleanup_finishes_before_cancellation_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during timeout cleanup cannot abandon child termination; the cancelled refresh records nothing."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("refresh me", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    mark_published_index_stale(key, reason="test_stale")
    status_before = load_refresh_status(published_index_storage_path(key))
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_finished = asyncio.Event()
    cleanup_cancelled = asyncio.Event()

    class _Stdin:
        def write(self, _payload: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    class _Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = _Stdin()

        async def wait(self) -> int:
            await asyncio.Event().wait()
            raise AssertionError

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _Process:
        _write_subprocess_result(_kwargs)
        return _Process()

    async def _fake_terminate(process: _Process) -> None:
        cleanup_started.set()
        try:
            await cleanup_release.wait()
        except asyncio.CancelledError:
            cleanup_cancelled.set()
            raise
        process.returncode = -9
        cleanup_finished.set()

    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(knowledge_refresh_runner, "_terminate_refresh_subprocess", _fake_terminate)
    monkeypatch.setattr(
        knowledge_refresh_runner,
        "_refresh_subprocess_timeout_seconds",
        lambda _runtime_paths: 0.05,
    )

    refresh_task = asyncio.create_task(
        knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
        ),
    )
    await cleanup_started.wait()
    refresh_task.cancel()
    await asyncio.sleep(0)
    try:
        assert not refresh_task.done()
        refresh_task.cancel()
        await asyncio.sleep(0)
        assert not refresh_task.done()
    finally:
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await refresh_task

    assert cleanup_finished.is_set()
    assert not cleanup_cancelled.is_set()
    assert load_refresh_status(published_index_storage_path(key)) == status_before


def test_refresh_subprocess_timeout_reads_the_runtime_environment(tmp_path: Path) -> None:
    """The refresh watchdog resolves config-adjacent environment and rejects unsafe values."""
    runtime_paths = test_runtime_paths(tmp_path)
    assert (
        knowledge_refresh_runner._refresh_subprocess_timeout_seconds(runtime_paths)
        == DEFAULT_KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_SECONDS
    )

    runtime_paths.env_path.write_text(f"{KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_ENV}=12.5\n", encoding="utf-8")
    configured_runtime_paths = test_runtime_paths(tmp_path)
    assert knowledge_refresh_runner._refresh_subprocess_timeout_seconds(configured_runtime_paths) == 12.5

    for raw_value in ("0", "nan", "inf", "not-a-number"):
        runtime_paths.env_path.write_text(
            f"{KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_ENV}={raw_value}\n",
            encoding="utf-8",
        )
        invalid_runtime_paths = test_runtime_paths(tmp_path)
        with pytest.raises(ValueError, match=KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_ENV):
            knowledge_refresh_runner._refresh_subprocess_timeout_seconds(invalid_runtime_paths)


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_invalid_refresh_timeout_is_recorded_as_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed watchdog setting rejects the refresh before a child starts and records a durable failure."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("refresh me", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    base_runtime_paths = runtime_paths_for(config)
    runtime_paths = replace(
        base_runtime_paths,
        env_file_values={KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_ENV: "invalid"},
    )
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    mark_published_index_stale(key, reason="test_stale")
    spawned = MagicMock(side_effect=AssertionError("no child may start"))
    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", spawned)

    with pytest.raises(_RefreshChildFailedError, match=KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_ENV):
        await knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
        )

    status = load_refresh_status(published_index_storage_path(key))
    assert status.reason == "test_stale"
    assert status.last_error is not None
    assert KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_ENV in status.last_error
    spawned.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_a_crashed_refresh_subprocess_is_recorded_as_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that exits without reporting or publishing is recorded as a failure beside the stale mark."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("refresh me", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    mark_published_index_stale(key, reason="test_stale")

    class _Stdin:
        def write(self, _payload: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    class _Process:
        returncode = 137
        stdin = _Stdin()

        async def wait(self) -> int:
            return self.returncode

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _Process:
        return _Process()

    terminated: list[_Process] = []

    async def _fake_terminate(process: _Process) -> None:
        terminated.append(process)

    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(knowledge_refresh_runner, "_terminate_refresh_subprocess", _fake_terminate)

    with pytest.raises(RuntimeError, match="exit code 137"):
        await knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
        )

    status = load_refresh_status(published_index_storage_path(key))
    assert status.reason == "test_stale"
    assert status.last_error is not None
    assert "exit code 137" in status.last_error
    assert _refresh_state_for_key(key) == "refresh_failed"
    assert len(terminated) == 1


def _publish_as_child(key: knowledge_registry.PublishedIndexKey) -> Publication:
    """Publish a newer collection the way a refresh child does just before it dies."""
    return _publish(
        key,
        collection="newer-success",
        source_signature="newer-source",
        published_revision="newer-revision",
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_a_refresh_subprocess_that_published_before_it_crashed_is_recorded_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that published and then died is judged by its publication: the refresh succeeded."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("refresh me", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    mark_published_index_stale(key, reason="test_stale")
    record_refresh_failed(key, error="older failure")

    class _Stdin:
        def write(self, _payload: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    class _Process:
        returncode = 137
        stdin = _Stdin()

        async def wait(self) -> int:
            _publish_as_child(key)
            return self.returncode

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _Process:
        return _Process()

    async def _fake_terminate(_process: _Process) -> None:
        pass

    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(knowledge_refresh_runner, "_terminate_refresh_subprocess", _fake_terminate)

    with pytest.raises(RuntimeError, match="exit code 137"):
        await knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
        )

    publication = load_publication(published_index_storage_path(key))
    status = load_refresh_status(published_index_storage_path(key))
    assert publication is not None
    assert publication.collection == "newer-success"
    assert (status.reason, status.last_error, status.consecutive_refresh_failures) == (None, None, 0)
    assert _refresh_state_for_key(key) == "none"


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_a_source_change_marked_while_a_crashed_child_published_stays_marked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success a dead child's publication earns clears only the stale marks made before it started."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("refresh me", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    mark_published_index_stale(key, reason="test_stale")

    class _Stdin:
        def write(self, _payload: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    class _Process:
        returncode = 137
        stdin = _Stdin()

        async def wait(self) -> int:
            _publish_as_child(key)
            mark_published_index_stale(key, reason="source_mutated")
            return self.returncode

    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _Process:
        return _Process()

    async def _fake_terminate(_process: _Process) -> None:
        pass

    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(knowledge_refresh_runner, "_terminate_refresh_subprocess", _fake_terminate)

    with pytest.raises(RuntimeError, match="exit code 137"):
        await knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
        )

    publication = load_publication(published_index_storage_path(key))
    status = load_refresh_status(published_index_storage_path(key))
    assert publication is not None
    assert publication.collection == "newer-success"
    assert (status.reason, status.last_error) == ("source_mutated", None)
    assert _refresh_state_for_key(key) == "stale"


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_refresh_subprocess_receives_conservative_thread_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh child processes should not inherit unbounded math/tokenizer thread settings."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    captured_env: dict[str, str] = {}

    class _Stdin:
        def write(self, _payload: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    class _Process:
        returncode = 0
        stdin = _Stdin()

        async def wait(self) -> int:
            return self.returncode

    async def _fake_create_subprocess_exec(*_args: object, **kwargs: object) -> _Process:
        _write_subprocess_result(kwargs)
        captured_env.update(kwargs["env"])
        return _Process()

    async def _fake_terminate(_process: _Process) -> None:
        pass

    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(knowledge_refresh_runner, "_terminate_refresh_subprocess", _fake_terminate)

    await knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
    )

    assert captured_env["OMP_NUM_THREADS"] == "1"
    assert captured_env["OPENBLAS_NUM_THREADS"] == "1"
    assert captured_env["MKL_NUM_THREADS"] == "1"
    assert captured_env["NUMEXPR_NUM_THREADS"] == "1"
    assert captured_env["VECLIB_MAXIMUM_THREADS"] == "1"
    assert captured_env["TOKENIZERS_PARALLELISM"] == "false"


@pytest.mark.asyncio
async def test_refresh_scheduler_shutdown_suppresses_completed_refresh_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown drains fire-and-forget refresh task failures instead of re-raising them."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()

    async def _fake_refresh(base_id: str, **_kwargs: object) -> object:
        _ = base_id
        msg = "refresh failed"
        raise RuntimeError(msg)

    monkeypatch.setattr("mindroom.knowledge.refresh_runner.refresh_knowledge_binding_in_subprocess", _fake_refresh)

    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await asyncio.sleep(0)
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_refresh_scheduler_does_not_schedule_after_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Late schedule calls after shutdown do not create orphaned refresh tasks."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    calls = 0

    async def _fake_refresh(base_id: str, **_kwargs: object) -> object:
        _ = base_id
        nonlocal calls
        calls += 1
        return object()

    monkeypatch.setattr("mindroom.knowledge.refresh_runner._refresh_knowledge_binding", _fake_refresh)

    await scheduler.shutdown()
    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await asyncio.sleep(0)

    assert calls == 0
    assert scheduler._tasks == {}


@pytest.mark.asyncio
async def test_refresh_status_is_visible_to_the_api_for_a_refresh_the_bots_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bots and the dashboard share the process's one scheduler, so each sees the other's refreshes."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    matrix_scheduler = _MultiAgentOrchestrator(runtime_paths=runtime_paths).knowledge_refresh_scheduler
    api_scheduler = runtime_refresh_scheduler(runtime_paths)
    target = knowledge_registry.resolve_refresh_target("docs", config=config, runtime_paths=runtime_paths)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocked_refresh(base_id: str, **_kwargs: object) -> object:
        _ = base_id
        started.set()
        await release.wait()
        return object()

    monkeypatch.setattr(
        "mindroom.knowledge.refresh_runner.refresh_knowledge_binding_in_subprocess",
        _blocked_refresh,
    )

    matrix_scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await started.wait()

    try:
        assert api_scheduler is matrix_scheduler
        assert api_scheduler.is_refreshing(target) is True
        assert knowledge_base_refreshing(
            api_scheduler,
            "docs",
            config=config,
            runtime_paths=runtime_paths,
        )
    finally:
        release.set()
        await matrix_scheduler.shutdown()


@pytest.mark.asyncio
async def test_refresh_scheduler_limits_concurrent_subprocess_refreshes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queued refreshes should not start more child refresh workers than the configured global limit."""
    docs_path = tmp_path / "docs"
    api_path = tmp_path / "api"
    manual_path = tmp_path / "manual"
    config = _config(
        tmp_path,
        bases={"docs": docs_path, "api": api_path, "manual": manual_path},
        agent_bases=["docs", "api", "manual"],
    )
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler(max_concurrent_refreshes=1)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    started_base_ids: list[str] = []
    active_refreshes = 0
    max_active_refreshes = 0

    async def _blocked_refresh(base_id: str, **kwargs: object) -> KnowledgeRefreshResult:
        nonlocal active_refreshes, max_active_refreshes
        started_base_ids.append(base_id)
        active_refreshes += 1
        max_active_refreshes = max(max_active_refreshes, active_refreshes)
        try:
            if len(started_base_ids) == 1:
                first_started.set()
                await release_first.wait()
            refresh_config = kwargs["config"]
            assert isinstance(refresh_config, Config)
            return KnowledgeRefreshResult(
                indexed_count=0,
                index_published=True,
                availability=KnowledgeAvailability.READY,
            )
        finally:
            active_refreshes -= 1

    monkeypatch.setattr(
        "mindroom.knowledge.refresh_runner.refresh_knowledge_binding_in_subprocess",
        _blocked_refresh,
    )

    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    scheduler.schedule_refresh("api", config=config, runtime_paths=runtime_paths)
    await first_started.wait()
    manual = asyncio.create_task(
        scheduler.refresh_now("manual", config=config, runtime_paths=runtime_paths, force_reindex=True),
    )
    await asyncio.sleep(0)

    assert started_base_ids == ["docs"]

    release_first.set()
    await asyncio.wait_for(manual, timeout=SETTLE_TIMEOUT_SECONDS)
    await _wait_until(lambda: not scheduler._tasks, "the queued refreshes did not finish")
    await scheduler.shutdown()

    assert started_base_ids == ["docs", "api", "manual"]
    assert max_active_refreshes == 1


def test_index_key_is_per_binding_not_raw_base_id(tmp_path: Path) -> None:
    """The same base id resolves to separate refresh keys when storage binding differs."""
    path = tmp_path / "docs"
    config_a = _config(tmp_path / "a", bases={"docs": path}, agent_bases=["docs"])
    config_b = _config(tmp_path / "b", bases={"docs": path}, agent_bases=["docs"])

    key_a = get_published_index("docs", config=config_a, runtime_paths=runtime_paths_for(config_a)).key
    key_b = get_published_index("docs", config=config_b, runtime_paths=runtime_paths_for(config_b)).key

    assert key_a.base_id == key_b.base_id == "docs"
    assert key_a != key_b


def test_shared_knowledge_path_named_memory_stays_config_relative(tmp_path: Path) -> None:
    """A shared KB named like memory should not bind to agent file-memory workspaces."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "openclaw": AgentConfig(
                    display_name="OpenClaw",
                    memory_backend="file",
                    knowledge_bases=["daily_memory"],
                ),
                "mindroom_spouse": AgentConfig(
                    display_name="MindRoom Spouse",
                    memory_backend="file",
                    knowledge_bases=["daily_memory"],
                ),
            },
            knowledge_bases={"daily_memory": KnowledgeBaseConfig(path="./memory")},
            memory={"backend": "file", "file": {"path": "./memory"}},
        ),
        runtime_paths,
    )

    dashboard_root = knowledge_api._knowledge_root(config, "daily_memory", runtime_paths, create=True)
    shared_key = resolve_published_index_key("daily_memory", config=config, runtime_paths=runtime_paths, create=True)
    openclaw_key = resolve_published_index_key(
        "daily_memory",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=_identity("@alice:localhost", agent_name="openclaw"),
        create=True,
    )
    spouse_key = resolve_published_index_key(
        "daily_memory",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=_identity("@alice:localhost", agent_name="mindroom_spouse"),
        create=True,
    )

    assert dashboard_root == runtime_paths.config_dir / "memory"
    assert Path(shared_key.knowledge_path) == runtime_paths.config_dir / "memory"
    assert Path(openclaw_key.knowledge_path) == runtime_paths.config_dir / "memory"
    assert Path(spouse_key.knowledge_path) == runtime_paths.config_dir / "memory"
    assert {shared_key, openclaw_key, spouse_key} == {shared_key}
    assert not (runtime_paths.storage_root / "agents" / "openclaw" / "workspace" / "memory").exists()
    assert not (runtime_paths.storage_root / "agents" / "mindroom_spouse" / "workspace" / "memory").exists()


def test_shared_relative_knowledge_base_keeps_non_memory_path_config_relative(tmp_path: Path) -> None:
    """Ordinary shared relative knowledge paths should stay config-relative."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config(
        agents={
            "helper": AgentConfig(
                display_name="Helper",
                memory_backend="file",
                knowledge_bases=["docs"],
            ),
        },
        knowledge_bases={"docs": KnowledgeBaseConfig(path="./knowledge_docs")},
        memory={"backend": "file", "file": {"path": "./memory"}},
    )
    config = bind_runtime_paths(config, runtime_paths)

    key = resolve_published_index_key(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=_identity("@alice:localhost"),
        create=True,
    )

    assert Path(key.knowledge_path) == runtime_paths.config_dir / "knowledge_docs"


def test_shared_literal_dollar_path_stays_config_relative(tmp_path: Path) -> None:
    """Literal dollar signs in path names should stay ordinary config-relative knowledge paths."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config(
        agents={
            "helper": AgentConfig(
                display_name="Helper",
                memory_backend="file",
                knowledge_bases=["daily_memory"],
            ),
        },
        knowledge_bases={"daily_memory": KnowledgeBaseConfig(path="./notes$archive")},
        memory={"backend": "file", "file": {"path": "./notes$archive"}},
    )
    config = bind_runtime_paths(config, runtime_paths)

    key = resolve_published_index_key(
        "daily_memory",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=_identity("@alice:localhost"),
        create=True,
    )

    assert Path(key.knowledge_path) == runtime_paths.config_dir / "notes$archive"


def test_file_memory_agent_without_configured_file_path_keeps_shared_base_config_relative(tmp_path: Path) -> None:
    """Agent-level file memory does not make arbitrary shared paths workspace-relative."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config(
        agents={
            "helper": AgentConfig(
                display_name="Helper",
                memory_backend="file",
                knowledge_bases=["daily_memory"],
            ),
        },
        knowledge_bases={"daily_memory": KnowledgeBaseConfig(path="./memory")},
        memory={"backend": "mem0"},
    )
    config = bind_runtime_paths(config, runtime_paths)

    key = resolve_published_index_key(
        "daily_memory",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=_identity("@alice:localhost"),
        create=True,
    )

    assert Path(key.knowledge_path) == runtime_paths.config_dir / "memory"


@pytest.mark.asyncio
async def test_private_agent_knowledge_publishes_isolated_indexes(tmp_path: Path) -> None:
    """Requester-local private knowledge gets separate indexes, kept in primary-only storage outside private roots."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge"),
                    ),
                ),
            },
        ),
        runtime_paths,
    )
    base_id = config.resolve_agent("helper").policy.private_knowledge_base_id
    assert base_id is not None
    identity_a = _identity("@alice:localhost")
    identity_b = _identity("@bob:localhost")
    key_a = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity_a,
        create=True,
    )
    key_b = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity_b,
        create=True,
    )
    Path(key_a.knowledge_path).mkdir(parents=True, exist_ok=True)
    Path(key_b.knowledge_path).mkdir(parents=True, exist_ok=True)
    (Path(key_a.knowledge_path) / "note.md").write_text("alice private note", encoding="utf-8")
    (Path(key_b.knowledge_path) / "note.md").write_text("bob private note", encoding="utf-8")

    await refresh_knowledge_binding(base_id, config=config, runtime_paths=runtime_paths, execution_identity=identity_a)
    await refresh_knowledge_binding(base_id, config=config, runtime_paths=runtime_paths, execution_identity=identity_b)
    knowledge_a = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        execution_identity=identity_a,
    ).knowledge
    knowledge_b = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        execution_identity=identity_b,
    ).knowledge

    assert key_a != key_b
    index_a, index_b = published_index_storage_path(key_a), published_index_storage_path(key_b)
    private_indexes = runtime_paths.storage_root.resolve() / "knowledge_db" / "private"
    # Each requester gets a namespace of its own, so even a colliding base key cannot share a directory.
    assert index_a.parents[1] != index_b.parents[1]
    assert index_a.parents[2] == index_b.parents[2] == private_indexes
    assert knowledge_a is not None
    assert knowledge_b is not None
    assert [document.content for document in await knowledge_a.asearch("private", 5)] == ["alice private note"]
    assert [document.content for document in await knowledge_b.asearch("private", 5)] == ["bob private note"]


@pytest.mark.asyncio
async def test_private_agent_knowledge_schedules_refresh_when_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requester-local READY indexes should be served and refreshed without request-time scans."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge"),
                    ),
                ),
            },
        ),
        runtime_paths,
    )
    base_id = config.resolve_agent("helper").policy.private_knowledge_base_id
    assert base_id is not None
    identity = _identity("@alice:localhost")
    key = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
        create=True,
    )
    knowledge_path = Path(key.knowledge_path)
    knowledge_path.mkdir(parents=True, exist_ok=True)
    note = knowledge_path / "note.md"
    note.write_text("alice private old", encoding="utf-8")

    await refresh_knowledge_binding(base_id, config=config, runtime_paths=runtime_paths, execution_identity=identity)
    note.write_text("alice private new", encoding="utf-8")
    scheduler = _RecordingScheduler(refreshing=False)
    unavailable: dict[str, KnowledgeAvailability] = {}
    unavailable_details: dict[str, KnowledgeAvailabilityDetail] = {}

    _forbid_corpus_scans(monkeypatch, "private READY access should not scan the local corpus")
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        execution_identity=identity,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    unavailable_details.update(_resolution.unavailable)
    knowledge = _resolution.knowledge

    assert knowledge is not None
    assert [document.content for document in await knowledge.asearch("private", 5)] == ["alice private old"]
    assert unavailable == {base_id: KnowledgeAvailability.STALE}
    assert unavailable_details == {
        base_id: KnowledgeAvailabilityDetail(
            availability=KnowledgeAvailability.STALE,
            search_available=True,
        ),
    }
    scheduler.schedule_refresh.assert_called_once()


def test_private_agent_knowledge_refresh_cooldowns_are_bounded(tmp_path: Path) -> None:
    """The scheduler's on-access cooldowns stay bounded however many requesters a private base has."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge"),
                    ),
                ),
            },
        ),
        runtime_paths,
    )
    base_id = config.resolve_agent("helper").policy.private_knowledge_base_id
    assert base_id is not None
    max_entries = knowledge_refresh_scheduler._MAX_SCHEDULED_COOLDOWNS
    scheduler = _RecordingScheduler(refreshing=False)

    for index in range(max_entries + 40):
        identity = _identity(f"@user{index}:localhost")
        resolve_published_index_key(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=identity,
            create=True,
        )
        # Stamp through the production path, so deleting its prune fails here.
        knowledge_utils._schedule_refresh_for_availability(
            scheduler,
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=identity,
            lookup=get_published_index(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=identity,
            ),
            availability=KnowledgeAvailability.STALE,
            wall_now=datetime.now(tz=UTC),
        )

    assert scheduler.schedule_refresh.call_count == max_entries + 40
    assert len(scheduler._scheduled_at) == max_entries


def test_private_index_read_path_cache_insertion_is_bounded(tmp_path: Path) -> None:
    """Loading persisted private indexes through the read path should prune old cache entries."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge"),
                    ),
                ),
            },
        ),
        runtime_paths,
    )
    base_id = config.resolve_agent("helper").policy.private_knowledge_base_id
    assert base_id is not None
    count = knowledge_registry._MAX_PRIVATE_PUBLISHED_INDEXES + 10

    for index in range(count):
        identity = _identity(f"@user{index}:localhost")
        key = resolve_published_index_key(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=identity,
            create=True,
        )
        collection = f"private_read_collection_{index}"
        seed_collection(
            KnowledgeCollection(path=published_index_storage_path(key), name=collection),
            [{"content": f"private read {index}", "metadata": {"source_path": "note.md"}}],
        )
        _publish(key, collection=collection, source_signature=f"sig-{index}")

    knowledge_registry._published_indexes.clear()

    for index in range(count):
        lookup = get_published_index(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=_identity(f"@user{index}:localhost"),
        )
        assert lookup.reader is not None

    private_index_count = sum(
        key.base_id.startswith(PRIVATE_KNOWLEDGE_BASE_ID_PREFIX) for key in knowledge_registry._published_indexes
    )
    assert private_index_count <= knowledge_registry._MAX_PRIVATE_PUBLISHED_INDEXES


def _write_queryable_index_state(
    key: knowledge_registry.PublishedIndexKey,
    *,
    collection: str,
) -> None:
    KnowledgeCollection(path=published_index_storage_path(key), name=collection).create(
        collection_meta(key.indexing_settings.to_metadata(), stage="complete"),
    )
    _publish(key, collection=collection, indexed_count=0, source_signature="credential-rotation-test")


def test_cached_handle_rebuilds_after_dashboard_embedder_credential_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credential save/delete replaces the cached client without rebuilding vectors."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    _write_queryable_index_state(key, collection="credential_rotation")
    manager = get_runtime_shared_credentials_manager(runtime_paths)
    manager.save_credentials("openai", {"api_key": "fallback-key"})
    manager.save_credentials("embedder", {"api_key": "old-key"})
    constructed_keys: list[str] = []

    def capture_embedder(_config: Config, _runtime_paths: RuntimePaths) -> object:
        constructed_keys.append(get_embedder_api_key(_runtime_paths))
        return object()

    monkeypatch.setattr(knowledge_registry, "create_configured_embedder", capture_embedder)

    first = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    first_handle = knowledge_registry._published_indexes[key]
    manager.save_credentials("embedder", {"api_key": "new-key"})
    second = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    second_handle = knowledge_registry._published_indexes[key]
    manager.delete_credentials("embedder")
    third = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert first.reader is not None
    assert second.reader is not None
    assert third.reader is not None
    assert first.reader is not second.reader
    assert second.reader is not third.reader
    assert constructed_keys == ["old-key", "new-key", "fallback-key"]
    assert first_handle.embedder_client_signature is not None
    assert second_handle.embedder_client_signature is not None
    assert first_handle.embedder_client_signature != second_handle.embedder_client_signature
    assert "old-key" not in first_handle.embedder_client_signature
    assert "new-key" not in second_handle.embedder_client_signature


def test_cached_handle_rebuilds_after_explicit_embedder_key_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config hot reload replaces a handle that captured the old explicit key."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    old_config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        memory={"embedder": {"config": {"api_key": "explicit-old"}}},
    )
    new_config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        memory={"embedder": {"config": {"api_key": "explicit-new"}}},
    )
    runtime_paths = runtime_paths_for(old_config)
    key = resolve_published_index_key("docs", config=old_config, runtime_paths=runtime_paths)
    assert key == resolve_published_index_key("docs", config=new_config, runtime_paths=runtime_paths)
    _write_queryable_index_state(key, collection="explicit_key_rotation")
    constructed_keys: list[str] = []

    def capture_embedder(config: Config, _runtime_paths: RuntimePaths) -> object:
        constructed_keys.append(
            get_embedder_api_key(
                _runtime_paths,
                explicit_api_key=config.memory.embedder.config.api_key,
            ),
        )
        return object()

    monkeypatch.setattr(knowledge_registry, "create_configured_embedder", capture_embedder)

    first = get_published_index("docs", config=old_config, runtime_paths=runtime_paths)
    second = get_published_index("docs", config=new_config, runtime_paths=runtime_paths)

    assert first.reader is not None
    assert second.reader is not None
    assert first.reader is not second.reader
    assert constructed_keys == ["explicit-old", "explicit-new"]


@pytest.mark.asyncio
async def test_published_indexed_count_uses_persisted_metadata_without_collection_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routine status counts come from metadata rather than scanning vector rows."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.reader is not None

    def _raise_scan(self: KnowledgeCollection, **_kwargs: object) -> NoReturn:
        _ = self
        msg = "collection scan should not be used"
        raise AssertionError(msg)

    monkeypatch.setattr(KnowledgeCollection, "_connect", _raise_scan)

    assert lookup.publication is not None
    assert lookup.publication.indexed_count == 1
    assert get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths).indexed_count == 1


@pytest.mark.asyncio
async def test_local_noop_refresh_reports_published_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unchanged local refresh republishes a usable index and reports it as published."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("local index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _track_reindex(self: KnowledgeManager, *, force_reindex: bool = False) -> RefreshOutcome:
        nonlocal reindex_count
        reindex_count += 1
        if reindex_count > 1:
            msg = "unchanged local refresh should not reindex"
            raise AssertionError(msg)
        return await original_reindex(self, force_reindex=force_reindex)

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert result.indexed_count == 1
    assert reindex_count == 1


@pytest.mark.asyncio
async def test_local_refresh_reindexes_when_content_changes_with_same_mtime_and_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unchanged fast path must not publish stale vectors after content-only changes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("old index", encoding="utf-8")
    initial_stat = doc.stat()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _track_reindex(self: KnowledgeManager, *, force_reindex: bool = False) -> RefreshOutcome:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self, force_reindex=force_reindex)

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("new index", encoding="utf-8")
    os.utime(doc, ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns))
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert reindex_count == 2
    assert lookup.reader is not None
    assert [document.content for document in await lookup.reader.asearch("index", limit=5)] == [
        "new index",
    ]


@pytest.mark.asyncio
async def test_refresh_does_not_synthesize_missing_published_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publication lost after its refresh leaves the base unpublished instead of synthesizing a READY one."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("metadata index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    original_reindex = KnowledgeManager.reindex_all

    async def _delete_metadata_after_reindex(self: KnowledgeManager, *, force_reindex: bool = False) -> RefreshOutcome:
        outcome = await original_reindex(self, force_reindex=force_reindex)
        publication_path(self._base_storage_path).unlink()
        return outcome

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _delete_metadata_after_reindex)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.availability is KnowledgeAvailability.INITIALIZING
    assert _publication(config, runtime_paths) is None
    assert not publication_path(_storage(config, runtime_paths)).exists()
    assert lookup.reader is None
    assert lookup.availability is KnowledgeAvailability.INITIALIZING


def test_published_metadata_write_uses_unique_temp_and_cleans_failed_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publication writes should not share one deterministic temp file."""
    attempted_temp_paths: list[Path] = []
    original_replace = Path.replace

    def _fail_temp_replace(self: Path, target: Path) -> Path:
        if self.parent == tmp_path and self.name.startswith(".publication.json.") and self.name.endswith(".tmp"):
            attempted_temp_paths.append(self)
            msg = "replace failed"
            raise OSError(msg)
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _fail_temp_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_publication(
            tmp_path,
            new_publication(
                settings=_test_indexing_settings(),
                collection="collection",
                indexed_count=1,
                source_signature="signature",
                published_revision=None,
            ),
        )

    assert attempted_temp_paths
    assert attempted_temp_paths[0].name != "publication.json.tmp"
    assert not attempted_temp_paths[0].exists()
    assert not publication_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_git_refresh_syncs_before_reindex_and_publishes_revision_without_secret_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git-backed refresh syncs first, publishes the revision, and persists no URL userinfo."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git index", encoding="utf-8")
    git_config = KnowledgeGitConfig(
        repo_url="https://ghp_secret:x-oauth-basic@example.com/org/repo.git",
        branch="main",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    order: list[str] = []
    original_reindex = KnowledgeManager.reindex_all

    async def _sync_success(self: GitKnowledgeSource) -> GitSyncResult:
        order.append("sync")
        return _record_git_sync(self, GitSyncResult(head="rev-git", updated=True), "doc.md")

    async def _track_reindex(self: KnowledgeManager, *, force_reindex: bool = False) -> RefreshOutcome:
        order.append("reindex")
        return await original_reindex(self, force_reindex=force_reindex)

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync_success)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)
    _install_git_revisions(monkeypatch, ["rev-git"])

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    storage = _storage(config, runtime_paths)
    publication = load_publication(storage)
    metadata_text = "".join(path.read_text(encoding="utf-8") for path in sorted(storage.glob("*.json")))

    assert result.index_published is True
    assert order == ["sync", "reindex"]
    assert publication is not None
    assert publication.published_revision == "rev-git"
    assert publication.source_signature == _corpus_hash(
        config,
        docs_path,
        git_dir=knowledge_git_dir(runtime_paths.storage_root, docs_path),
        tracked_relative_paths={"doc.md"},
    )
    assert "ghp_secret" not in metadata_text
    assert "x-oauth-basic" not in metadata_text


def _git_noop_config(tmp_path: Path, *, files: tuple[str, ...] = ("doc.md",)) -> tuple[Config, RuntimePaths]:
    """Build a Git-backed base used by the revision-gating tests."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    for name in files:
        (docs_path / name).write_text("git index", encoding="utf-8")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")},
    )
    return config, runtime_paths_for(config)


def _install_git_sync_results(
    monkeypatch: pytest.MonkeyPatch,
    results: list[GitSyncResult],
    *,
    tracked: tuple[str, ...] = ("doc.md",),
) -> None:
    """Drive Git source sync through a fixed sequence of poll outcomes."""

    async def _sync(self: GitKnowledgeSource) -> GitSyncResult:
        return _record_git_sync(self, results.pop(0), *tracked)

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync)


def _install_git_revisions(monkeypatch: pytest.MonkeyPatch, revisions: list[str | None]) -> None:
    """Return a fixed sequence of ``git rev-parse`` results, repeating the last."""
    pending = list(revisions)

    async def _rev_parse(self: KnowledgeManager, ref: str) -> str | None:
        del self, ref
        return pending.pop(0) if len(pending) > 1 else pending[0]

    monkeypatch.setattr(GitKnowledgeSource, "_rev_parse", _rev_parse)


@dataclass
class _SignatureCounter:
    """Count corpus-hash calls made through one module's imported binding."""

    calls: int = 0


def _install_counting_signature(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> _SignatureCounter:
    """Count corpus scans made through one module's ``scan_corpus`` binding without changing their behavior."""
    counter = _SignatureCounter()
    original_scan = module.scan_corpus

    def _counting_scan(
        config: Config,
        base_id: str,
        knowledge_root: Path,
        *,
        git_dir: Path,
        tracked_relative_paths: Iterable[str] | None = None,
        known: Mapping[str, FileSignature] | None = None,
    ) -> dict[str, tuple[FileSignature, Path]]:
        counter.calls += 1
        return original_scan(
            config,
            base_id,
            knowledge_root,
            git_dir=git_dir,
            tracked_relative_paths=tracked_relative_paths,
            known=known,
        )

    monkeypatch.setattr(module, "scan_corpus", _counting_scan)
    return counter


@pytest.mark.asyncio
async def test_git_noop_refresh_skips_full_reindex_when_index_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged Git poll should update sync metadata without rebuilding the collection."""
    config, runtime_paths = _git_noop_config(tmp_path)
    _install_git_sync_results(
        monkeypatch,
        [
            GitSyncResult(head="rev-a", updated=True),
            GitSyncResult(head="rev-b", updated=False),
        ],
    )
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _track_reindex(self: KnowledgeManager, *, force_reindex: bool = False) -> RefreshOutcome:
        nonlocal reindex_count
        reindex_count += 1
        if reindex_count > 1:
            msg = "unchanged git poll should not reindex"
            raise AssertionError(msg)
        return await original_reindex(self, force_reindex=force_reindex)

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)
    _install_git_revisions(monkeypatch, ["rev-a"])

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state_before_noop = _publication(config, runtime_paths)
    assert state_before_noop is not None
    assert state_before_noop.published_revision == "rev-a"
    await asyncio.sleep(0.001)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state_after_noop = _publication(config, runtime_paths)

    assert result.index_published is True
    assert result.indexed_count == 1
    assert state_after_noop is not None
    assert state_after_noop.collection == state_before_noop.collection
    assert state_after_noop.published_revision == "rev-b"
    assert state_after_noop.last_published_at != state_before_noop.last_published_at
    assert reindex_count == 1


@pytest.mark.asyncio
async def test_git_noop_refresh_skips_corpus_hash_when_revision_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unmoved Git revision proves the corpus is unchanged without reading every file."""
    config, runtime_paths = _git_noop_config(tmp_path)
    _install_git_sync_results(
        monkeypatch,
        [
            GitSyncResult(head="rev-a", updated=True),
            GitSyncResult(head="rev-a", updated=False),
        ],
    )
    _install_git_revisions(monkeypatch, ["rev-a"])
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    _forbid_corpus_scans(monkeypatch, "an unchanged Git revision must not re-hash the corpus")
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    publication = _publication(config, runtime_paths)
    assert result.index_published is True
    assert result.availability is KnowledgeAvailability.READY
    assert publication is not None
    assert publication.published_revision == "rev-a"


@pytest.mark.asyncio
async def test_git_noop_refresh_hashes_corpus_when_revision_moved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revision the published index was not built from still needs content verification."""
    config, runtime_paths = _git_noop_config(tmp_path)
    _install_git_sync_results(
        monkeypatch,
        [
            GitSyncResult(head="rev-a", updated=True),
            GitSyncResult(head="rev-b", updated=False),
        ],
    )
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    counter = _install_counting_signature(monkeypatch, knowledge_manager_module)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert counter.calls == 1
    assert result.index_published is True


@pytest.mark.asyncio
async def test_git_noop_refresh_hashes_corpus_when_index_predates_revision_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An index published without a recorded revision cannot be trusted by revision alone."""
    config, runtime_paths = _git_noop_config(tmp_path)
    _install_git_sync_results(
        monkeypatch,
        [
            GitSyncResult(head="rev-a", updated=True),
            GitSyncResult(head="rev-a", updated=False),
        ],
    )
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    storage = _storage(config, runtime_paths)
    published = load_publication(storage)
    assert published is not None
    save_publication(storage, replace(published, published_revision=None))

    counter = _install_counting_signature(monkeypatch, knowledge_manager_module)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert counter.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed_files", "expected_scanned"),
    [
        pytest.param(frozenset({"second.md"}), [("second.md",)], id="reliable-delta"),
        pytest.param(None, [("first.md", "second.md", "third.md")], id="full-scan-fallback"),
    ],
)
async def test_git_update_uses_reliable_delta_for_file_signature_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_files: frozenset[str] | None,
    expected_scanned: list[tuple[str, ...]],
) -> None:
    """A revision-matched Git delta should avoid hashing every unchanged file."""
    names = ("first.md", "second.md", "third.md")
    config, runtime_paths = _git_noop_config(tmp_path, files=names)
    _install_git_sync_results(
        monkeypatch,
        [
            GitSyncResult(head="rev-a", updated=True),
            GitSyncResult(head="rev-b", updated=True),
        ],
        tracked=names,
    )
    _install_git_revisions(monkeypatch, ["rev-a", "rev-a", "rev-b", "rev-b"])
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (tmp_path / "docs" / "second.md").write_text("changed", encoding="utf-8")

    async def _changed_files_between(
        _self: GitKnowledgeSource,
        before_head: str | None,
        after_head: str | None,
    ) -> frozenset[str] | None:
        assert before_head == "rev-a"
        assert after_head == "rev-b"
        return changed_files

    monkeypatch.setattr(GitKnowledgeSource, "changed_files_between", _changed_files_between)

    scanned: list[tuple[str, ...]] = []
    original_scan = knowledge_manager_module.scan_file_signatures_async

    async def _record_scanned_files(files: Sequence[tuple[str, Path]]) -> dict[str, tuple[FileSignature, Path]]:
        scanned.append(tuple(sorted(relative_path for relative_path, _path in files)))
        return await original_scan(files)

    monkeypatch.setattr(knowledge_manager_module, "scan_file_signatures_async", _record_scanned_files)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert result.indexed_count == 1
    assert scanned == expected_scanned


@pytest.mark.asyncio
async def test_git_candidate_cancelled_before_reconciliation_rescans_on_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh cancelled before reconciling keeps its candidate at the revision its claims describe."""
    config, runtime_paths = _git_noop_config(tmp_path)
    _install_git_sync_results(
        monkeypatch,
        [
            GitSyncResult(head="rev-a", updated=True),
            GitSyncResult(head="rev-b", updated=True),
            GitSyncResult(head="rev-b", updated=False),
        ],
    )
    _install_git_revisions(monkeypatch, ["rev-a", "rev-a", "rev-b", "rev-b"])
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (tmp_path / "docs" / "doc.md").write_text("changed", encoding="utf-8")

    delta_calls = 0

    async def _cancel_once(
        _self: GitKnowledgeSource,
        before_head: str | None,
        after_head: str | None,
    ) -> frozenset[str]:
        nonlocal delta_calls
        delta_calls += 1
        if delta_calls == 1:
            raise asyncio.CancelledError
        return frozenset({"doc.md"}) if (before_head, after_head) == ("rev-a", "rev-b") else frozenset()

    monkeypatch.setattr(GitKnowledgeSource, "changed_files_between", _cancel_once)

    with pytest.raises(asyncio.CancelledError):
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert result.indexed_count == 1


@pytest.mark.asyncio
async def test_git_delta_targets_the_revision_verified_by_the_refresh_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first reconciliation delta must cover the round's exact Git revision."""
    names = ("first.md", "second.md")
    config, runtime_paths = _git_noop_config(tmp_path, files=names)
    _install_git_sync_results(
        monkeypatch,
        [
            GitSyncResult(head="rev-a", updated=True),
            GitSyncResult(head="rev-b", updated=True),
        ],
        tracked=names,
    )
    _install_git_revisions(monkeypatch, ["rev-a", "rev-a", "rev-c", "rev-c"])
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (tmp_path / "docs" / "first.md").write_text("changed in rev-b", encoding="utf-8")

    async def _changed_files_between(
        _self: GitKnowledgeSource,
        _before_head: str | None,
        after_head: str | None,
    ) -> frozenset[str]:
        (tmp_path / "docs" / "second.md").write_text("changed in rev-c", encoding="utf-8")
        if after_head == "rev-c":
            return frozenset(names)
        return frozenset({"first.md"})

    monkeypatch.setattr(GitKnowledgeSource, "changed_files_between", _changed_files_between)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert result.indexed_count == 2


@pytest.mark.asyncio
async def test_git_publish_records_verified_revision_before_later_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Published metadata must identify the revision whose vectors were verified."""
    config, runtime_paths = _git_noop_config(tmp_path)
    _install_git_sync_results(
        monkeypatch,
        [
            GitSyncResult(head="rev-a", updated=True),
            GitSyncResult(head="rev-b", updated=True),
            GitSyncResult(head="rev-b", updated=True),
        ],
    )
    _install_git_revisions(monkeypatch, ["rev-a", "rev-a", "rev-c", "rev-c", "rev-b", "rev-b"])

    async def _changed_files_between(
        _self: GitKnowledgeSource,
        before_head: str | None,
        after_head: str | None,
    ) -> frozenset[str] | None:
        if before_head is None:
            return None
        return frozenset({"doc.md"}) if before_head != after_head else frozenset()

    monkeypatch.setattr(GitKnowledgeSource, "changed_files_between", _changed_files_between)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    docs_path = tmp_path / "docs"
    (docs_path / "doc.md").write_text("content at rev-c", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state_at_rev_c = _publication(config, runtime_paths)

    (docs_path / "doc.md").write_text("content at rev-b", encoding="utf-8")
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state_at_rev_b = _publication(config, runtime_paths)

    assert state_at_rev_c is not None
    assert state_at_rev_c.published_revision == "rev-c"
    assert state_at_rev_b is not None
    assert state_at_rev_b.published_revision == "rev-b"
    assert state_at_rev_b.source_signature == _corpus_hash(
        config,
        docs_path,
        git_dir=knowledge_git_dir(runtime_paths.storage_root, docs_path),
        tracked_relative_paths={"doc.md"},
    )
    assert result.index_published is True


@pytest.mark.asyncio
async def test_reindex_skips_live_corpus_hash_when_revision_is_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Git revision that held still proves the source did not move while the pass ran."""
    config, runtime_paths = _git_noop_config(tmp_path)
    _install_git_sync_results(
        monkeypatch,
        [GitSyncResult(head="rev-a", updated=True)],
    )
    _install_git_revisions(monkeypatch, ["rev-a"])

    _forbid_corpus_scans(monkeypatch, "a stable Git revision must not re-hash the corpus after indexing")

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert result.indexed_count == 1


@pytest.mark.asyncio
async def test_reindex_does_not_publish_a_corpus_truncated_by_a_transient_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file lost to a transient stat error must not publish as a complete index.

    Files whose signature scan raises are dropped from the pass's own completeness
    accounting, so the post-pass check is the only thing standing between a
    truncated corpus and publication. Once published at a revision, the unchanged
    fast path would keep republishing it, so the file would never come back.
    """
    config, runtime_paths = _git_noop_config(tmp_path, files=("keep.md", "flaky.md"))
    _install_git_sync_results(
        monkeypatch,
        [GitSyncResult(head="rev-a", updated=True)],
        tracked=("keep.md", "flaky.md"),
    )
    _install_git_revisions(monkeypatch, ["rev-a"])

    original_signature = knowledge_corpus_signature._descriptor_signature
    remaining_failures = {"flaky.md": 1}

    def _flaky_signature(descriptor: int, file_path: Path, output: IO[bytes] | None = None) -> FileSignature:
        if output is None and remaining_failures.get(file_path.name):
            remaining_failures[file_path.name] -= 1
            raise OSError(116, "Stale file handle")
        return original_signature(descriptor, file_path, output)

    monkeypatch.setattr(knowledge_corpus_signature, "_descriptor_signature", _flaky_signature)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.indexed_count == 2
    publication = _publication(config, runtime_paths)
    assert publication is not None
    assert publication.indexed_count == 2


@pytest.mark.asyncio
async def test_reindex_reconciles_when_revision_moves_mid_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revision that moved while the pass ran must reconcile before publishing."""
    config, runtime_paths = _git_noop_config(tmp_path)
    _install_git_sync_results(
        monkeypatch,
        [GitSyncResult(head="rev-a", updated=True)],
    )
    # Round one starts at rev-a and finds rev-b after indexing; round two is stable.
    _install_git_revisions(monkeypatch, ["rev-a", "rev-b", "rev-b"])

    with capture_logs() as logs:
        result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    events = [entry.get("event") for entry in logs]
    assert "Knowledge source changed during refresh; reconciling candidate" in events
    assert result.index_published is True


@pytest.mark.asyncio
async def test_reindex_hashes_live_corpus_for_non_git_bases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local base has no revision to trust, so it still verifies by content."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("local index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    counter = _install_counting_signature(monkeypatch, knowledge_manager_module)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert counter.calls >= 1
    assert result.index_published is True


@pytest.mark.asyncio
async def test_non_git_refresh_publishes_when_file_and_directory_share_a_stem(tmp_path: Path) -> None:
    """Stable corpora publish when a file and directory share a stem."""
    docs_path = tmp_path / "docs"
    nested_path = docs_path / "topic"
    nested_path.mkdir(parents=True)
    (docs_path / "topic.md").write_text("top-level topic", encoding="utf-8")
    (nested_path / "details.md").write_text("nested topic details", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert result.availability is KnowledgeAvailability.READY
    assert result.indexed_count == 2


@pytest.mark.asyncio
async def test_git_noop_refresh_ignores_untracked_indexable_file_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git-backed corpora use tracked files only and ignore untracked checkout files.

    The second poll reports a moved revision on purpose. An unmoved revision
    short-circuits change detection entirely, which would let this test pass
    without ever exercising the tracked-only filtering it exists to pin.
    """
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git tracked index", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    sync_results = [
        GitSyncResult(head="rev-a", updated=True),
        GitSyncResult(head="rev-b", updated=False),
    ]
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _sync(self: GitKnowledgeSource) -> GitSyncResult:
        return _record_git_sync(self, sync_results.pop(0), "doc.md")

    async def _track_reindex(self: KnowledgeManager, *, force_reindex: bool = False) -> RefreshOutcome:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self, force_reindex=force_reindex)

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (docs_path / "untracked.md").write_text("git untracked local corpus", encoding="utf-8")
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert reindex_count == 1
    assert lookup.reader is not None
    assert [document.content for document in await lookup.reader.asearch("git", limit=5)] == [
        "git tracked index",
    ]


@pytest.mark.asyncio
async def test_git_noop_refresh_rebuilds_when_collection_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged Git poll must not recreate a collection for a missing index."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git repaired", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    sync_results = [
        GitSyncResult(head="rev-a", updated=True),
        GitSyncResult(head="rev-a", updated=False),
    ]
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _sync(self: GitKnowledgeSource) -> GitSyncResult:
        return _record_git_sync(self, sync_results.pop(0), "doc.md")

    async def _track_reindex(self: KnowledgeManager, *, force_reindex: bool = False) -> RefreshOutcome:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self, force_reindex=force_reindex)

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_publication(published_index_storage_path(key))
    assert state is not None
    assert state.collection is not None
    missing_collection = state.collection
    KnowledgeCollection(path=published_index_storage_path(key), name=missing_collection).delete()
    knowledge_registry._published_indexes.clear()
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    repaired_state = load_publication(published_index_storage_path(key))

    assert result.index_published is True
    assert reindex_count == 2
    assert repaired_state is not None
    assert repaired_state.collection != missing_collection
    assert not KnowledgeCollection(path=published_index_storage_path(key), name=missing_collection).exists()
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.reader is not None
    assert [document.content for document in await lookup.reader.asearch("git", limit=5)] == [
        "git repaired",
    ]


@pytest.mark.asyncio
async def test_unchanged_refresh_builds_no_embedder_and_republishes_only_an_existing_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unchanged path needs neither an embedder nor a reader, and never claims success for a missing collection."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("unchanged index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    published = _publication(config, runtime_paths)
    assert published is not None
    assert published.collection is not None
    with monkeypatch.context() as no_embedder:
        refusing = MagicMock(side_effect=AssertionError("an unchanged refresh built an embedder"))
        no_embedder.setattr(knowledge_manager_module, "create_configured_embedder", refusing)
        no_embedder.setattr(knowledge_registry, "create_configured_embedder", refusing)

        unchanged = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert unchanged.index_published is True
    assert unchanged.availability is KnowledgeAvailability.READY
    assert _publication(config, runtime_paths) == published

    KnowledgeCollection(path=_storage(config, runtime_paths), name=published.collection).delete()
    rebuilt = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    republished = _publication(config, runtime_paths)

    assert rebuilt.index_published is True
    assert rebuilt.indexed_count == 1
    assert republished is not None
    assert republished.collection != published.collection
    assert KnowledgeCollection(path=_storage(config, runtime_paths), name=str(republished.collection)).exists()


@pytest.mark.asyncio
async def test_git_noop_refresh_rebuilds_after_chunking_config_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunking changes must rebuild even when Git reports no repository updates."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("git chunking old", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    sync_results = [
        GitSyncResult(head="rev-a", updated=True),
        GitSyncResult(head="rev-a", updated=False),
    ]
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _sync(self: GitKnowledgeSource) -> GitSyncResult:
        return _record_git_sync(self, sync_results.pop(0), "doc.md")

    async def _track_reindex(self: KnowledgeManager, *, force_reindex: bool = False) -> RefreshOutcome:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self, force_reindex=force_reindex)

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("git chunking rebuilt", encoding="utf-8")
    result = await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert reindex_count == 2
    lookup = get_published_index("docs", config=changed_config, runtime_paths=runtime_paths)
    assert lookup.reader is not None
    assert [document.content for document in await lookup.reader.asearch("git", limit=5)] == [
        "git chunking rebuilt",
    ]


@pytest.mark.asyncio
async def test_force_git_reindex_bypasses_noop_fast_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit reindex should rebuild even when Git reports updated=False."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("git force old", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    sync_results = [
        GitSyncResult(head="rev-a", updated=True),
        GitSyncResult(head="rev-a", updated=False),
    ]
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _sync(self: GitKnowledgeSource) -> GitSyncResult:
        return _record_git_sync(self, sync_results.pop(0), "doc.md")

    async def _track_reindex(self: KnowledgeManager, *, force_reindex: bool = False) -> RefreshOutcome:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self, force_reindex=force_reindex)

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("git force rebuilt", encoding="utf-8")
    result = await refresh_knowledge_binding(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
        force_reindex=True,
    )

    assert result.index_published is True
    assert reindex_count == 2
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.reader is not None
    assert [document.content for document in await lookup.reader.asearch("git", limit=5)] == [
        "git force rebuilt",
    ]


@pytest.mark.asyncio
async def test_an_interrupted_forced_reindex_is_finished_by_the_next_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next refresh resumes a forced rebuild cut short, skipping the unchanged shortcut until it publishes."""
    # One file at a time, in name order, so the rebuild is cut short after "a.md".
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=1\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "a.md").write_text("alpha", encoding="utf-8")
    (docs_path / "b.md").write_text("bravo", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    replaced = _publication(config, runtime_paths)
    assert replaced is not None
    original_index_file = FileIndexer._index_file
    indexed: list[str] = []
    cut_short = asyncio.Event()

    async def _cut_short_at_b(self: FileIndexer, prepared: _PreparedFile, **kwargs: object) -> FileSignature | None:
        if prepared.relative_path == "b.md":
            cut_short.set()
            await asyncio.Event().wait()
        return await original_index_file(self, prepared, **kwargs)

    async def _record_indexed(self: FileIndexer, prepared: _PreparedFile, **kwargs: object) -> FileSignature | None:
        indexed.append(prepared.relative_path)
        return await original_index_file(self, prepared, **kwargs)

    monkeypatch.setattr(FileIndexer, "_index_file", _cut_short_at_b)
    forced = asyncio.create_task(
        refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths, force_reindex=True),
    )
    await asyncio.wait_for(cut_short.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    forced.cancel()
    with pytest.raises(asyncio.CancelledError):
        await forced
    [rebuild] = _working_collections(config, runtime_paths)
    assert rebuild.meta().rebuild is True
    assert set(rebuild.files()) == {"a.md"}
    assert _publication(config, runtime_paths) == replaced

    monkeypatch.setattr(FileIndexer, "_index_file", _record_indexed)
    finished = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    publication = _publication(config, runtime_paths)

    assert finished.index_published is True
    assert indexed == ["b.md"]
    assert publication is not None
    assert publication.collection == rebuild.name
    # A forced rebuild keeps none of the vectors it replaced.
    assert publication.retired is None
    assert not KnowledgeCollection(path=_storage(config, runtime_paths), name=str(replaced.collection)).exists()

    unchanged = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert unchanged.index_published is True
    assert indexed == ["b.md"]
    assert _publication(config, runtime_paths) == publication


@pytest.mark.asyncio
async def test_git_sync_failure_preserves_last_good_index_and_redacts_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Git sync failure keeps the last-good index available under stale metadata."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("stable git index", encoding="utf-8")
    git_config = KnowledgeGitConfig(
        repo_url="https://ghp_secret:x-oauth-basic@example.com/org/repo.git",
        branch="main",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)

    async def _sync_success(self: GitKnowledgeSource) -> GitSyncResult:
        return _record_git_sync(self, GitSyncResult(head="rev-ok", updated=True), "doc.md")

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync_success)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    async def _sync_failure(self: GitKnowledgeSource) -> GitSyncResult:
        _ = self
        msg = "fetch failed https://ghp_secret:x-oauth-basic@example.com/org/repo.git"
        raise RuntimeError(msg)

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync_failure)
    with pytest.raises(RuntimeError, match="fetch failed"):
        await run_refresh_child("docs", config=config, runtime_paths=runtime_paths)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    last_error = _refresh_status(config, runtime_paths).last_error
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is False
    assert _refresh_state_for_key(key) == "refresh_failed"
    assert last_error is not None
    assert "fetch failed" in last_error
    assert "ghp_secret" not in last_error
    assert "x-oauth-basic" not in last_error
    assert lookup.reader is not None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED
    assert [document.content for document in await lookup.reader.asearch("index", limit=5)] == [
        "stable git index",
    ]


@pytest.mark.asyncio
async def test_cold_git_sync_failure_records_failed_availability_and_redacted_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first Git failure is observable as refresh_failed instead of initializing."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    git_config = KnowledgeGitConfig(
        repo_url="https://ghp_secret:x-oauth-basic@example.com/org/repo.git",
        branch="main",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)

    async def _sync_failure(self: GitKnowledgeSource) -> GitSyncResult:
        _ = self
        msg = "clone failed https://ghp_secret:x-oauth-basic@example.com/org/repo.git"
        raise RuntimeError(msg)

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync_failure)

    with pytest.raises(RuntimeError, match="clone failed"):
        await run_refresh_child("docs", config=config, runtime_paths=runtime_paths)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    last_error = _refresh_status(config, runtime_paths).last_error
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.availability is KnowledgeAvailability.REFRESH_FAILED
    assert _publication(config, runtime_paths) is None
    assert last_error is not None
    assert "clone failed" in last_error
    assert "ghp_secret" not in last_error
    assert "x-oauth-basic" not in last_error
    assert _refresh_state_for_key(key) == "refresh_failed"
    assert lookup.reader is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED


@pytest.mark.asyncio
async def test_git_failure_redacts_authorization_headers_from_raised_and_metadata_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Process-local Git Authorization headers should not leak through command failures."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    git_config = KnowledgeGitConfig(
        repo_url="https://example.com/org/repo.git",
        branch="main",
        credentials_service="github_private",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials(
        "github_private",
        {"token": "secret-token"},
    )
    encoded = base64.b64encode(b"x-access-token:secret-token").decode("ascii")
    bearer_value = "bearer-value"
    stderr = (
        "fatal: clone failed\n"
        f"GIT_CONFIG_VALUE_0=Authorization: Basic {encoded}\n"
        "decoded credential x-access-token:secret-token\n"
        f"Authorization: Bearer {bearer_value}\n"
    )

    class _FailedGitProcess:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", stderr.encode()

        def kill(self) -> None:
            return None

        async def wait(self) -> None:
            return None

    async def _fail_git_command(*args: object, **kwargs: object) -> _FailedGitProcess:
        _ = (args, kwargs)
        return _FailedGitProcess()

    monkeypatch.setenv(knowledge_git_source_module._REFRESH_SUBPROCESS_ENV, "1")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail_git_command)

    with pytest.raises(RuntimeError) as exc_info:
        await run_refresh_child("docs", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    error_texts = [
        str(exc_info.value),
        _refresh_status(config, runtime_paths).last_error or "",
        lookup.status.last_error or "",
    ]

    assert _refresh_state_for_key(key) == "refresh_failed"
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED
    for error_text in error_texts:
        assert "Authorization: Basic ***" in error_text
        assert "Authorization: Bearer ***" in error_text
        assert encoded not in error_text
        assert "x-access-token:secret-token" not in error_text
        assert "secret-token" not in error_text
        assert bearer_value not in error_text


@pytest.mark.asyncio
async def test_git_refresh_marks_duplicate_source_sibling_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Git update for one base must not leave sibling indexes READY for the old checkout."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("shared git old", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"alpha": docs_path, "beta": docs_path},
        agent_bases=["alpha", "beta"],
        git_configs={"alpha": git_config, "beta": git_config},
    )
    runtime_paths = runtime_paths_for(config)

    async def _sync_updated(self: GitKnowledgeSource) -> GitSyncResult:
        return _record_git_sync(self, GitSyncResult(head=f"rev-{self.base_id}", updated=True), "doc.md")

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync_updated)

    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("beta", config=config, runtime_paths=runtime_paths)
    beta_lookup = get_published_index("beta", config=config, runtime_paths=runtime_paths)
    assert beta_lookup.reader is not None
    assert beta_lookup.availability is KnowledgeAvailability.READY
    assert [document.content for document in await beta_lookup.reader.asearch("git", limit=5)] == [
        "shared git old",
    ]

    doc.write_text("shared git new", encoding="utf-8")
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    beta_key = resolve_published_index_key("beta", config=config, runtime_paths=runtime_paths)
    refreshed_beta_lookup = get_published_index("beta", config=config, runtime_paths=runtime_paths)

    # The runtime marks the alias once the refresh that moved the shared checkout reports it.
    assert _refresh_state_for_key(beta_key) == "stale"
    assert _refresh_status(config, runtime_paths, "beta").reason == "git_source_updated"
    assert refreshed_beta_lookup.reader is not None
    assert refreshed_beta_lookup.availability is KnowledgeAvailability.STALE
    assert [document.content for document in await refreshed_beta_lookup.reader.asearch("git", limit=5)] == [
        "shared git old",
    ]


@pytest.mark.asyncio
async def test_git_pull_that_changes_one_file_only_reindexes_that_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-file commit must cost one file's indexing, not the whole checkout's."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote_work = tmp_path / "remote-work"
    remote_work.mkdir()

    async def _git(cwd: Path, *args: str) -> None:
        await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    await _git(remote_work, "init", "-b", "main")
    await _git(remote_work, "config", "user.email", "tests@example.com")
    await _git(remote_work, "config", "user.name", "MindRoom Tests")
    for index in range(5):
        (remote_work / f"doc{index}.md").write_text(f"original body {index}", encoding="utf-8")
    await _git(remote_work, "add", ".")
    await _git(remote_work, "commit", "-m", "seed")
    remote_bare = tmp_path / "remote.git"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", "--bare", str(remote_work), str(remote_bare)],
        check=True,
        capture_output=True,
        text=True,
    )

    docs_path = tmp_path / "checkout"
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url=str(remote_bare), branch="main")},
    )
    runtime_paths = runtime_paths_for(config)
    first = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert first.indexed_count == 5

    (remote_work / "doc2.md").write_text("rewritten body 2", encoding="utf-8")
    await _git(remote_work, "commit", "-am", "change one file")
    await _git(remote_work, "push", str(remote_bare), "main")

    second = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert second.index_published is True
    assert second.indexed_count == 1, "the whole checkout was reindexed for a one-file commit"
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.reader is not None
    contents = sorted(document.content for document in await lookup.reader.asearch("body", limit=10))
    assert contents == [
        "original body 0",
        "original body 1",
        "original body 3",
        "original body 4",
        "rewritten body 2",
    ]


@pytest.mark.asyncio
async def test_linked_worktree_checkout_is_refused_and_not_listed(tmp_path: Path) -> None:
    """A .git pointer file is never followed: its target could be any repository.

    Earlier releases ran Git in such a checkout, which let whoever could write
    the pointer choose the repository whose config and hooks Git executed.
    """
    remote_work = tmp_path / "remote-work"
    remote_work.mkdir()

    async def _git(cwd: Path, *args: str) -> None:
        await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    await _git(remote_work, "init", "-b", "main")
    await _git(remote_work, "config", "user.email", "tests@example.com")
    await _git(remote_work, "config", "user.name", "MindRoom Tests")
    (remote_work / "doc.md").write_text("worktree checkout content", encoding="utf-8")
    await _git(remote_work, "add", "doc.md")
    await _git(remote_work, "commit", "-m", "main")
    remote_bare = tmp_path / "remote.git"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", "--bare", str(remote_work), str(remote_bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    seed_checkout = tmp_path / "seed-checkout"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", str(remote_bare), str(seed_checkout)],
        check=True,
        capture_output=True,
        text=True,
    )
    docs_path = tmp_path / "worktree-checkout"
    await _git(seed_checkout, "worktree", "add", "--detach", str(docs_path), "HEAD")
    assert (docs_path / ".git").is_file()

    git_config = KnowledgeGitConfig(repo_url=str(remote_bare), branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
    resolved_git_config = manager.git_source._git_config()
    assert resolved_git_config is not None

    with pytest.raises(RuntimeError, match="is a link or a file, not a Git directory"):
        await manager.git_source._ensure_repository(resolved_git_config)
    git_dir = manager.git_source.git_dir

    assert (docs_path / ".git").is_file()
    assert (docs_path / "doc.md").read_text(encoding="utf-8") == "worktree checkout content"
    assert not git_checkout_present(docs_path, git_dir)
    assert list_git_tracked_knowledge_files(config, "docs", docs_path, git_dir) == []

    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    client = TestClient(main.app, base_url="http://localhost")
    response = client.get("/api/knowledge/bases/docs/status")

    assert response.status_code == 200
    assert response.json()["git"]["repo_present"] is False


@pytest.mark.asyncio
async def test_candidate_indexing_hashes_content_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-file content hashing should run in a worker thread."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("threaded hash", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    event_loop_thread = get_ident()
    signature_threads: list[int] = []
    original_file_signature = knowledge_file_indexer.file_signature

    def _record_signature_thread(file_path: Path, snapshot: Path | None = None) -> FileSignature:
        signature_threads.append(get_ident())
        return original_file_signature(file_path, snapshot)

    monkeypatch.setattr(knowledge_file_indexer, "file_signature", _record_signature_thread)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert signature_threads
    assert all(thread_id != event_loop_thread for thread_id in signature_threads)


@pytest.mark.asyncio
async def test_git_updated_stale_registry_mark_uses_async_registry_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime marks a moved checkout's aliases through the async marker, which keeps status I/O off the loop."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git updated", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    event_loop_thread = get_ident()
    mark_threads: list[int] = []

    async def _sync_updated(self: GitKnowledgeSource) -> GitSyncResult:
        return _record_git_sync(self, GitSyncResult(head="rev-updated", updated=True), "doc.md")

    async def _record_mark_thread(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        mark_threads.append(get_ident())
        return ("docs",)

    monkeypatch.setattr(GitKnowledgeSource, "sync", _sync_updated)
    monkeypatch.setattr(knowledge_refresh_runner, "mark_knowledge_source_changed_async", _record_mark_thread)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert mark_threads == [event_loop_thread]


@pytest.mark.asyncio
async def test_refresh_scheduler_manual_reindex_waits_behind_the_running_refresh_and_absorbs_the_queued_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An awaited manual reindex runs after the refresh already running, standing in for the one queued behind it."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    old_config = config.model_copy(deep=True)
    old_config.knowledge_bases["docs"].chunk_size = 1024
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    seen: list[tuple[int, bool]] = []

    async def _fake_refresh(base_id: str, **kwargs: object) -> object:
        assert base_id == "docs"
        refresh_config = kwargs["config"]
        assert isinstance(refresh_config, Config)
        force_reindex = bool(kwargs.get("force_reindex", False))
        seen.append((refresh_config.knowledge_bases["docs"].chunk_size, force_reindex))
        if len(seen) == 1:
            first_started.set()
            await release_first.wait()
        return knowledge_refresh_runner.KnowledgeRefreshResult(
            indexed_count=1,
            index_published=True,
            availability=KnowledgeAvailability.READY,
        )

    monkeypatch.setattr("mindroom.knowledge.refresh_runner.refresh_knowledge_binding_in_subprocess", _fake_refresh)

    scheduler.schedule_refresh("docs", config=old_config, runtime_paths=runtime_paths)
    await first_started.wait()
    scheduler.schedule_refresh("docs", config=old_config, runtime_paths=runtime_paths)
    manual_task = asyncio.create_task(
        scheduler.refresh_now("docs", config=config, runtime_paths=runtime_paths, force_reindex=True),
    )
    await asyncio.sleep(0)
    assert seen == [(1024, False)]
    release_first.set()
    result = await manual_task
    await _wait_until(lambda: not scheduler._tasks, "manual refresh left a stale background refresh running")
    await scheduler.shutdown()

    assert seen == [(1024, False), (5000, True)]
    assert result.index_published is True


@pytest.mark.asyncio
async def test_an_abandoned_manual_reindex_keeps_running_for_the_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that stops waiting for a manual reindex does not cancel it; it still publishes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("abandoned reindex", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    target = knowledge_registry.resolve_refresh_target("docs", config=config, runtime_paths=runtime_paths)
    entered, release = _block_reindex(monkeypatch)

    waiter = asyncio.create_task(
        scheduler.refresh_now("docs", config=config, runtime_paths=runtime_paths, force_reindex=True),
    )
    await asyncio.wait_for(entered.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    try:
        assert scheduler.is_refreshing(target)
        release.set()
        await _wait_until(lambda: not scheduler.is_refreshing(target), "the abandoned reindex did not finish")
    finally:
        release.set()
        await scheduler.shutdown()

    publication = _publication(config, runtime_paths)
    assert publication is not None
    assert publication.indexed_count == 1
    assert _refresh_state_for_key(resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)) == (
        "none"
    )


def test_credential_free_repo_url_drops_http_userinfo_query_and_fragment() -> None:
    """A persistent clone URL keeps no HTTP userinfo, query string, or fragment."""
    assert (
        credential_free_repo_url(
            "https://user:password@example.com/repo.git?token=secret#frag-secret",
        )
        == "https://example.com/repo.git"
    )


def test_credential_free_repo_url_preserves_passwordless_ssh_username() -> None:
    """Passwordless SSH transport usernames are part of the clone identity."""
    assert (
        credential_free_repo_url(
            "ssh://git@example.com/org/repo.git;token=secret?query=secret#frag-secret",
        )
        == "ssh://git@example.com/org/repo.git"
    )


def test_credential_free_repo_url_strips_secret_bearing_userinfo() -> None:
    """Persistent clone URLs must not retain passwords, HTTP userinfo, query strings, or fragments."""
    assert (
        credential_free_repo_url(
            "ssh://git:secret@example.com/org/repo.git;token=secret?query=secret#frag-secret",
        )
        == "ssh://example.com/org/repo.git"
    )
    assert (
        credential_free_repo_url(
            "https://user@example.com/org/repo.git;token=secret?query=secret#frag-secret",
        )
        == "https://example.com/org/repo.git"
    )


def test_git_url_identity_preserves_passwordless_ssh_usernames() -> None:
    """Passwordless SSH usernames are identity, but secret-bearing userinfo is not."""
    assert credential_free_url_identity("ssh://git@example.com/org/repo.git") != credential_free_url_identity(
        "ssh://deploy@example.com/org/repo.git",
    )
    assert credential_free_url_identity("ssh://user:old@example.com/org/repo.git") == credential_free_url_identity(
        "ssh://user:new@example.com/org/repo.git",
    )
    assert credential_free_url_identity(
        "git+https://user:old@example.com/org/repo.git",
    ) == credential_free_url_identity("git+https://user:new@example.com/org/repo.git")
    assert credential_free_url_identity(
        "ssh://user:old@example.com/org/repo.git;token=secret?query=secret#frag-secret",
    ) == credential_free_url_identity("ssh://example.com/org/repo.git")


@pytest.mark.asyncio
async def test_index_file_runs_off_event_loop_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-file indexing must run on a worker thread so the asyncio loop stays responsive.

    The store's row writes are synchronous SQLite calls, and embedding each chunk is a
    blocking provider request; on the event loop either would block Matrix
    sync, tool calls, and cache writes for every file's embed and write cycle.
    """
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("hello", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    main_thread_id = get_ident()
    write_thread_ids: list[int] = []
    original_write = knowledge_file_indexer.write_file_rows

    def _record_write(*args: object, **kwargs: object) -> None:
        write_thread_ids.append(get_ident())
        original_write(*args, **kwargs)

    monkeypatch.setattr(knowledge_file_indexer, "write_file_rows", _record_write)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert write_thread_ids, "expected at least one row write during refresh"
    for thread_id in write_thread_ids:
        assert thread_id != main_thread_id, (
            f"A file's rows were written on the asyncio main thread (id={thread_id}); "
            "indexing must run on a worker thread via asyncio.to_thread."
        )


@pytest.mark.asyncio
async def test_malformed_json_falls_back_to_text_and_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed JSON remains searchable instead of blocking the whole candidate."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    malformed = '{\n  "claim": "still useful",\n  “broken”: true\n}\n'
    source_path = docs_path / "claim.json"
    source_path.write_text(malformed, encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    original_read_text = Path.read_text
    source_reads = 0

    def _count_source_reads(path: Path, *args: object, **kwargs: object) -> str:
        nonlocal source_reads
        # Readers open a private same-name snapshot of the listed source.
        if path.name == source_path.name:
            source_reads += 1
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _count_source_reads)

    with capture_logs() as logs:
        result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert lookup.reader is not None
    documents = await lookup.reader.asearch("still useful", limit=5)
    assert len(documents) == 1
    assert '"claim": "still useful"' in documents[0].content
    assert "“broken”: true" in documents[0].content
    fallback = [entry for entry in logs if entry["event"] == "Malformed JSON knowledge file; indexing as text"]
    assert [(entry["path"], entry["line"], entry["column"]) for entry in fallback] == [("claim.json", 3, 3)]
    assert source_reads == 1


@pytest.mark.asyncio
async def test_valid_json_keeps_structured_reader(tmp_path: Path) -> None:
    """Valid JSON lists remain separate structured documents."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "claims.json").write_text('[{"claim": "one"}, {"claim": "two"}]', encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    with capture_logs() as logs:
        result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert lookup.reader is not None
    assert [document.content for document in await lookup.reader.asearch("claim", limit=5)] == [
        '{"claim": "one"}',
        '{"claim": "two"}',
    ]
    assert all(entry["event"] != "Malformed JSON knowledge file; indexing as text" for entry in logs)


@pytest.mark.asyncio
async def test_valid_json_does_not_hide_downstream_json_decode_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A downstream JSONDecodeError remains a failure when source JSON is valid."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "claim.json").write_text('{"claim": "valid"}', encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    def _fail_after_read(*args: object, **kwargs: object) -> None:
        # The file was read and embedded; a later step fails with a JSON decode error of its own.
        del args, kwargs
        message = "downstream response was not JSON"
        raise json.JSONDecodeError(message, "<html>", 0)

    monkeypatch.setattr(knowledge_file_indexer, "write_file_rows", _fail_after_read)

    with capture_logs() as logs:
        result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is False
    assert (
        result.last_error
        == "Indexed 0 of 1 managed knowledge files (first error: knowledge indexing failed (JSONDecodeError))"
    )
    assert all(entry["event"] != "Malformed JSON knowledge file; indexing as text" for entry in logs)


#: Repository URLs that must never be written to ``.git/config``. The first four
#: are documented provider credential forms with the scheme mistyped or dropped;
#: ``urlparse`` reports the username as a scheme and finds no authority, so a
#: check that trusted the parse would treat the password as a hostname.
_UNPERSISTABLE_REPO_URLS = [
    pytest.param("oauth2:{secret}@gitlab.com:org/repo.git", id="gitlab-oauth2-form"),
    pytest.param("x-access-token:{secret}@github.com/org/repo.git", id="github-token-form"),
    pytest.param("user:{secret}@github.com:org/repo.git", id="userinfo-without-scheme"),
    pytest.param("user%3A{secret}@github.com:org/repo.git", id="encoded-colon-without-scheme"),
    pytest.param("https:{secret}@host/x", id="scheme-without-slashes"),
    pytest.param("HTTPS:{secret}@host/x", id="uppercase-scheme-without-slashes"),
    pytest.param("http:///git-user:{secret}@example.com/org/repo.git", id="empty-authority"),
    pytest.param("//git-user:{secret}@example.com/org/repo.git", id="protocol-relative"),
    pytest.param("https://user%3A{secret}%40example.com/repo.git", id="percent-encoded-authority"),
    pytest.param("https://user%253A{secret}%2540example.com/repo.git", id="double-encoded-authority"),
    pytest.param("https://host/https://u:{secret}@inner/x", id="nested-url"),
]

#: Remote forms that must keep working. Refusing any of these would break a
#: supported configuration, so the gate has to admit them positively.
_PERSISTABLE_REPO_URLS = [
    ("https://example.com/org/repo.git", "https://example.com/org/repo.git"),
    ("https://user:{secret}@example.com/org/repo.git", "https://example.com/org/repo.git"),
    ("ssh://git@example.com/org/repo.git", "ssh://git@example.com/org/repo.git"),
    ("ssh://git:{secret}@example.com/org/repo.git", "ssh://example.com/org/repo.git"),
    ("git@github.com:org/repo.git", "git@github.com:org/repo.git"),
    ("github.com:org/repo.git", "github.com:org/repo.git"),
    ("git@my_host.com:o/r.git", "git@my_host.com:o/r.git"),
    ("https://[::1]:8443/org/repo.git", "https://[::1]:8443/org/repo.git"),
    ("file:///srv/repos/x.git", "file:///srv/repos/x.git"),
    ("/srv/repos/x.git", "/srv/repos/x.git"),
    ("https://host:8443/a@b", "https://host:8443/a@b"),
    ("https://host/@scope/pkg.git", "https://host/@scope/pkg.git"),
]

#: Netloc codepoints that NFKC-normalise to a URL delimiter. ``urlsplit``
#: rejects these and quotes the offending netloc -- password included -- in the
#: exception, and no redactor can clean that message because it holds no ASCII
#: delimiter to anchor on. Both are reachable from ``repo_url``, an unvalidated
#: string, and neither needs a credential present to make ``urlparse`` raise.
_LOOKALIKE_SEPARATORS = ["\uff20", "\ufe6b", "\uff1a"]


@pytest.mark.parametrize("repo_url_template", _UNPERSISTABLE_REPO_URLS)
def test_unsafe_remote_url_is_refused_rather_than_persisted(repo_url_template: str) -> None:
    """A remote URL that cannot be parsed is refused, not sanitised.

    Writing the checkout's ``origin`` is the one place a credential would land on
    disk and stay there across syncs, so the rule is parse-or-refuse: accept only
    shapes whose authority actually resolves, and reject the rest rather than
    guessing where their userinfo sits.
    """
    secret = "S3CR3T-CANARY"  # noqa: S105
    repo_url = repo_url_template.format(secret=secret)

    with pytest.raises(RuntimeError, match="Refusing to write an unsafe remote URL") as exc_info:
        knowledge_git_source_module._persistable_remote_url(repo_url, "docs")

    assert secret not in str(exc_info.value)


@pytest.mark.parametrize(("repo_url_template", "expected"), _PERSISTABLE_REPO_URLS)
def test_supported_remote_url_forms_are_still_persistable(repo_url_template: str, expected: str) -> None:
    """Parse-or-refuse must not cost any supported remote form."""
    secret = "S3CR3T-CANARY"  # noqa: S105
    persisted = knowledge_git_source_module._persistable_remote_url(repo_url_template.format(secret=secret), "docs")

    assert persisted == expected
    assert secret not in persisted


@pytest.mark.parametrize("separator", _LOOKALIKE_SEPARATORS)
def test_lookalike_separator_refusal_names_no_url(separator: str) -> None:
    """The refusal, and everything chained behind it, must name no URL.

    ``urlsplit``'s message quotes the netloc, and the scheduled refresh
    subprocess logs failures with ``logger.exception``, which prints the whole
    chain -- so the refusal is raised ``from None``.
    """
    secret = "S3CR3T-CANARY"  # noqa: S105
    repo_url = f"https://user:{secret}{separator}example.com/org/repo.git"

    with pytest.raises(RuntimeError) as exc_info:
        knowledge_git_source_module._persistable_remote_url(repo_url, "docs")

    chain = "".join(traceback.format_exception(type(exc_info.value), exc_info.value, exc_info.value.__traceback__))
    assert secret not in chain


@pytest.mark.parametrize("separator", _LOOKALIKE_SEPARATORS)
def test_git_auth_env_refuses_a_lookalike_separator_without_raising(separator: str, tmp_path: Path) -> None:
    """``_git_auth_env`` must be safe on its own, not because of when it is called.

    Every caller reaches it only after ``_persistable_remote_url`` has refused
    such URLs, so this is unreachable in the current ordering. It is pinned
    because that ordering is not a property of the function, and an exception
    escaping here would carry the password into whatever logs it.
    """
    secret = "S3CR3T-CANARY"  # noqa: S105
    repo_url = f"https://user:{secret}{separator}example.com/org/repo.git"

    assert knowledge_git_source_module._git_auth_env(repo_url, None, test_runtime_paths(tmp_path)) is None


@pytest.mark.parametrize("separator", ["\uff20", "\uff1a"])
def test_redacting_an_unparseable_url_does_not_raise(separator: str) -> None:
    """The crash this PR exists to fix, at the redactor itself.

    A netloc holding an NFKC delimiter lookalike makes ``urlparse`` raise. No
    credential is required: recording *any* Git failure whose message contains
    such a URL previously raised while the failure was being recorded, and the
    knowledge API then returned 500 for as long as the error stayed persisted.
    """
    text = f"fatal: unable to access 'https://exa{separator}mple.com/repo.git': failed"

    assert "***" in redact_credentials_in_text(text)


@pytest.mark.parametrize("length", [2047, 2048, 2049, 4096])
def test_long_credential_free_urls_keep_their_diagnostic(length: int) -> None:
    """Redaction must not apply the write path's length policy to diagnostics.

    ``MAX_REDACTABLE_TOKEN_LENGTH`` bounds ``fully_unquoted``, which only the
    ``.git/config`` write gate calls. Bounding the redactor by the same constant
    replaced any URL past 2048 characters with ``***``, so a Git or Git LFS error
    carrying a long endpoint lost its diagnostic entirely -- for a URL that parses
    fine and holds no credential. The expectation here is ``origin/main``'s:
    preserved at every length.
    """
    prefix = "https://host/"
    url = prefix + "a" * (length - len(prefix))
    text = f"fatal: unable to access '{url}': failed"

    assert redact_credentials_in_text(text) == text


def test_redacting_a_non_ascii_basic_token_does_not_raise() -> None:
    """A Basic token that is not decodable must still redact, not blow up.

    ``b64decode`` raises a bare ``ValueError`` for non-ASCII input rather than
    the ``binascii.Error`` a narrower ``except`` would catch, so this pins the
    breadth of that handler: the header is still redacted, and redaction never
    replaces the Git failure it was called to sanitise.
    """
    assert redact_credentials_in_text("Authorization: Basic éééé") == "Authorization: Basic ***"


def test_redacting_basic_secrets_scrubs_a_secret_that_contains_another_whole() -> None:
    """Decoded Basic credentials are scrubbed wherever else they appear, the longest first."""
    entries = [("alice", "hunter2"), ("bob", "hunter2-extra"), ("carol", "plain")]
    headers = [
        f"Authorization: Basic {base64.b64encode(f'{user}:{password}'.encode()).decode()}" for user, password in entries
    ]
    text = "\n".join([*headers, "echo hunter2-extra and hunter2 and alice:plain and carol:plain"])

    assert redact_credentials_in_text(text).splitlines() == [
        *["Authorization: Basic ***"] * len(entries),
        "echo *** and *** and alice:*** and ***",
    ]


@pytest.mark.parametrize("lead", ["", "-", "1.", "+x9-"])
def test_redacting_a_url_finds_its_scheme_after_other_scheme_characters(lead: str) -> None:
    """A URL starts at the first letter of the scheme-character run that ends at ``://``."""
    text = f"fatal: {lead}https://user:token@example.com/repo.git failed"

    assert redact_credentials_in_text(text) == f"fatal: {lead}https://***@example.com/repo.git failed"


def test_redacting_a_long_run_of_scheme_characters_stays_within_a_cpu_budget() -> None:
    """A stored Git error is re-redacted on every knowledge API read, so a long non-URL run must not stall the loop."""
    text = "remote: " + "A" * 100_000

    with cpu_budget(0.5):
        redacted = redact_credentials_in_text(text)

    assert redacted == text


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_refresh_subprocess_returns_exact_result(tmp_path: Path) -> None:
    """A real child returns the manual refresh result without rebuilding it from metadata."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "one.md").write_text("one", encoding="utf-8")
    (docs / "two.md").write_text("two", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs}, agent_bases=["docs"])
    config.knowledge_bases["docs"].mode = "files"
    runtime_paths = runtime_paths_for(config)
    result = await knowledge_refresh_runner.refresh_knowledge_binding_in_subprocess(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
        force_reindex=True,
    )
    assert result.indexed_count == 0  # Files mode does not index vectors.
    assert result.index_published is True
    assert result.availability is KnowledgeAvailability.READY
    assert result.last_error is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_manual_refresh_returns_failed_candidate_result_and_tracks_parent_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child's report, including a failed candidate's count, is authoritative, and the base refreshes meanwhile."""
    config = _config(tmp_path, bases={"docs": tmp_path / "docs"}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    target = knowledge_registry.refresh_target_for_published_index_key(key)
    scheduler = KnowledgeRefreshScheduler()
    process = MagicMock(returncode=0)
    process.stdin.drain = AsyncMock()
    process.stdin.wait_closed = AsyncMock()
    process.wait = AsyncMock(return_value=0)

    async def spawn(*_args: object, **kwargs: object) -> MagicMock:
        assert scheduler.is_refreshing(target)
        output = kwargs["stdout"]
        assert isinstance(output, BufferedIOBase)
        output.write(
            json.dumps({"indexed_count": 7, "published": False, "error": "embedding unavailable"}).encode(),
        )
        output.flush()
        return process

    monkeypatch.setattr(knowledge_refresh_runner.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(knowledge_refresh_runner, "_terminate_refresh_subprocess", AsyncMock())
    result = await scheduler.refresh_now(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
        force_reindex=True,
    )
    assert result == knowledge_refresh_runner.KnowledgeRefreshResult(
        indexed_count=7,
        index_published=False,
        availability=KnowledgeAvailability.REFRESH_FAILED,
        last_error="embedding unavailable",
    )
    assert _refresh_status(config, runtime_paths).last_error == "embedding unavailable"
    await _wait_until(lambda: not scheduler.is_refreshing(target), "the refresh still counted as running")
    await scheduler.shutdown()


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_refresh_child")
async def test_refresh_spawn_failure_updates_persisted_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed interpreter launch is still a failed refresh, and the base stops counting as refreshing."""
    config = _config(tmp_path, bases={"docs": tmp_path / "docs"}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    scheduler = KnowledgeRefreshScheduler()
    monkeypatch.setattr(
        knowledge_refresh_runner.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=OSError("spawn refused")),
    )
    with pytest.raises(_RefreshChildFailedError, match="spawn refused"):
        await scheduler.refresh_now("docs", config=config, runtime_paths=runtime_paths)
    status = _refresh_status(config, runtime_paths)
    assert status.last_error == "spawn refused"
    assert status.consecutive_refresh_failures == 1
    assert (
        knowledge_registry.index_availability(key, None, status, publication_unreadable=False, refreshing=False)
        is KnowledgeAvailability.REFRESH_FAILED
    )
    await _wait_until(
        lambda: not scheduler.is_refreshing(knowledge_registry.refresh_target_for_published_index_key(key)),
        "the failed refresh still counted as running",
    )
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_a_queued_forced_reindex_keeps_its_force_and_its_waiter_when_a_refresh_joins_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain refresh queued behind a forced reindex keeps the reindex forced and its caller answered."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    calls: list[bool] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def _fake_refresh(_base_id: str, *, force_reindex: bool = False, **_kwargs: object) -> str:
        calls.append(force_reindex)
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
        return f"result {len(calls)}"

    monkeypatch.setattr("mindroom.knowledge.refresh_runner.refresh_knowledge_binding_in_subprocess", _fake_refresh)
    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await asyncio.wait_for(first_started.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    forced = asyncio.create_task(
        scheduler.refresh_now("docs", config=config, runtime_paths=runtime_paths, force_reindex=True),
    )
    await asyncio.sleep(0)
    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    try:
        release_first.set()
        assert await asyncio.wait_for(forced, timeout=5) == "result 2"
        assert calls == [False, True]
    finally:
        release_first.set()
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_a_shut_down_runtime_scheduler_is_replaced(tmp_path: Path) -> None:
    """Shutting the process scheduler down releases its slot, so the next runtime gets a fresh one."""
    runtime_paths = test_runtime_paths(tmp_path)
    first = runtime_refresh_scheduler(runtime_paths)
    await first.shutdown()
    second = runtime_refresh_scheduler(runtime_paths)
    assert second is not first
    await second.shutdown()


def test_a_source_change_supersedes_the_last_failure() -> None:
    """A source-change mark clears the failure it supersedes and records the reason."""
    status = marked_stale(RefreshStatus(last_error="boom", consecutive_refresh_failures=2), "source_mutated")
    assert status.last_error is None
    assert status.reason == "source_mutated"


def test_an_unreadable_publication_stays_failed_while_refreshing() -> None:
    """An unreadable publication reports a failure even while a refresh runs."""
    assert index_refresh_state(RefreshStatus(), publication_unreadable=True, refreshing=True) == "refresh_failed"


def test_a_deferred_cold_refresh_marks_the_source_empty(tmp_path: Path) -> None:
    """A content-gated refresh with nothing to publish marks the base stale with reason source_empty."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    _record_child_result(key, _RefreshChildResult(deferred=True), 0)
    assert load_refresh_status(published_index_storage_path(key)).reason == "source_empty"


@pytest.mark.asyncio
async def test_an_empty_publication_of_an_emptied_source_stays_deferred(tmp_path: Path) -> None:
    """A content-gated base whose publication indexed nothing still defers while its source is empty."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", knowledge_bases=["docs"])},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_path), require_content_before_publish=True)},
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    save_publication(
        published_index_storage_path(key),
        new_publication(
            settings=key.indexing_settings,
            collection="mindroom_knowledge_probe",
            indexed_count=0,
            source_signature="signature",
            published_revision=None,
        ),
    )
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
    assert await _should_defer_cold_empty_publication(manager, key)


def test_a_mode_change_of_an_unpublished_base_marks_nothing(tmp_path: Path) -> None:
    """A mode change of a base that never published writes no refresh status."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    semantic = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    files = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], modes={"docs": "files"})
    runtime_paths = runtime_paths_for(semantic)
    assert reconcile_knowledge_mode_transition_states(semantic, files, runtime_paths) == ()
    key = resolve_published_index_key("docs", config=semantic, runtime_paths=runtime_paths)
    assert not (published_index_storage_path(key) / "refresh_status.json").exists()
