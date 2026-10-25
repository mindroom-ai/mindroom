"""Tests for carrying an agent's long-term memory, in mem0's Chroma stores and in memory files, to its new name."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_rename.entity_state import entity_state_steps
from mindroom.agent_rename.links import link_steps
from mindroom.agent_rename.memory import memory_steps
from mindroom.agent_rename.steps import (
    BACKUPS_DIRECTORY_NAME,
    DirectoryEdit,
    FileRewrite,
    MovePath,
    RenameApplyError,
    RenamePlan,
    RenameRefusedError,
    apply_plan,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.memory._file_backend import _search_scope_memory_entries
from mindroom.memory._policy import (
    allowed_scope_storage_paths,
)
from mindroom.memory_scope_ids import agent_scope_user_id, build_team_user_id
from mindroom.tool_system.worker_routing import agent_state_root_path
from tests.agent_rename_fixtures.memory import (
    AD_HOC,
    HELPER,
    MEMORIES,
    OLD,
    PREFIX,
    TEXTS,
    chroma_client,
    entity_store,
    file_resolution,
    install_memories,
    memory_config,
    memory_store,
    query_vector,
    visible_memories,
)
from tests.agent_rename_helpers import tree_snapshot
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_rename.steps import Step
    from mindroom.constants import RuntimePaths

NEW = "coder"
NOW = datetime(2026, 10, 20, 9, 30, 0, tzinfo=UTC)


def _plan(runtime_paths: RuntimePaths) -> list[Step]:
    """Plan like `mindroom agents rename`: the state root moves first, then the memory inside and beside it."""
    return entity_state_steps(runtime_paths, old_name=OLD, new_name=NEW) + memory_steps(
        runtime_paths,
        config=memory_config(OLD),
        old_name=OLD,
        new_name=NEW,
    )


def _apply(runtime_paths: RuntimePaths, steps: list[Step]) -> None:
    apply_plan(
        RenamePlan(storage_root=runtime_paths.storage_root, steps=tuple(steps), reports=()),
        kind="rename",
        now=NOW,
    )


def _visible_files(caller: str | list[str], config: Config, runtime_paths: RuntimePaths) -> Counter[tuple[str, str]]:
    """Count the (scope, text) memories ``caller`` finds in file memory, searching each memory's own words."""
    seen: Counter[tuple[str, str]] = Counter()
    for scope, root in allowed_scope_storage_paths(caller, runtime_paths.storage_root, config, runtime_paths):
        resolution = file_resolution(scope, root, config, runtime_paths)
        for text in TEXTS:
            hits = _search_scope_memory_entries(scope, text, resolution, config, limit=10)
            seen.update((scope, hit["memory"]) for hit in hits if hit["memory"] == text)
    return seen


def _scope_ids_in_store(root: Path) -> dict[str, set[str]]:
    """Map each collection of the Chroma store at ``root`` to the scope ids of its records."""
    with chroma_client(str(root / "chroma")) as client:
        return {
            collection.name: {
                str(metadata["user_id"]) for metadata in collection.get(include=["metadatas"])["metadatas"] or []
            }
            for collection in client.list_collections()
        }


def _root(runtime_paths: RuntimePaths, agent: str) -> Path:
    return agent_state_root_path(runtime_paths.storage_root, agent)


def _step_paths(step: Step) -> list[Path]:
    if isinstance(step, MovePath):
        return [step.source, step.destination]
    if isinstance(step, DirectoryEdit):
        return [step.path]
    msg = f"Unexpected memory step {step}"
    raise AssertionError(msg)


def _without_backups(snapshot: dict[str, bytes | str]) -> dict[str, bytes | str]:
    return {key: value for key, value in snapshot.items() if not key.startswith(BACKUPS_DIRECTORY_NAME)}


