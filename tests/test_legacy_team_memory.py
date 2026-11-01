"""The one-time merge of per-member team memory copies, on copies recorded from the release that wrote them.

`fixtures/legacy_team_memory/` holds what that release's real Mem0 2.2.1 and Chroma stores and file backend kept for
the team of `general` and `calculator` after three turns: both members extracted the first turn differently, only
`general` stored the second, and only `calculator` the third; `general` also holds one memory of its own.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import chromadb
import pytest
from chromadb.api.client import Client
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.memory import MemoryConfig, _MemoryEmbedderConfig
from mindroom.config.models import EmbedderConfig
from mindroom.constants import resolve_runtime_paths, tracking_dir
from mindroom.embeddings.errors import EmbedderRequestError
from mindroom.memory import legacy_team_memory
from mindroom.memory._file_backend import FileMemoryBackend
from mindroom.memory._mem0_backend import Mem0MemoryBackend
from mindroom.memory._policy import team_scope, team_user_id
from mindroom.memory.config import create_memory_instance
from mindroom.model_defaults import OPENAI_EMBEDDING_DIMENSIONS
from mindroom.orchestrator import main
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from mindroom.embeddings.base import EmbeddingPurpose

_FIXTURES = Path(__file__).parent / "fixtures" / "legacy_team_memory"
_TEAM = "team_calculator+general"
_RECORDED = json.loads((_FIXTURES / "mem0_member_stores.json").read_text())
_COLLECTION = "mindroom_memories_8f334045"


def _client(path: Path) -> chromadb.ClientAPI:
    return chromadb.Client(Settings(anonymized_telemetry=False, persist_directory=str(path), is_persistent=True))


def _as_stored(*rows: dict) -> dict[str, dict]:
    return {row["id"]: {"metadata": row["metadata"], "embedding": row["embedding"]} for row in rows}


def _recorded(member: str, text: str) -> dict:
    return next(row for row in _RECORDED[member][_COLLECTION] if row["metadata"]["data"] == text)


def _load_recorded(storage: Path, recorded: dict) -> None:
    """Store recorded member Mem0 records in their members' stores below ``storage``."""
    for member, collections in recorded.items():
        client = _client(storage / "agents" / member / "chroma")
        for name, rows in collections.items():
            client.get_or_create_collection(name, embedding_function=None).add(
                ids=[row["id"] for row in rows],
                embeddings=[row["embedding"] for row in rows],
                metadatas=[row["metadata"] for row in rows],
            )


def _plant(storage: Path, member: str, rows: list[tuple[str, str, str]], team: str = _TEAM) -> None:
    """Store team records ``(id, turn, text)`` in a member's store the way Mem0 does, its hash of the text included."""
    collection = _client(storage / "agents" / member / "chroma").get_or_create_collection(
        _COLLECTION,
        embedding_function=None,
    )
    collection.add(
        ids=[record_id for record_id, _turn, _text in rows],
        embeddings=[[1.0, 0.0, 0.0, 0.0] for _row in rows],
        metadatas=[
            {
                "user_id": team,
                "data": text,
                "hash": hashlib.md5(text.encode(), usedforsecurity=False).hexdigest(),
                "mindroom_replica_key": turn,
            }
            for _id, turn, text in rows
        ],
    )


@pytest.fixture
def storage(tmp_path: Path) -> Path:
    """Return a storage root holding the recorded member copies."""
    _load_recorded(tmp_path, _RECORDED)
    shutil.copytree(_FIXTURES / "agents", tmp_path / "agents", dirs_exist_ok=True)
    return tmp_path


@pytest.fixture(autouse=True)
def _recorded_embedder() -> Iterator[None]:
    """The recording embedded offline, four values per embedding, under the configured OpenAI model's name."""
    with (
        patch("mindroom.embeddings.factory.create_configured_embedder", return_value=_RecordedEmbedder()),
        patch.object(legacy_team_memory, "OPENAI_EMBEDDING_DIMENSIONS", {"text-embedding-3-small": 4}),
    ):
        yield


def _team_records(storage: Path, team: str = _TEAM) -> dict[str, dict]:
    """Return one team's records in the team store, which holds every team's."""
    return {
        record_id: record
        for record_id, record in _stored(storage / "team_memory" / "chroma").items()
        if record["metadata"]["user_id"] == team
    }


