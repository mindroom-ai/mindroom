"""The one-time merge of per-member team memory copies, on copies recorded from the release that wrote them.

`fixtures/legacy_team_memory/` holds what that release's real Mem0 2.2.1 and Chroma stores and file backend kept for
the team of `general` and `calculator` after three turns: both members extracted the first turn differently, only
`general` stored the second, and only `calculator` the third; `general` also holds one memory of its own.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import chromadb
import pytest
from chromadb.api.client import Client
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.memory import MemoryConfig, _MemoryEmbedderConfig
from mindroom.config.models import EmbedderConfig
from mindroom.constants import resolve_runtime_paths, tracking_dir
from mindroom.memory import legacy_team_memory
from mindroom.memory._file_backend import FileMemoryBackend
from mindroom.memory._mem0_backend import Mem0MemoryBackend
from mindroom.memory.config import create_memory_instance
from mindroom.orchestrator import main
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from collections.abc import Sequence

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


@pytest.fixture
def storage(tmp_path: Path) -> Path:
    """Return a storage root holding the recorded member copies."""
    for member, collections in _RECORDED.items():
        client = _client(tmp_path / "agents" / member / "chroma")
        for name, rows in collections.items():
            client.get_or_create_collection(name, embedding_function=None).add(
                ids=[row["id"] for row in rows],
                embeddings=[row["embedding"] for row in rows],
                metadatas=[row["metadata"] for row in rows],
            )
    shutil.copytree(_FIXTURES / "agents", tmp_path / "agents", dirs_exist_ok=True)
    return tmp_path


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
    # Both members extracted the first turn; calculator sorts first, so its extraction of that turn is the team's, and
    # each member's lone turn stays.
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
    assert _stored(team_root / "chroma") == _expected_team_records()
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
async def test_member_copies_merge_into_one_store_per_team_once(storage: Path) -> None:
    """Every turn any member holds survives once, as one extraction, member stores keep only their own memories."""
    await _migrate(storage)

    _assert_merged(storage)
    receipt = tracking_dir(runtime_paths_for(_config(storage, "mem0"))) / "team_memory_merged.json"
    assert receipt.exists()
    with patch.object(legacy_team_memory, "_merge_file_copies") as merge:
        await _migrate(storage)
    merge.assert_not_called()


@pytest.mark.asyncio
async def test_a_start_interrupted_while_removing_copies_merges_no_turn_twice(storage: Path) -> None:
    """A start that stopped after merging but before every copy left the members finishes on the next start."""
    delete = Collection.delete
    deletions: list[object] = []

    def interrupted_delete(collection: Collection, *args: object, **kwargs: object) -> None:
        # The first member's copies leave; the start stops before the second member's do.
        if deletions:
            raise _StartStoppedError
        deletions.append(collection)
        delete(collection, *args, **kwargs)

    with patch.object(Collection, "delete", interrupted_delete), pytest.raises(_StartStoppedError):
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
    assert _stored(storage / "team_memory" / _TEAM / "chroma") == expected
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

    merged = _stored(storage / "team_memory" / _TEAM / "chroma")
    assert merged.pop("planted")["metadata"]["data"] == "Planted fact"
    assert merged == _expected_team_records()


@pytest.mark.asyncio
async def test_a_team_whose_store_refuses_its_records_keeps_its_copies_and_is_not_merged_again(storage: Path) -> None:
    """A planted record of another dimension makes the team's store refuse the batch; the copies stay, unmerged."""
    planted = _client(storage / "agents" / "planter" / "chroma").get_or_create_collection(
        _COLLECTION,
        embedding_function=None,
    )
    planted.add(
        ids=["planted"],
        embeddings=[[1.0, 0.0]],
        metadatas=[{"user_id": _TEAM, "data": "Planted fact", "mindroom_replica_key": "planted-turn"}],
    )
    runtime_paths = runtime_paths_for(_config(storage, "mem0"))

    await _migrate(storage, "planter")

    # No later start writes them again, so none undoes an edit made to a merged fact since.
    assert (tracking_dir(runtime_paths) / "team_memory_merged.json").exists()
    for member in ("calculator", "general"):
        assert set(_stored(storage / "agents" / member / "chroma")) == {
            row["id"] for row in _RECORDED[member][_COLLECTION]
        }


@pytest.mark.asyncio
async def test_a_record_chroma_rejects_as_malformed_leaves_its_team_unmerged(storage: Path) -> None:
    """Chroma validates a planted record's metadata on write, and rejecting it never stops startup."""
    malformed = ValueError("Expected metadata value to be a str, int, float, bool, SparseVector, list, or None")
    with patch.object(Collection, "upsert", side_effect=malformed):
        await _migrate(storage)

    assert (tracking_dir(runtime_paths_for(_config(storage, "mem0"))) / "team_memory_merged.json").exists()
    for member in ("calculator", "general"):
        assert set(_stored(storage / "agents" / member / "chroma")) == {
            row["id"] for row in _RECORDED[member][_COLLECTION]
        }