def test_memories_searchable_under_new_scope_ids(tmp_path: Path) -> None:
    """After the rename, the agent reads exactly the memories it read before, under its new scope ids."""
    runtime_paths = install_memories(tmp_path)
    before = visible_memories(OLD, memory_config(OLD), runtime_paths)
    files_before = _visible_files(OLD, memory_config(OLD), runtime_paths)
    untouched = tree_snapshot(runtime_paths.storage_root)

    steps = _plan(runtime_paths)

    assert tree_snapshot(runtime_paths.storage_root) == untouched
    _apply(runtime_paths, steps)

    expected = Counter(
        {(agent_scope_user_id(NEW), "code prefers tabs"): 1, ("team_coder+helper", "crew ships on fridays"): 2},
    )
    assert before == Counter({("agent_code", "code prefers tabs"): 1, ("team_code+helper", "crew ships on fridays"): 2})
    assert visible_memories(NEW, memory_config(NEW), runtime_paths) == expected
    assert files_before == before
    assert _visible_files(NEW, memory_config(NEW), runtime_paths) == expected

    with memory_store(_root(runtime_paths, NEW), memory_config(NEW), runtime_paths) as store:
        hits = store.search(
            query="tabs",
            vectors=query_vector("code prefers tabs"),
            top_k=3,
            filters={"user_id": agent_scope_user_id(NEW)},
        )
        assert [(hit.payload["data"], hit.payload["agent"]) for hit in hits] == [("code prefers tabs", NEW)]
        assert hits[0].score == pytest.approx(1.0)
        assert (
            store.search(query="tabs", vectors=query_vector("code prefers tabs"), filters={"user_id": "agent_code"})
            == []
        )
        entities = entity_store(store)
        assert [
            (hit.payload["data"], hit.payload["linked_memory_ids"])
            for hit in entities.list(filters={"user_id": agent_scope_user_id(NEW)})[0]
        ] == [("code prefers tabs entity", ["memory-0"])]
        assert entities.list(filters={"user_id": "agent_code"})[0] == []
    assert not (_root(runtime_paths, NEW) / "memory_files" / agent_scope_user_id(OLD)).exists()


def test_team_scopes_recomputed(tmp_path: Path) -> None:
    """Configured and ad-hoc team scopes naming the agent get their sorted member list with the new name everywhere."""
    runtime_paths = install_memories(tmp_path)
    ad_hoc_before = visible_memories(AD_HOC, memory_config(OLD), runtime_paths)

    _apply(runtime_paths, _plan(runtime_paths))

    ad_hoc = [NEW, PREFIX, HELPER]
    # Sorted again: the new name sorts after code_team, the old one before it.
    assert build_team_user_id(ad_hoc) == "team_code_team+coder+helper"
    assert ad_hoc_before == Counter({("team_code+code_team+helper", "pairing notes"): 3})
    expected = Counter({("team_code_team+coder+helper", "pairing notes"): 3})
    assert visible_memories(ad_hoc, memory_config(NEW), runtime_paths) == expected
    assert _visible_files(ad_hoc, memory_config(NEW), runtime_paths) == expected

    old_scopes = {"agent_code", "team_code+helper", "team_code+code_team+helper"}
    expected_scopes = {
        NEW: {"agent_coder", "team_coder+helper", "team_code_team+coder+helper"},
        PREFIX: {"agent_code_team", "team_code_team+coder+helper", "team_code_team+helper"},
        HELPER: {"agent_helper", "team_coder+helper", "team_code_team+coder+helper", "team_code_team+helper"},
    }
    for agent, scopes in expected_scopes.items():
        root = _root(runtime_paths, agent)
        # Memories and the entity records linked to them alike.
        collections = _scope_ids_in_store(root)
        assert len(collections) == 2
        assert all(found == scopes for found in collections.values())
        assert {path.name for path in (root / "memory_files").iterdir()} == scopes
        assert not scopes & old_scopes
    with memory_store(_root(runtime_paths, HELPER), memory_config(NEW), runtime_paths) as store:
        team_members = {
            hit.payload["user_id"]: hit.payload["team_members"]
            for hit in store.list(top_k=100)[0]
            if "team_members" in hit.payload
        }
    assert team_members == {
        "team_coder+helper": [NEW, HELPER],
        "team_code_team+coder+helper": [NEW, PREFIX, HELPER],
        "team_code_team+helper": [PREFIX, HELPER],
    }


def test_prefix_agent_untouched(tmp_path: Path) -> None:
    """Renaming code leaves code_team's stores alone when no team scope there names code."""
    memories = tuple(memory for memory in MEMORIES if memory[0] != build_team_user_id(AD_HOC))
    runtime_paths = install_memories(tmp_path, memories)
    prefix_root = _root(runtime_paths, PREFIX)
    before = tree_snapshot(prefix_root)

    steps = _plan(runtime_paths)

    assert any(isinstance(step, DirectoryEdit) for step in steps)
    assert not [path for step in steps for path in _step_paths(step) if path.is_relative_to(prefix_root)]
    _apply(runtime_paths, steps)
    assert tree_snapshot(prefix_root) == before
    assert visible_memories(PREFIX, memory_config(NEW), runtime_paths) == Counter(
        {("agent_code_team", "code_team reviews"): 1},
    )