def _config(storage: Path, backend: str, *more_agents: str) -> Config:
    runtime_paths = resolve_runtime_paths(
        config_path=storage / "config.yaml",
        storage_path=storage,
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    memory = MemoryConfig(
        backend=backend,
        embedder=_MemoryEmbedderConfig(
            provider="openai",
            config=EmbedderConfig(model="text-embedding-3-small", api_key="sk-test"),
        ),
    )
    agents = {
        name: AgentConfig(display_name=name.capitalize(), role="Assistant")
        for name in ("general", "calculator", *more_agents)
    }
    teams = {"pair": TeamConfig(display_name="Pair", role="Pair", agents=["general", "calculator"])}
    return bind_runtime_paths(Config(agents=agents, teams=teams, memory=memory), runtime_paths)


def _receipt(storage: Path) -> Path:
    return tracking_dir(runtime_paths_for(_config(storage, "mem0"))) / "team_memory_merged.json"


def _delete_refused_in(member: str) -> Callable[..., None]:
    """Return a Chroma delete that a member's store refuses, as a read-only database does."""
    delete = Collection.delete

    def refusing_delete(collection: Collection, *args: object, **kwargs: object) -> None:
        if member in str(collection._client.get_settings().persist_directory):
            msg = "attempt to write a readonly database"
            raise RuntimeError(msg)
        delete(collection, *args, **kwargs)

    return refusing_delete


def _delete_stopping_after(count: int) -> Callable[..., None]:
    """Return a Chroma delete that lets ``count`` deletes through and then stops the start."""
    delete = Collection.delete
    deletions: list[Collection] = []

    def interrupted_delete(collection: Collection, *args: object, **kwargs: object) -> None:
        if len(deletions) == count:
            raise _StartStoppedError
        deletions.append(collection)
        delete(collection, *args, **kwargs)

    return interrupted_delete


def _stored(path: Path) -> dict[str, dict]:
    collection = _client(path).get_or_create_collection(_COLLECTION, embedding_function=None)
    records = collection.get(include=["embeddings", "metadatas"])
    return {
        record_id: {"metadata": metadata, "embedding": [float(value) for value in embedding]}
        for record_id, metadata, embedding in zip(
            records["ids"],
            records["metadatas"],
            records["embeddings"],
            strict=True,
        )
    }


def _expected_team_records() -> dict[str, dict]:
    # Both members extracted the first turn; calculator's extraction of it holds more distinct facts, so it is the
    # team's, and each member's lone turn stays.
    kept = [
        _recorded("calculator", "The team ships on Fridays"),
        _recorded("calculator", "PRs get pair reviews"),
        _recorded("calculator", "The team uses trunk-based development"),
        _recorded("calculator", "The team retro is on the last Thursday"),
        _recorded("general", "The team standup is at 9am"),
    ]
    return _as_stored(*kept)


class _StartStoppedError(BaseException):
    """A start that stopped partway, as a crash would, which no error handling of the merge catches."""


async def _migrate(storage: Path, *more_agents: str) -> None:
    config = _config(storage, "mem0", *more_agents)
    await legacy_team_memory.migrate_team_memory(runtime_paths_for(config), config)


def _assert_merged(storage: Path) -> None:
    team_root = storage / "team_memory" / _TEAM
    assert _team_records(storage) == _expected_team_records()
    assert _stored(storage / "agents" / "general" / "chroma") == _as_stored(_recorded("general", "General likes tea"))
    assert _stored(storage / "agents" / "calculator" / "chroma") == {}
    assert (team_root / "MEMORY.md").read_text() == (
        "# Memory\n"
        "\n"
        "- [id=m_20261029233607_ce3d8982] We ship on Fridays.\n"
        "- [id=m_20261029233607_cadca565] Retro is the last Thursday.\n"
        "- [id=m_20261029233607_a14b4cf2] Standup is at 9am.\n"
    )
    for member in ("general", "calculator"):
        assert not (storage / "agents" / member / "memory_files").exists()
    assert (storage / "agents" / "general" / "workspace" / "MEMORY.md").read_text() == (
        _FIXTURES / "agents" / "general" / "workspace" / "MEMORY.md"
    ).read_text()


@pytest.mark.asyncio
async def test_member_copies_merge_into_team_memory_once(storage: Path) -> None:
    """Every turn any member holds survives once, as one extraction, member stores keep only their own memories."""
    await _migrate(storage)

    _assert_merged(storage)
    assert _receipt(storage).exists()
    team_file = storage / "team_memory" / _TEAM / "MEMORY.md"
    merged = team_file.read_text()
    late_copy = storage / "agents" / "general" / "memory_files" / _TEAM
    late_copy.mkdir(parents=True)
    (late_copy / "MEMORY.md").write_text("# Memory\n\n- [id=m_late] Late note.\n")

    await _migrate(storage)

    assert team_file.read_text() == merged
    assert (late_copy / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_a_start_interrupted_while_removing_copies_merges_no_turn_twice(storage: Path) -> None:
    """A start that stopped after merging but before every copy left the members finishes on the next start."""
    # General's dropped extraction leaves; the start stops before calculator's kept one does.
    with patch.object(Collection, "delete", _delete_stopping_after(1)), pytest.raises(_StartStoppedError):
        await _migrate(storage)

    await _migrate(storage)

    _assert_merged(storage)


@pytest.mark.asyncio
async def test_a_start_interrupted_while_moving_records_or_copies_finishes_on_the_next(storage: Path) -> None:
    """Stopping after the team's file was published, or inside the move into the team's store, loses and doubles nothing."""
    remove_copy = legacy_team_memory._remove_copy
    removed: list[Path] = []

    def interrupted_remove(storage_root: Path, copy: Path) -> None:
        if removed:
            raise _StartStoppedError
        removed.append(copy)
        remove_copy(storage_root, copy)

    with patch.object(legacy_team_memory, "_remove_copy", interrupted_remove), pytest.raises(_StartStoppedError):
        await _migrate(storage)
    upsert = Collection.upsert

    def interrupted_upsert(collection: Collection, *, ids: list[str], **records: list) -> None:
        # Only part of the records reach the team's store before the start stops.
        upsert(collection, ids=ids[:2], **{key: values[:2] for key, values in records.items()})
        raise _StartStoppedError

    with patch.object(Collection, "upsert", interrupted_upsert), pytest.raises(_StartStoppedError):
        await _migrate(storage)

    await _migrate(storage)

    _assert_merged(storage)


@pytest.mark.asyncio
async def test_records_move_in_batches_chroma_accepts(storage: Path) -> None:
    """Chroma refuses a write larger than its batch limit, so a large team moves in several writes."""
    upsert, delete = Collection.upsert, Collection.delete
    sizes: list[int] = []

    def sized_upsert(collection: Collection, *, ids: list[str], **records: list) -> None:
        sizes.append(len(ids))
        upsert(collection, ids=ids, **records)

    def sized_delete(collection: Collection, *, ids: list[str]) -> None:
        sizes.append(len(ids))
        delete(collection, ids=ids)

    with (
        patch.object(Client, "get_max_batch_size", return_value=2),
        patch.object(Collection, "upsert", sized_upsert),
        patch.object(Collection, "delete", sized_delete),
    ):
        await _migrate(storage)

    _assert_merged(storage)
    assert max(sizes) == 2


@pytest.mark.asyncio
async def test_copies_holding_what_mindroom_did_not_write_stay_where_they_are(storage: Path) -> None:
    """A copy with a hand-written line, an extra file, or a link in its place is left in place, not merged or lost."""
    copies = storage / "agents"
    calculator_copy = copies / "calculator" / "memory_files" / _TEAM
    with (calculator_copy / "MEMORY.md").open("a") as memory:
        memory.write("A note someone wrote by hand.\n")
    general_copy = copies / "general" / "memory_files" / _TEAM
    (general_copy / "memory").mkdir()
    outside = storage / "outside"
    outside.mkdir()
    (outside / "MEMORY.md").write_text("# Memory\n\n- [id=m_planted] Planted.\n")
    (copies / "calculator" / "memory_files" / "team_calculator+planted").symlink_to(outside)

    await _migrate(storage)

    assert not (storage / "team_memory" / _TEAM / "MEMORY.md").exists()
    assert not (storage / "team_memory" / "team_calculator+planted").exists()
    assert "A note someone wrote by hand." in (calculator_copy / "MEMORY.md").read_text()
    assert (general_copy / "memory").is_dir()
    assert (outside / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_an_empty_file_copy_merges_like_any_other(storage: Path) -> None:
    """A member whose copy holds nothing yet still leaves it, and the team keeps the other member's entries."""
    copies = storage / "agents"
    (copies / "general" / "memory_files" / _TEAM / "MEMORY.md").write_bytes(b"")

    await _migrate(storage)

    calculator_copy = _FIXTURES / "agents" / "calculator" / "memory_files" / _TEAM / "MEMORY.md"
    assert (storage / "team_memory" / _TEAM / "MEMORY.md").read_text() == calculator_copy.read_text()
    for member in ("general", "calculator"):
        assert not (copies / member / "memory_files").exists()


@pytest.mark.asyncio
async def test_a_member_store_holding_a_link_is_left_unopened(storage: Path) -> None:
    """Chroma opens a store's files by path, so a store with a planted link stays as it is and only the others merge."""
    outside = storage / "outside.sqlite3"
    outside.write_bytes(b"")
    (storage / "agents" / "calculator" / "chroma" / "planted.sqlite3").symlink_to(outside)

    await _migrate(storage)

    general_turns = {"The team reviews PRs in pairs", "The team standup is at 9am", "The team ships on Fridays"}
    expected = _as_stored(
        *(row for row in _RECORDED["general"][_COLLECTION] if row["metadata"]["data"] in general_turns),
    )
    assert _team_records(storage) == expected
    calculator = {row["id"] for row in _RECORDED["calculator"][_COLLECTION]}
    assert set(_stored(storage / "agents" / "calculator" / "chroma")) == calculator
    assert outside.read_bytes() == b""


@pytest.mark.asyncio
async def test_a_member_store_chroma_cannot_read_is_left_and_the_others_merge(storage: Path) -> None:
    """A member store holding a broken database does not stop startup or the merge of the others."""
    broken = storage / "agents" / "broken" / "chroma"
    broken.mkdir(parents=True)
    (broken / "chroma.sqlite3").write_bytes(b"not a database")

    await _migrate(storage, "broken")

    _assert_merged(storage)
    assert (broken / "chroma.sqlite3").read_bytes() == b"not a database"


@pytest.mark.asyncio
async def test_a_record_whose_replica_key_is_not_text_counts_as_a_turn_of_its_own(storage: Path) -> None:
    """A planted record with a list for its replica key neither stops startup nor joins another turn."""
    planted = _client(storage / "agents" / "general" / "chroma").get_collection(_COLLECTION)
    planted.add(
        ids=["planted"],
        embeddings=[[1.0, 0.0, 0.0, 0.0]],
        metadatas=[{"user_id": _TEAM, "data": "Planted fact", "mindroom_replica_key": ["turn"]}],
    )

    await _migrate(storage)

    merged = _team_records(storage)
    assert merged.pop("planted")["metadata"]["data"] == "Planted fact"
    assert merged == _expected_team_records()


@pytest.mark.asyncio
async def test_a_record_of_another_embedding_size_stays_and_every_team_still_merges(storage: Path) -> None:
    """The team store holds every team's records, so a planted record of another size, written first, would bind it."""
    # Planted under a scope naming its holder, so it merges from that store, and sorting before every real team.
    planted = "team_aaaa+planter"
    _client(storage / "agents" / "planter" / "chroma").get_or_create_collection(
        _COLLECTION,
        embedding_function=None,
    ).add(
        ids=["planted"],
        embeddings=[[1.0, 0.0]],
        metadatas=[{"user_id": planted, "data": "Planted fact", "mindroom_replica_key": "planted-turn"}],
    )

    await _migrate(storage, "planter")

    assert _receipt(storage).exists()
    assert _team_records(storage) == _expected_team_records()
    assert _team_records(storage, planted) == {}
    assert set(_stored(storage / "agents" / "planter" / "chroma")) == {"planted"}


@pytest.mark.asyncio
async def test_records_the_team_store_holds_set_the_size_without_asking_the_embedder(storage: Path) -> None:
    """The primary-owned team store vouches for its embedder's size, so a merge it already began needs no embedder."""
    _client(storage / "team_memory" / "chroma").get_or_create_collection(_COLLECTION, embedding_function=None).add(
        ids=["held"],
        embeddings=[[0.0, 0.0, 0.0, 1.0]],
        metadatas=[{"user_id": _TEAM, "data": "Held team fact", "hash": "held"}],
    )
    unreachable = EmbedderRequestError("embedder endpoint unreachable")

    with patch("mindroom.embeddings.factory.create_configured_embedder", side_effect=unreachable):
        await _migrate(storage)

    assert set(_team_records(storage)) == {"held", *_expected_team_records()}
    assert _receipt(storage).exists()


@pytest.mark.asyncio
async def test_a_storage_root_without_mem0_copies_never_asks_the_embedder(tmp_path: Path) -> None:
    """Only Mem0 copies need an embedding size, so file copies alone merge without the embedder."""
    shutil.copytree(_FIXTURES / "agents", tmp_path / "agents")
    unreachable = EmbedderRequestError("embedder endpoint unreachable")

    with patch("mindroom.embeddings.factory.create_configured_embedder", side_effect=unreachable):
        await _migrate(tmp_path)

    assert (tmp_path / "team_memory" / _TEAM / "MEMORY.md").exists()
    assert _receipt(tmp_path).exists()


@pytest.mark.parametrize(
    ("provider", "settings", "process_env", "size"),
    [
        ("openai", {"model": "text-embedding-3-small", "dimensions": 256}, {}, 256),
        ("openai", {"model": "text-embedding-3-large"}, {}, 3072),
        ("openai", {"model": "text-embedding-3-small", "host": "https://api.openai.com/v1"}, {}, 1536),
        ("openai", {"model": "text-embedding-3-small", "host": "http://embedder.test/v1"}, {}, None),
        ("openai", {"model": "text-embedding-3-small"}, {"OPENAI_BASE_URL": "http://embedder.test/v1"}, None),
        ("openai", {"model": "text-embedding-3-small"}, {"OPENAI_BASE_URL_FILE": "endpoint"}, None),
        ("openai", {"model": "text-embedding-3-small"}, {"OPENAI_BASE_URL": ""}, 1536),
        ("openai", {"model": "text-embedding-3-small", "host": "http://embedder.test/v1", "dimensions": 256}, {}, None),
        ("sentence_transformers", {"model": "sentence-transformers/all-MiniLM-L6-v2"}, {}, None),
        ("huggingface", {"model": "sentence-transformers/all-MiniLM-L6-v2", "dimensions": 256}, {}, None),
    ],
    ids=[
        "configured dimensions",
        "known OpenAI model",
        "OpenAI's own API set as host",
        "other OpenAI-compatible host",
        "other host from OPENAI_BASE_URL",
        "other host from OPENAI_BASE_URL_FILE",
        "blank OPENAI_BASE_URL",
        "dimensions another host may ignore",
        "local model",
        "dimensions no Mem0-only embedder receives",
    ],
)
def test_only_settings_that_fix_the_embedding_size_spare_the_embedder_call(
    tmp_path: Path,
    provider: str,
    settings: dict[str, object],
    process_env: dict[str, str],
    size: int | None,
) -> None:
    """Only OpenAI's own API is sure to apply `dimensions` and to serve an OpenAI model's size; any other embedder is asked."""
    memory = MemoryConfig(embedder=_MemoryEmbedderConfig(provider=provider, config=EmbedderConfig(**settings)))
    (tmp_path / "endpoint").write_text("http://embedder.test/v1\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=process_env,
    )
    with patch.object(legacy_team_memory, "OPENAI_EMBEDDING_DIMENSIONS", OPENAI_EMBEDDING_DIMENSIONS):
        assert legacy_team_memory._configured_embedding_size(Config(memory=memory), runtime_paths) == size


@pytest.mark.asyncio
async def test_an_embedder_whose_settings_fix_its_size_is_never_asked(storage: Path) -> None:
    """A known OpenAI model's size needs no call, so an embedder outage at the first start holds no merge back."""
    unreachable = EmbedderRequestError("embedder endpoint unreachable")
    with patch("mindroom.embeddings.factory.create_configured_embedder", side_effect=unreachable):
        await _migrate(storage)

    _assert_merged(storage)
    assert _receipt(storage).exists()


@pytest.mark.asyncio
async def test_while_the_embedder_cannot_answer_the_mem0_copies_wait(storage: Path) -> None:
    """Without a size the team store, the embedder's settings, or the embedder vouches for, no Mem0 copy moves."""
    unreachable = EmbedderRequestError("embedder endpoint unreachable")
    with (
        patch("mindroom.embeddings.factory.create_configured_embedder", side_effect=unreachable),
        patch.object(legacy_team_memory, "OPENAI_EMBEDDING_DIMENSIONS", {}),
    ):
        await _migrate(storage)

    assert _team_records(storage) == {}
    for member in ("calculator", "general"):
        assert set(_stored(storage / "agents" / member / "chroma")) == {
            row["id"] for row in _RECORDED[member][_COLLECTION]
        }
    assert not _receipt(storage).exists()

    await _migrate(storage)

    _assert_merged(storage)


@pytest.mark.asyncio
async def test_a_record_chroma_rejects_as_malformed_leaves_its_team_unmerged(storage: Path) -> None:
    """Chroma validates a planted record's metadata on write, and rejecting it never stops startup."""
    malformed = ValueError("Expected metadata value to be a str, int, float, bool, SparseVector, list, or None")
    with patch.object(Collection, "upsert", side_effect=malformed):
        await _migrate(storage)

    assert _receipt(storage).exists()
    for member in ("calculator", "general"):
        assert set(_stored(storage / "agents" / member / "chroma")) == {
            row["id"] for row in _RECORDED[member][_COLLECTION]
        }


@pytest.mark.asyncio
async def test_a_storage_error_writing_a_team_store_stops_the_start_and_the_next_one_merges(storage: Path) -> None:
    """A failure that is not a rejected record leaves every copy and no receipt, so a later start merges everything."""
    storage_error = RuntimeError("disk I/O error")
    with patch.object(Collection, "upsert", side_effect=storage_error), pytest.raises(RuntimeError, match="disk I/O"):
        await _migrate(storage)

    assert not _receipt(storage).exists()
    await _migrate(storage)

    _assert_merged(storage)


@pytest.mark.asyncio
async def test_a_copy_a_member_store_refuses_to_delete_stays_and_the_merge_finishes(storage: Path) -> None:
    """A store that reads but refuses writes keeps its now unread copies instead of stopping startup."""
    with patch.object(Collection, "delete", _delete_refused_in("calculator")):
        await _migrate(storage)

    assert _team_records(storage) == _expected_team_records()
    assert set(_stored(storage / "agents" / "calculator" / "chroma")) == {
        row["id"] for row in _RECORDED["calculator"][_COLLECTION]
    }
    assert _receipt(storage).exists()


@pytest.mark.asyncio
async def test_a_start_stopped_after_a_refused_delete_keeps_the_same_extraction(storage: Path) -> None:
    """A dropped extraction that stays keeps the kept one beside it, so a start stopped before the receipt keeps it."""
    with (
        patch.object(Collection, "delete", _delete_refused_in("general")),
        patch.object(legacy_team_memory, "write_json_file_durable", side_effect=_StartStoppedError),
        pytest.raises(_StartStoppedError),
    ):
        await _migrate(storage)

    await _migrate(storage)

    _assert_merged(storage)


@pytest.mark.asyncio
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
async def test_a_file_copy_that_cannot_be_removed_stays_and_the_merge_finishes(storage: Path) -> None:
    """A merged copy whose directory refuses the removal stays behind, never read again, instead of stopping startup."""
    copy = storage / "agents" / "general" / "memory_files" / _TEAM
    copy.chmod(0o500)
    try:
        await _migrate(storage)
    finally:
        copy.chmod(0o700)

    assert (storage / "team_memory" / _TEAM / "MEMORY.md").read_text().count("[id=") == 3
    assert (copy / "MEMORY.md").exists()
    assert _receipt(storage).exists()


@pytest.mark.asyncio
async def test_without_chroma_only_file_copies_merge_and_the_rest_waits(storage: Path) -> None:
    """A start that cannot install Chroma merges file copies; a later start merges the Mem0 ones."""
    with patch.object(legacy_team_memory, "ensure_optional_deps", side_effect=ImportError("Missing dependencies")):
        await _migrate(storage)

    assert (storage / "team_memory" / _TEAM / "MEMORY.md").exists()
    assert not (storage / "team_memory" / "chroma").exists()
    assert not _receipt(storage).exists()

    await _migrate(storage)

    _assert_merged(storage)


@pytest.mark.asyncio
async def test_copies_of_another_embedder_and_entity_rows_stay(storage: Path) -> None:
    """Only the configured embedder's records merge, the ones the runtime read; Mem0's entity rows are not facts."""
    member_store = _client(storage / "agents" / "general" / "chroma")
    other = member_store.get_or_create_collection("mindroom_memories_0ther000", embedding_function=None)
    # Another embedder of the same size, so only the collection tells its records apart.
    other.add(
        ids=["older-embedder"],
        embeddings=[[0.0, 1.0, 0.0, 0.0]],
        metadatas=[{"user_id": _TEAM, "data": "Older embedder fact", "hash": "h"}],
    )
    entities = member_store.get_or_create_collection(f"{_COLLECTION}_entities", embedding_function=None)
    entities.add(
        ids=["entity"],
        embeddings=[[1.0, 0.0, 0.0, 0.0]],
        metadatas=[{"user_id": _TEAM, "data": "Fridays", "entity_type": "DATE"}],
    )

    await _migrate(storage)

    _assert_merged(storage)
    team_store = _client(storage / "team_memory" / "chroma")
    assert {collection.name for collection in team_store.list_collections()} == {_COLLECTION}
    assert other.get()["ids"] == ["older-embedder"]
    assert entities.get()["ids"] == ["entity"]


def _vector(text: str) -> list[float]:
    """Embed like the recording did, so a query finds the recorded memories."""
    digest = hashlib.sha256(text.encode()).digest()
    raw = [digest[index] - 128 for index in range(4)]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


class _RecordedEmbedder:
    sends_batches = True

    def embed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        del purpose
        return [_vector(text) for text in texts]


@pytest.mark.asyncio
async def test_both_members_find_and_edit_the_merged_team_memories(storage: Path) -> None:
    """Mem0 reads the moved records as stored, and an edit through one member is what the other member finds."""
    await _migrate(storage)
    config = _config(storage, "mem0")
    runtime_paths = runtime_paths_for(config)
    backend = Mem0MemoryBackend(
        runtime_paths=runtime_paths,
        create_memory=lambda root, config: create_memory_instance(root, config, runtime_paths),
    )
    standup = _recorded("general", "The team standup is at 9am")

    with patch("mindroom.embeddings.factory.create_configured_embedder", return_value=_RecordedEmbedder()):
        for member in ("general", "calculator"):
            outcome = await backend.search("The team standup is at 9am", member, config, limit=1)
            assert [(result["id"], result["user_id"]) for result in outcome.results] == [(standup["id"], _TEAM)]
        await backend.update(standup["id"], "The team standup is at 10am", "calculator", config)
        found = await backend.get(standup["id"], "general", config)

    assert found is not None
    assert found["memory"] == "The team standup is at 10am"


@pytest.mark.asyncio
async def test_both_file_members_find_the_merged_team_entries(storage: Path) -> None:
    """Each file member's search reads the one merged team file."""
    await _migrate(storage)
    config = _config(storage, "file")
    backend = FileMemoryBackend(runtime_paths=runtime_paths_for(config))

    for member in ("general", "calculator"):
        outcome = await backend.search("standup retro Fridays", member, config, limit=10)
        assert sorted(result["memory"] for result in outcome.results if result["user_id"] == _TEAM) == [
            "Retro is the last Thursday.",
            "Standup is at 9am.",
            "We ship on Fridays.",
        ]


@pytest.mark.asyncio
async def test_the_runtime_merges_team_memory_with_its_loaded_config_before_it_serves(tmp_path: Path) -> None:
    """The merge reads the config's Mem0 agents after old worker mounts are gone, and no reply runs before it ends."""

    class _NextStartupStepError(Exception):
        pass

    steps: list[str] = []
    startup_config = object()
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})

    def merge(_paths: object, config: object) -> None:
        steps.append("merge" if config is startup_config else "merge without the loaded config")

    with (
        patch("mindroom.orchestrator.setup_logging"),
        patch(
            "mindroom.orchestration.storage_preparation.retire_state_root_worker_mounts",
            side_effect=lambda _paths: steps.append("retire"),
        ),
        patch("mindroom.orchestration.storage_preparation.migrate_conversation_storage"),
        patch("mindroom.orchestrator.load_startup_config", side_effect=lambda *_a, **_k: startup_config),
        patch("mindroom.orchestrator.migrate_team_memory", side_effect=merge),
        patch("mindroom.orchestrator._MultiAgentOrchestrator", side_effect=_NextStartupStepError),
        pytest.raises(_NextStartupStepError),
    ):
        await main(log_level="INFO", runtime_paths=runtime_paths, api=False)

    assert steps == ["retire", "merge"]


@pytest.mark.asyncio
async def test_only_the_mem0_stores_the_runtime_opens_merge(storage: Path) -> None:
    """A store of an agent no longer configured, private, or on file memory was never read for team memory."""
    for name in ("removed", "filer", "secret"):
        _plant(storage, name, [(f"{name}-copy", f"{name}-turn", f"{name} fact")])
    config = _config(storage, "mem0", "filer", "secret")
    config.agents["filer"].memory_backend = "file"
    config.agents["secret"].private = AgentPrivateConfig(per="user")
    opened: list[Path] = []
    client = legacy_team_memory._chroma_client

    def recording_client(path: Path) -> object:
        opened.append(path.relative_to(storage))
        return client(path)

    with patch.object(legacy_team_memory, "_chroma_client", side_effect=recording_client):
        await legacy_team_memory.migrate_team_memory(runtime_paths_for(config), config)

    assert {path.parts[1] for path in opened if path.parts[0] == "agents"} == {"calculator", "general"}
    _assert_merged(storage)


@pytest.mark.asyncio
async def test_a_team_copy_held_by_an_agent_outside_the_team_stays_where_it_is(storage: Path) -> None:
    """The earlier runtime read a team's copies only from its members, so another agent's copy is not merged."""
    _plant(storage, "outsider", [("o1", "outsider-turn", "Outsider fact")])
    copy = storage / "agents" / "outsider" / "memory_files" / _TEAM
    copy.mkdir(parents=True)
    (copy / "MEMORY.md").write_text("# Memory\n\n- [id=m_outsider] Outsider note.\n")

    await _migrate(storage, "outsider")

    _assert_merged(storage)
    assert set(_stored(storage / "agents" / "outsider" / "chroma")) == {"o1"}
    assert (copy / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_file_copies_of_members_with_edge_underscores_merge_where_the_team_reads(tmp_path: Path) -> None:
    """Agent directories lose leading and trailing underscores and copy directories a trailing one, as on main."""
    team = team_user_id(["_alpha", "beta_"])
    for agent_dir, entry in (("alpha", "m_alpha"), ("beta", "m_beta")):
        copy = tmp_path / "agents" / agent_dir / "memory_files" / "team__alpha+beta"
        copy.mkdir(parents=True)
        (copy / "MEMORY.md").write_text(f"# Memory\n\n- [id={entry}] Note from {agent_dir}.\n")
    config = _config(tmp_path, "file", "_alpha", "beta_")

    await legacy_team_memory.migrate_team_memory(runtime_paths_for(config), config)

    merged = (team_scope(team, runtime_paths_for(config)).root / "MEMORY.md").read_text()
    assert "[id=m_alpha]" in merged
    assert "[id=m_beta]" in merged
    assert not (tmp_path / "agents" / "alpha" / "memory_files").exists()
    assert not (tmp_path / "agents" / "beta" / "memory_files").exists()


@pytest.mark.asyncio
async def test_two_scopes_sharing_a_directory_each_keep_a_fact_they_share(tmp_path: Path) -> None:
    """Scopes differing only in a trailing underscore share a store, and Mem0 keeps one record per fact per scope."""
    _plant(tmp_path, "general", [("g1", "t1", "Fact A")])
    _plant(tmp_path, "general", [("g2", "t2", "Fact A")], team="team_calculator+general_")

    await _migrate(tmp_path)

    merged = _stored(tmp_path / "team_memory" / "chroma")
    assert sorted(record["metadata"]["user_id"] for record in merged.values()) == [_TEAM, "team_calculator+general_"]


@pytest.mark.asyncio
async def test_a_turn_keeps_the_copy_main_s_edit_bug_left_whole(tmp_path: Path) -> None:
    """Recorded on main after deleting one fact and editing another through `general`, which sorts second.

    Main's edit bug deleted calculator's whole first turn and rewrote both of its second-turn records to the edited
    text, so each turn keeps general's extraction, which holds the most distinct facts, and no fact is lost or doubled.
    """
    recorded = json.loads((_FIXTURES / "mem0_member_stores_after_edits.json").read_text())
    _load_recorded(tmp_path, recorded)

    await _migrate(tmp_path)

    merged = _team_records(tmp_path)
    assert sorted(record["metadata"]["data"] for record in merged.values()) == [
        "Team fact five",
        "Team fact four, revised",
        "Team fact three",
        "Team fact two",
    ]
    assert set(merged) == {row["id"] for row in recorded["general"][_COLLECTION]}


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True], ids=["one start", "interrupted"])
async def test_a_fact_two_kept_extractions_share_is_stored_once(tmp_path: Path, interrupted: bool) -> None:
    """Mem0 deduplicates facts per member store, so the turns kept from different members can repeat one.

    Interrupted after calculator's kept turn left its store, the next start keeps only general's turn, whose fact A
    the team's store already holds.
    """
    _plant(tmp_path, "calculator", [("c1", "t1", "Fact A"), ("c2", "t1", "Fact B"), ("c3", "t2", "Fact C")])
    _plant(tmp_path, "general", [("g1", "t1", "Fact B"), ("g2", "t2", "Fact A"), ("g3", "t2", "Fact C")])
    if interrupted:
        # Each member's dropped turn goes first, then calculator's kept one; general's kept one would be next.
        with patch.object(Collection, "delete", _delete_stopping_after(3)), pytest.raises(_StartStoppedError):
            await _migrate(tmp_path)

    await _migrate(tmp_path)

    merged = _team_records(tmp_path)
    assert sorted(record["metadata"]["data"] for record in merged.values()) == ["Fact A", "Fact B", "Fact C"]