@pytest.mark.asyncio
async def test_a_storage_error_writing_a_team_store_stops_the_start_and_the_next_one_merges(storage: Path) -> None:
    """A failure that is not a rejected record leaves every copy and no receipt, so a later start merges everything."""
    upsert = Collection.upsert
    upserts: list[object] = []

    def failing_upsert(collection: Collection, *args: object, **kwargs: object) -> None:
        if not upserts:
            upserts.append(collection)
            msg = "disk I/O error"
            raise RuntimeError(msg)
        upsert(collection, *args, **kwargs)

    with patch.object(Collection, "upsert", failing_upsert), pytest.raises(RuntimeError, match="disk I/O error"):
        await _migrate(storage)

    assert not (tracking_dir(runtime_paths_for(_config(storage, "mem0"))) / "team_memory_merged.json").exists()
    await _migrate(storage)

    _assert_merged(storage)


@pytest.mark.asyncio
async def test_a_copy_a_member_store_refuses_to_delete_stays_and_the_merge_finishes(storage: Path) -> None:
    """A store that reads but refuses writes keeps its now unread copies instead of stopping startup."""
    delete = Collection.delete

    def refusing_delete(collection: Collection, *args: object, **kwargs: object) -> None:
        if "calculator" in str(collection._client.get_settings().persist_directory):
            msg = "attempt to write a readonly database"
            raise RuntimeError(msg)
        delete(collection, *args, **kwargs)

    with patch.object(Collection, "delete", refusing_delete):
        await _migrate(storage)

    assert _stored(storage / "team_memory" / _TEAM / "chroma") == _expected_team_records()
    assert set(_stored(storage / "agents" / "calculator" / "chroma")) == {
        row["id"] for row in _RECORDED["calculator"][_COLLECTION]
    }
    assert (tracking_dir(runtime_paths_for(_config(storage, "mem0"))) / "team_memory_merged.json").exists()


@pytest.mark.asyncio
async def test_a_start_stopped_after_a_refused_delete_keeps_the_same_extraction(storage: Path) -> None:
    """A dropped extraction that stays keeps the kept one beside it, so a start stopped before the receipt keeps it."""
    delete = Collection.delete

    def refusing_delete(collection: Collection, *args: object, **kwargs: object) -> None:
        if "general" in str(collection._client.get_settings().persist_directory):
            msg = "attempt to write a readonly database"
            raise RuntimeError(msg)
        delete(collection, *args, **kwargs)

    with (
        patch.object(Collection, "delete", refusing_delete),
        patch.object(legacy_team_memory, "write_json_file_durable", side_effect=_StartStoppedError),
        pytest.raises(_StartStoppedError),
    ):
        await _migrate(storage)

    await _migrate(storage)

    _assert_merged(storage)


@pytest.mark.asyncio
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
    assert (tracking_dir(runtime_paths_for(_config(storage, "mem0"))) / "team_memory_merged.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["chroma", "config"])
async def test_without_chroma_or_config_only_file_copies_merge_and_the_rest_waits(storage: Path, missing: str) -> None:
    """A start that cannot install Chroma or load its config merges file copies; a later start merges the Mem0 ones."""
    runtime_paths = runtime_paths_for(_config(storage, "mem0"))
    if missing == "chroma":
        with patch.object(legacy_team_memory, "ensure_optional_deps", side_effect=ImportError("Missing dependencies")):
            await _migrate(storage)
    else:
        await legacy_team_memory.migrate_team_memory(runtime_paths, None)

    assert (storage / "team_memory" / _TEAM / "MEMORY.md").exists()
    assert not (storage / "team_memory" / _TEAM / "chroma").exists()
    assert not (tracking_dir(runtime_paths) / "team_memory_merged.json").exists()

    await _migrate(storage)

    _assert_merged(storage)


@pytest.mark.asyncio
async def test_each_embedder_collection_merges_on_its_own_and_entity_rows_stay(storage: Path) -> None:
    """Team records of another embedder's collection move to that collection; Mem0's entity rows are not facts."""
    member_store = _client(storage / "agents" / "general" / "chroma")
    other = member_store.get_or_create_collection("mindroom_memories_0ther000", embedding_function=None)
    other.add(
        ids=["older-embedder"],
        embeddings=[[0.0, 1.0]],
        metadatas=[{"user_id": _TEAM, "data": "Older embedder fact", "hash": "h"}],
    )
    entities = member_store.get_or_create_collection(f"{_COLLECTION}_entities", embedding_function=None)
    entities.add(
        ids=["entity"],
        embeddings=[[1.0, 0.0, 0.0, 0.0]],
        metadatas=[{"user_id": _TEAM, "data": "Fridays", "entity_type": "DATE"}],
    )

    await _migrate(storage)

    team_store = _client(storage / "team_memory" / _TEAM / "chroma")
    moved = team_store.get_collection("mindroom_memories_0ther000").get()
    assert moved["ids"] == ["older-embedder"]
    assert f"{_COLLECTION}_entities" not in {collection.name for collection in team_store.list_collections()}
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
        patch("mindroom.orchestrator.sync_env_to_credentials"),
        patch(
            "mindroom.orchestrator.retire_state_root_worker_mounts",
            side_effect=lambda _paths: steps.append("retire"),
        ),
        patch("mindroom.orchestrator.migrate_conversation_storage"),
        patch("mindroom.orchestrator.load_startup_config", side_effect=lambda *_a, **_k: startup_config),
        patch("mindroom.orchestrator.migrate_team_memory", side_effect=merge),
        patch("mindroom.orchestrator._MultiAgentOrchestrator", side_effect=_NextStartupStepError),
        pytest.raises(_NextStartupStepError),
    ):
        await main(log_level="INFO", runtime_paths=runtime_paths, api=False)

    assert steps == ["retire", "merge"]