def test_agent_sharing_the_state_root_through_a_symlink_gets_each_store_renamed_once(tmp_path: Path) -> None:
    """An agent whose state root is a symlink to the renamed agent's reads the same stores, so they are planned once."""
    runtime_paths = install_memories(tmp_path)
    _root(runtime_paths, "flash").symlink_to(OLD)
    config = memory_config(OLD)
    config = Config(agents={**config.agents, "flash": AgentConfig(display_name="Flash")}, teams=config.teams)
    before = visible_memories(OLD, memory_config(OLD), runtime_paths)

    shared = memory_steps(runtime_paths, config=config, old_name=OLD, new_name=NEW)
    alone = memory_steps(runtime_paths, config=memory_config(OLD), old_name=OLD, new_name=NEW)
    entity_state = entity_state_steps(runtime_paths, old_name=OLD, new_name=NEW)
    _apply(runtime_paths, [*entity_state, *link_steps(runtime_paths, old_name=OLD, new_name=NEW), *shared])

    expected = Counter(
        {(agent_scope_user_id(NEW), "code prefers tabs"): 1, ("team_coder+helper", "crew ships on fridays"): 2},
    )
    assert [step.description for step in shared] == [step.description for step in alone]
    assert before == Counter({("agent_code", "code prefers tabs"): 1, ("team_code+helper", "crew ships on fridays"): 2})
    assert visible_memories(NEW, memory_config(NEW), runtime_paths) == expected
    assert _visible_files(NEW, memory_config(NEW), runtime_paths) == expected


def test_failure_after_any_step_restores_the_tree(tmp_path: Path) -> None:
    """A failure after any number of completed steps puts every chroma store and memory file back byte for byte."""
    probe = _plan(install_memories(tmp_path / "probe"))
    assert {type(step) for step in probe} == {MovePath, DirectoryEdit}

    def fail(content: bytes) -> bytes:  # noqa: ARG001
        msg = "boom"
        raise RuntimeError(msg)

    for completed in range(len(probe) + 1):
        runtime_paths = install_memories(tmp_path / f"run-{completed}")
        sentinel = runtime_paths.storage_root / "sentinel.txt"
        sentinel.write_text("sentinel")
        original = tree_snapshot(runtime_paths.storage_root)
        steps = [*_plan(runtime_paths)[:completed], FileRewrite(sentinel, fail, "fail")]

        with pytest.raises(RenameApplyError, match="fail"):
            _apply(runtime_paths, steps)

        assert _without_backups(tree_snapshot(runtime_paths.storage_root)) == original


@pytest.mark.parametrize("taken", ["chroma", "memory_files"])
@pytest.mark.parametrize(
    ("stale_scope", "refusal"),
    [
        (agent_scope_user_id(NEW), "agent_coder already exists there"),
        # Renaming it would merge two members into one: team_coder+coder+helper.
        (build_team_user_id([OLD, NEW, HELPER]), "coder is already one of its members"),
    ],
)
def test_stale_new_scope_refuses(tmp_path: Path, taken: str, stale_scope: str, refusal: str) -> None:
    """A store already holding the new scope id, or a scope naming both names, refuses before anything is written."""
    runtime_paths = install_memories(tmp_path)
    old_root = _root(runtime_paths, OLD)
    if taken == "chroma":
        with memory_store(old_root, memory_config(OLD), runtime_paths) as store:
            payload = {"data": "a former coder's memory", "user_id": stale_scope}
            store.insert(vectors=[query_vector("code prefers tabs")], payloads=[payload], ids=["stale"])
    else:
        (old_root / "memory_files" / stale_scope).mkdir()
    before = tree_snapshot(runtime_paths.storage_root)

    with pytest.raises(RenameRefusedError, match=refusal):
        memory_steps(runtime_paths, config=memory_config(OLD), old_name=OLD, new_name=NEW)

    assert tree_snapshot(runtime_paths.storage_root) == before


def test_symlinked_chroma_store_refuses(tmp_path: Path) -> None:
    """Swapping an edited copy in would replace the link and leave the store it points to unedited, so it refuses."""
    runtime_paths = install_memories(tmp_path)
    store = _root(runtime_paths, OLD) / "chroma"
    store.rename(tmp_path / "elsewhere")
    store.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    before = tree_snapshot(tmp_path)

    with pytest.raises(RenameRefusedError, match="is a symlink"):
        memory_steps(runtime_paths, config=memory_config(OLD), old_name=OLD, new_name=NEW)

    assert tree_snapshot(tmp_path) == before


def test_agent_without_memory_plans_nothing(tmp_path: Path) -> None:
    """No store names the agent, so there is nothing to plan."""
    runtime_paths = test_runtime_paths(tmp_path)
    _root(runtime_paths, OLD).mkdir(parents=True)

    assert memory_steps(runtime_paths, config=memory_config(OLD), old_name=OLD, new_name=NEW) == []