@pytest.mark.asyncio
async def test_a_turn_whose_extractions_tie_keeps_the_first_member_s_by_name(tmp_path: Path) -> None:
    """Equal extractions of a turn go to the member that sorts first, the same choice on every start."""
    _plant(tmp_path, "general", [("g1", "t1", "Fact Y")])
    _plant(tmp_path, "calculator", [("c1", "t1", "Fact X")])

    await _migrate(tmp_path)

    merged = _team_records(tmp_path)
    assert {record_id: record["metadata"]["data"] for record_id, record in merged.items()} == {"c1": "Fact X"}
    assert _stored(tmp_path / "agents" / "general" / "chroma") == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True], ids=["one start", "interrupted"])
async def test_records_planted_under_one_id_in_two_members_stores_never_stop_the_start(
    tmp_path: Path,
    interrupted: bool,
) -> None:
    """Chroma refuses two records of one ID in a write, so the team's store keeps the first, on every start."""
    _plant(tmp_path, "calculator", [("planted", "t2", "Fact B")])
    _plant(tmp_path, "general", [("planted", "t1", "Fact A")])
    if interrupted:
        with patch.object(Collection, "delete", _delete_stopping_after(0)), pytest.raises(_StartStoppedError):
            await _migrate(tmp_path)

    await _migrate(tmp_path)

    merged = _team_records(tmp_path)
    assert {record_id: record["metadata"]["data"] for record_id, record in merged.items()} == {"planted": "Fact B"}
    assert _receipt(tmp_path).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True], ids=["one start", "interrupted"])
