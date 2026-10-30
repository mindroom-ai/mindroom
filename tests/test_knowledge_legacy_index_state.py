"""The startup conversion of knowledge index state that earlier releases kept outside each collection.

Each fixture under ``tests/fixtures/knowledge_index_state`` is what the release before the conversion left on disk,
recorded by running that release's refreshes; a test moves it under its own storage root, converts it, and refreshes.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import mindroom.knowledge.legacy_index_state as legacy_module
import mindroom.knowledge.manager as knowledge_manager_module
import mindroom.knowledge.registry as knowledge_registry
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.embeddings.errors import EmbedderRequestError
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.index_metadata import load_publication, load_refresh_status, publication_path
from mindroom.knowledge.legacy_index_state import migrate_knowledge_index_state
from mindroom.knowledge.refresh_runner import _refresh_knowledge_binding as refresh_knowledge_binding
from mindroom.knowledge.registry import get_published_index, published_index_storage_path, resolve_published_index_key
from mindroom.knowledge.vector_store import KnowledgeCollection, list_collection_names
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.constants import RuntimePaths

_FIXTURES = Path(__file__).parent / "fixtures" / "knowledge_index_state"
_LEGACY_FILES = (
    "indexing_settings.json",
    "candidate_index.json",
    "candidate_index.jsonl",
    "published_index_seal.json",
    "retired_index_seal.json",
)


class _RecordingEmbedder:
    sends_batches = False

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.fail = False

    def embed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        del purpose
        if self.fail:
            detail = "embedder request failed (HTTP 500)"
            raise EmbedderRequestError(detail)
        self.texts.extend(texts)
        return [[float(len(text)), 1.0] for text in texts]

    async def aembed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        return self.embed(texts, purpose=purpose)


@pytest.fixture
def embedder(monkeypatch: pytest.MonkeyPatch) -> _RecordingEmbedder:
    """Index and search with one recording embedder."""
    recording = _RecordingEmbedder()
    monkeypatch.setattr(knowledge_manager_module, "create_configured_embedder", lambda *_a, **_k: recording)
    monkeypatch.setattr(knowledge_registry, "create_configured_embedder", lambda *_a, **_k: recording)
    knowledge_registry._published_indexes.clear()
    return recording


def _install(name: str, tmp_path: Path) -> tuple[Config, RuntimePaths, Path]:
    """Recreate one recorded base under this test's storage root, renamed for its paths."""
    fixture = _FIXTURES / name
    manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
    docs = tmp_path / "docs"
    docs.mkdir()
    for relative_path, body in manifest["docs"].items():
        (docs / relative_path).write_text(body, encoding="utf-8")
    legacy_settings = json.loads((fixture / "storage" / "indexing_settings.json").read_text(encoding="utf-8"))
    config = bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", knowledge_bases=["docs"])},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs), mode=legacy_settings["settings"]["mode"])},
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths, create=True)
    storage = published_index_storage_path(key)
    storage.mkdir(parents=True, exist_ok=True)
    renames = {
        manifest["storage_key"]: storage.name,
        manifest["knowledge_path"]: key.knowledge_path,
        manifest["storage_root"]: key.storage_root,
    }
    for recorded in sorted((fixture / "storage").iterdir()):
        target_name = recorded.name.replace(manifest["storage_key"], storage.name)
        if recorded.suffix in {".json", ".jsonl"}:
            text = recorded.read_text(encoding="utf-8")
            for old, new in renames.items():
                text = text.replace(old, new)
            (storage / target_name).write_text(text, encoding="utf-8")
        else:
            shutil.copyfile(recorded, storage / target_name)
    return config, runtime_paths, storage


def _schema_version(collection: KnowledgeCollection) -> int:
    with closing(sqlite3.connect(collection.file)) as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


def _row_paths(collection: KnowledgeCollection) -> set[str]:
    with closing(sqlite3.connect(collection.file)) as connection:
        return {path for (path,) in connection.execute("SELECT DISTINCT source_path FROM chunks")}