@pytest.mark.asyncio
async def test_only_the_mem0_stores_the_runtime_opens_merge(storage: Path) -> None:
    """A store of an agent no longer configured, or on file memory, was never read for team memory and stays shut."""
    for name in ("removed", "filer"):
        store = _client(storage / "agents" / name / "chroma").get_or_create_collection(
            _COLLECTION,
            embedding_function=None,
        )
        store.add(
            ids=[f"{name}-copy"],
            embeddings=[[1.0, 0.0, 0.0, 0.0]],
            metadatas=[{"user_id": _TEAM, "data": f"{name} fact", "mindroom_replica_key": f"{name}-turn"}],
        )
    config = _config(storage, "mem0", "filer")
    config.agents["filer"].memory_backend = "file"
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
async def test_a_turn_keeps_the_copy_main_s_edit_bug_left_whole(tmp_path: Path) -> None:
    """Recorded on main after deleting one fact and editing another through `general`, which sorts second.

    Main's edit bug deleted calculator's whole first turn and rewrote both of its second-turn records to the edited
    text, so each turn keeps general's extraction, which holds the most distinct facts, and no fact is lost or doubled.
    """
    recorded = json.loads((_FIXTURES / "mem0_member_stores_after_edits.json").read_text())
    for member, collections in recorded.items():
        client = _client(tmp_path / "agents" / member / "chroma")
        for name, rows in collections.items():
            client.get_or_create_collection(name, embedding_function=None).add(
                ids=[row["id"] for row in rows],
                embeddings=[row["embedding"] for row in rows],
                metadatas=[row["metadata"] for row in rows],
            )

    await _migrate(tmp_path)

    merged = _stored(tmp_path / "team_memory" / _TEAM / "chroma")
    assert sorted(record["metadata"]["data"] for record in merged.values()) == [
        "Team fact five",
        "Team fact four, revised",
        "Team fact three",
        "Team fact two",
    ]
    assert set(merged) == {row["id"] for row in recorded["general"][_COLLECTION]}


def _plant(storage: Path, member: str, rows: list[tuple[str, str, str]]) -> None:
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
                "user_id": _TEAM,
                "data": text,
                "hash": hashlib.md5(text.encode(), usedforsecurity=False).hexdigest(),
                "mindroom_replica_key": turn,
            }
            for _id, turn, text in rows
        ],
    )


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
        delete = Collection.delete
        deletions: list[object] = []

        def interrupted_delete(collection: Collection, *args: object, **kwargs: object) -> None:
            # Each member's dropped turn goes first, then calculator's kept one; general's kept one would be next.
            if len(deletions) == 3:
                raise _StartStoppedError
            deletions.append(collection)
            delete(collection, *args, **kwargs)

        with patch.object(Collection, "delete", interrupted_delete), pytest.raises(_StartStoppedError):
            await _migrate(tmp_path)

    await _migrate(tmp_path)

    merged = _stored(tmp_path / "team_memory" / _TEAM / "chroma")
    assert sorted(record["metadata"]["data"] for record in merged.values()) == ["Fact A", "Fact B", "Fact C"]


@pytest.mark.asyncio
async def test_a_tied_copy_main_s_edit_bug_rewrote_is_stored_once(tmp_path: Path) -> None:
    """An edit through general rewrote both of calculator's records of the turn to its one edited fact."""
    _plant(tmp_path, "calculator", [("c1", "t1", "Fact A, edited"), ("c2", "t1", "Fact A, edited")])
    _plant(tmp_path, "general", [("g1", "t1", "Fact A, edited")])

    await _migrate(tmp_path)

    merged = _stored(tmp_path / "team_memory" / _TEAM / "chroma")
    assert [record["metadata"]["data"] for record in merged.values()] == ["Fact A, edited"]