async def test_a_record_planted_under_another_teams_id_never_costs_that_team_its_fact(
    storage: Path,
    interrupted: bool,
) -> None:
    """A planted record that takes a team record's ID first keeps that record out of team memory, never off the disk."""
    victim = _recorded("calculator", "The team ships on Fridays")
    planted = "team_aaaa+planter"
    # The planted record copies the victim's text too, so only its scope tells the two apart.
    _plant(storage, "planter", [(victim["id"], "planted-turn", victim["metadata"]["data"])], team=planted)
    if interrupted:
        with patch.object(Collection, "delete", _delete_stopping_after(0)), pytest.raises(_StartStoppedError):
            await _migrate(storage, "planter")

    await _migrate(storage, "planter")

    assert _team_records(storage, planted).keys() == {victim["id"]}
    assert _team_records(storage) == {
        record_id: record for record_id, record in _expected_team_records().items() if record_id != victim["id"]
    }
    left = _stored(storage / "agents" / "calculator" / "chroma")
    assert left == _as_stored(victim)
    assert _receipt(storage).exists()


@pytest.mark.asyncio
async def test_a_record_a_teammate_planted_under_ones_id_never_costs_the_team_that_fact(storage: Path) -> None:
    """A member's worker can plant a record under a teammate's record ID; the teammate's copy stays on disk."""
    standup = _recorded("general", "The team standup is at 9am")
    _plant(storage, "calculator", [(standup["id"], "planted-turn", "Planted fact")])

    await _migrate(storage)

    assert _team_records(storage)[standup["id"]]["metadata"]["data"] == "Planted fact"
    assert standup["id"] in _stored(storage / "agents" / "general" / "chroma")


@pytest.mark.asyncio
async def test_a_tied_copy_main_s_edit_bug_rewrote_is_stored_once(tmp_path: Path) -> None:
    """An edit through general rewrote both of calculator's records of the turn to its one edited fact."""
    _plant(tmp_path, "calculator", [("c1", "t1", "Fact A, edited"), ("c2", "t1", "Fact A, edited")])
    _plant(tmp_path, "general", [("g1", "t1", "Fact A, edited")])

    await _migrate(tmp_path)

    merged = _team_records(tmp_path)
    assert [record["metadata"]["data"] for record in merged.values()] == ["Fact A, edited"]