@pytest.mark.asyncio
async def test_a_published_base_with_its_retired_collection_converts_and_refreshes_without_embedding(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """The publication, the collection it retired, and their claims survive; nothing is embedded again."""
    config, runtime_paths, storage = _install("two_publications", tmp_path)
    legacy_published = json.loads((storage / "published_index_seal.json").read_text(encoding="utf-8"))["collection"]
    legacy_retired = json.loads((storage / "retired_index_seal.json").read_text(encoding="utf-8"))["collection"]

    await migrate_knowledge_index_state(runtime_paths)

    assert not any((storage / name).exists() for name in _LEGACY_FILES)
    publication = load_publication(storage)
    assert publication is not None
    assert publication.collection == legacy_published
    assert publication.retired == legacy_retired
    for name in (legacy_published, legacy_retired):
        collection = KnowledgeCollection(path=storage, name=name)
        assert _schema_version(collection) == 2
        assert collection.meta().stage == "complete"
        assert set(collection.files()) == _row_paths(collection)
    resolution = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert resolution.availability is KnowledgeAvailability.READY
    assert resolution.reader is not None
    assert [hit.content for hit in await resolution.reader.asearch("bravo", limit=5) if "bravo" in hit.content] == [
        "bravo two",
    ]
    embedder.texts.clear()

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published
    assert embedder.texts == []

    (tmp_path / "docs" / "a.md").write_text("alpha two", encoding="utf-8")
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published
    # The retired collection is taken over, so it also catches up on the edit its successor published.
    assert sorted(embedder.texts) == ["alpha two", "bravo two"]
    publication = load_publication(storage)
    assert publication is not None
    assert (publication.collection, publication.retired) == (legacy_retired, legacy_published)


@pytest.mark.asyncio
async def test_a_publication_of_a_resumed_candidate_converts_with_its_claims(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A publication the old code could not seal still gets its claims, so an unchanged refresh embeds nothing."""
    config, runtime_paths, storage = _install("resumed_publication", tmp_path)

    await migrate_knowledge_index_state(runtime_paths)

    publication = load_publication(storage)
    assert publication is not None
    assert publication.collection is not None
    assert publication.retired is None
    assert set(KnowledgeCollection(path=storage, name=publication.collection).files()) == {"a.md", "b.md", "c.md"}
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert result.published
    assert embedder.texts == []


@pytest.mark.asyncio
async def test_an_interrupted_build_with_a_journal_resumes_where_it_stopped(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A candidate whose journal held its last claims resumes from its rows and embeds only the failed file."""
    config, runtime_paths, storage = _install("mid_build_journal", tmp_path)
    candidate = json.loads((storage / "candidate_index.json").read_text(encoding="utf-8"))["collection"]

    await migrate_knowledge_index_state(runtime_paths)

    collection = KnowledgeCollection(path=storage, name=candidate)
    assert collection.meta().stage == "building"
    assert set(collection.files()) == {"a.md", "b.md"}
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert result.published
    assert embedder.texts == ["delta one"]
    publication = load_publication(storage)
    assert publication is not None
    assert publication.collection == candidate


@pytest.mark.asyncio
async def test_an_interrupted_bulk_copy_is_discarded_and_the_publication_kept(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Rows a copy wrote before recording any claim may cut a file short, so that candidate is deleted."""
    config, runtime_paths, storage = _install("interrupted_copy", tmp_path)
    candidate = json.loads((storage / "candidate_index.json").read_text(encoding="utf-8"))["collection"]

    await migrate_knowledge_index_state(runtime_paths)

    assert candidate not in list_collection_names(storage)
    publication = load_publication(storage)
    assert publication is not None
    assert publication.collection in list_collection_names(storage)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert result.published
    assert embedder.texts == []


@pytest.mark.asyncio
async def test_a_failed_first_build_keeps_its_error_and_resumes_its_candidate(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A base that never published reports its last failure and resumes its candidate's finished files."""
    config, runtime_paths, storage = _install("cold_failure", tmp_path)

    await migrate_knowledge_index_state(runtime_paths)

    assert not publication_path(storage).exists()
    status = load_refresh_status(storage)
    assert status.last_error is not None
    assert status.consecutive_refresh_failures == 1
    assert get_published_index("docs", config=config, runtime_paths=runtime_paths).availability is (
        KnowledgeAvailability.REFRESH_FAILED
    )
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert result.published
    assert embedder.texts == ["bravo one"]


@pytest.mark.asyncio
async def test_a_stale_mark_survives_the_conversion(tmp_path: Path, embedder: _RecordingEmbedder) -> None:
    """A source change the old runtime marked but never published still reads as stale."""
    del embedder
    config, runtime_paths, storage = _install("stale_marked", tmp_path)

    await migrate_knowledge_index_state(runtime_paths)

    assert load_refresh_status(storage).reason == "filesystem_watch"
    assert get_published_index("docs", config=config, runtime_paths=runtime_paths).availability is (
        KnowledgeAvailability.STALE
    )


@pytest.mark.asyncio
async def test_a_files_mode_publication_converts_without_a_collection(tmp_path: Path) -> None:
    """A files-mode base keeps its published source listing."""
    _config, runtime_paths, storage = _install("files_mode", tmp_path)

    await migrate_knowledge_index_state(runtime_paths)

    publication = load_publication(storage)
    assert publication is not None
    assert publication.collection is None
    assert publication.settings.mode == "files"


@pytest.mark.asyncio
async def test_an_unreadable_legacy_state_stays_unreadable_and_protects_every_collection(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Without a readable state nothing proves which collection is live, so none is deleted until a replacement publishes."""
    config, runtime_paths, storage = _install("two_publications", tmp_path)
    (storage / "indexing_settings.json").write_text("{not json", encoding="utf-8")
    collections = list_collection_names(storage)

    await migrate_knowledge_index_state(runtime_paths)

    assert publication_path(storage).exists()
    assert load_publication(storage) is None
    assert get_published_index("docs", config=config, runtime_paths=runtime_paths).availability is (
        KnowledgeAvailability.REFRESH_FAILED
    )
    embedder.fail = True
    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).error is not None
    assert set(collections) <= set(list_collection_names(storage))

    embedder.fail = False
    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published
    publication = load_publication(storage)
    assert publication is not None
    assert list_collection_names(storage) == (publication.collection,)


@pytest.mark.asyncio
async def test_a_second_start_changes_nothing_and_an_interrupted_conversion_finishes(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """The conversion deletes the legacy state last, so a rerun after a crash converts the base whole."""
    del embedder
    _config, runtime_paths, storage = _install("two_publications", tmp_path)
    (storage / "publication.json").write_text("{}", encoding="utf-8")

    await migrate_knowledge_index_state(runtime_paths)
    converted = {path.name: path.read_bytes() for path in storage.iterdir() if path.suffix == ".json"}
    await migrate_knowledge_index_state(runtime_paths)

    assert load_publication(storage) is not None
    assert {path.name: path.read_bytes() for path in storage.iterdir() if path.suffix == ".json"} == converted


async def _convert_failing_every_collection_upgrade(
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the conversion with each collection upgrade failing as a full disk fails it, after the state files moved."""

    def _fail(*_args: object, **_kwargs: object) -> None:
        msg = "database or disk is full"
        raise sqlite3.OperationalError(msg)

    with monkeypatch.context() as patch:
        patch.setattr(legacy_module, "_upgrade_named_collections", _fail)
        await migrate_knowledge_index_state(runtime_paths)


@pytest.mark.asyncio
async def test_a_failed_conversion_is_not_retried_over_a_later_publication(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversion that fails drops the legacy state, so no later start restores its publication over a newer one."""
    del embedder
    config, runtime_paths, storage = _install("two_publications", tmp_path)
    await _convert_failing_every_collection_upgrade(runtime_paths, monkeypatch)
    assert not any((storage / name).exists() for name in _LEGACY_FILES)

    # Two publications, so cleanup deletes the collection the legacy state published.
    for index in range(2):
        (tmp_path / "docs" / "added.md").write_text(f"added {index}", encoding="utf-8")
        assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published
    published = load_publication(storage)
    assert published is not None
    assert published.collection is not None

    await migrate_knowledge_index_state(runtime_paths)

    assert load_publication(storage) == published
    assert KnowledgeCollection(path=storage, name=published.collection).file.exists()


@pytest.mark.asyncio
async def test_a_failed_conversion_leaves_the_base_unpublished_until_a_refresh_rebuilds_it(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A base whose collections did not convert is left unpublished, and its refresh publishes a readable collection."""
    config, runtime_paths, storage = _install("two_publications", tmp_path)
    await _convert_failing_every_collection_upgrade(runtime_paths, monkeypatch)
    assert load_publication(storage) is None

    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published

    publication = load_publication(storage)
    assert publication is not None
    assert publication.collection is not None
    assert _schema_version(KnowledgeCollection(path=storage, name=publication.collection)) == 2
    resolution = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert resolution.reader is not None
    assert await resolution.reader.asearch("alpha", limit=1)
    assert embedder.texts


@pytest.mark.asyncio
async def test_a_linked_base_directory_is_left_alone(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A base directory replaced by a link is not converted through it."""
    del embedder
    _config, runtime_paths, storage = _install("two_publications", tmp_path)
    real = tmp_path / "elsewhere"
    storage.rename(real)
    storage.symlink_to(real, target_is_directory=True)
    await migrate_knowledge_index_state(runtime_paths)
    assert (real / "indexing_settings.json").exists()
    assert not publication_path(real).exists()


@pytest.mark.asyncio
async def test_a_collection_an_unfinished_record_names_is_resumed(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A collection a readable but unfinished legacy record names is upgraded as a build and resumed without embedding."""
    config, runtime_paths, storage = _install("resumed_publication", tmp_path)
    state_file = storage / "indexing_settings.json"
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    payload["status"] = "indexing"
    state_file.write_text(json.dumps(payload), encoding="utf-8")
    await migrate_knowledge_index_state(runtime_paths)
    assert load_publication(storage) is None
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert result.published
    assert embedder.texts == []
