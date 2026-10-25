"""Long-term memory of agents and teams in mem0's Chroma stores and in memory files."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from chromadb.api.client import Client
from chromadb.config import Settings
from mem0.vector_stores.chroma import ChromaDB

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.memory._file_backend import _append_scope_memory_entry
from mindroom.memory._policy import (
    allowed_scope_storage_paths,
    resolve_file_memory_resolution,
    storage_paths_for_scope_user_id,
)
from mindroom.memory.config import _get_memory_config, _install_chroma_similarity_scores
from mindroom.memory_scope_ids import agent_name_from_scope_user_id, agent_scope_user_id, build_team_user_id
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.memory._shared import FileMemoryResolution


OLD = "code"

# Its name extends the renamed agent's, so a substring match would wrongly touch it.
PREFIX = "code_team"
HELPER = "helper"

# A team formed on the fly, never configured; MindRoom stores its memory under every member's state root.
AD_HOC = [OLD, PREFIX, HELPER]

# (scope user id, memory text, the metadata MindRoom adds), written to every store MindRoom writes the scope to.
MEMORIES = (
    (agent_scope_user_id(OLD), "code prefers tabs", {"type": "conversation", "agent": OLD}),
    (build_team_user_id([OLD, HELPER]), "crew ships on fridays", {"is_team": True, "team_members": [OLD, HELPER]}),
    (build_team_user_id(AD_HOC), "pairing notes", {"is_team": True, "team_members": AD_HOC}),
    (agent_scope_user_id(PREFIX), "code_team reviews", {"type": "conversation", "agent": PREFIX}),
    (agent_scope_user_id(HELPER), "helper drafts", {"type": "conversation", "agent": HELPER}),
    (build_team_user_id([PREFIX, HELPER]), "duo standup", {"is_team": True, "team_members": [PREFIX, HELPER]}),
)
TEXTS = [text for _, text, _ in MEMORIES]


def memory_config(agent: str) -> Config:
    """Return the config naming the renamed agent ``agent``, a member of team ``crew`` with ``helper``."""
    return Config(
        agents={name: AgentConfig(display_name=name.title()) for name in (agent, PREFIX, HELPER)},
        teams={"crew": TeamConfig(display_name="Crew", role="Ships releases", agents=[agent, HELPER])},
    )


def query_vector(text: str) -> list[float]:
    """Return a vector that matches only the memory ``text``."""
    return [1.0 if candidate == text else 0.0 for candidate in TEXTS]


def chroma_client(path: str) -> Client:
    """Return a client for the Chroma store at ``path`` with mem0's settings, which releases the store when closed."""
    return Client(settings=Settings(anonymized_telemetry=False, is_persistent=True, persist_directory=path))


@contextmanager
def memory_store(root: Path, config: Config, runtime_paths: RuntimePaths) -> Iterator[ChromaDB]:
    """Open the mem0 Chroma store MindRoom keeps at a state root, and close it again like a stopped MindRoom."""
    vector_store = _get_memory_config(root, config, runtime_paths)["vector_store"]["config"]
    with chroma_client(vector_store["path"]) as client:
        store = ChromaDB(collection_name=vector_store["collection_name"], client=client)
        _install_chroma_similarity_scores(store)
        yield store


def entity_store(store: ChromaDB) -> ChromaDB:
    """Return mem0's entity store beside ``store``: collection ``<collection>_entities`` in the same Chroma store."""
    return ChromaDB(collection_name=f"{store.collection_name}_entities", client=store.client)


def file_resolution(scope: str, root: Path, config: Config, runtime_paths: RuntimePaths) -> FileMemoryResolution:
    """Resolve a scope's file memory at ``root`` the way the file backend does for reads and writes."""
    return resolve_file_memory_resolution(
        root,
        config,
        runtime_paths,
        agent_name=agent_name_from_scope_user_id(scope),
        original_storage_path=runtime_paths.storage_root,
    )


def install_memories(tmp_path: Path, memories: tuple[tuple[str, str, dict[str, Any]], ...] = MEMORIES) -> RuntimePaths:
    """Write each memory to mem0 and to file memory at every state root MindRoom stores its scope under."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = memory_config(OLD)
    for index, (scope, text, metadata) in enumerate(memories):
        for root in storage_paths_for_scope_user_id(scope, runtime_paths.storage_root, config, runtime_paths):
            with memory_store(root, config, runtime_paths) as store:
                payload = {"data": text, "user_id": scope, **metadata}
                store.insert(vectors=[query_vector(text)], payloads=[payload], ids=[f"memory-{index}"])
                # The entity record mem0 2.0.1 links to the memory (mem0/memory/main.py's _upsert_entity).
                entity = {"data": f"{text} entity", "entity_type": "concept", "linked_memory_ids": [f"memory-{index}"]}
                entity_store(store).insert(
                    vectors=[query_vector(text)],
                    payloads=[{**entity, "user_id": scope}],
                    ids=[f"entity-{index}"],
                )
            resolution = file_resolution(scope, root, config, runtime_paths)
            _append_scope_memory_entry(scope, text, resolution, config)
    return runtime_paths


def visible_memories(caller: str | list[str], config: Config, runtime_paths: RuntimePaths) -> Counter[tuple[str, str]]:
    """Count the (scope, text) memories ``caller`` reads from mem0, over every store MindRoom's scope policy reads."""
    seen: Counter[tuple[str, str]] = Counter()
    for scope, root in allowed_scope_storage_paths(caller, runtime_paths.storage_root, config, runtime_paths):
        with memory_store(root, config, runtime_paths) as store:
            seen.update((scope, hit.payload["data"]) for hit in store.list(filters={"user_id": scope}, top_k=100)[0])
    return seen
