"""Knowledge listing, binding, and reads never follow links agent code plants in a workspace."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge import manager as knowledge_manager_module
from mindroom.knowledge.file_listing import knowledge_files_from_relative_paths, list_knowledge_files
from mindroom.runtime_resolution import resolve_knowledge_binding

if TYPE_CHECKING:
    from pathlib import Path

_VICTIM_NOTE = "victim-only note"


def _victim_notes(tmp_path: Path) -> Path:
    victim = tmp_path / "storage" / "private_instances" / "victim-scope" / "alpha" / "alpha_data" / "notes"
    victim.mkdir(parents=True)
    (victim / "secret.md").write_text(_VICTIM_NOTE, encoding="utf-8")
    return victim


@pytest.mark.parametrize("spelling", ["absolute", "storage_variable"])
def test_shared_knowledge_binding_refuses_a_link_below_its_agent_workspace(tmp_path: Path, spelling: str) -> None:
    """A shared base inside an agent workspace cannot be pointed at another instance by a planted link."""
    storage = tmp_path / "storage"
    workspace = storage / "agents" / "alpha" / "workspace"
    workspace.mkdir(parents=True)
    victim = _victim_notes(tmp_path)
    path = (
        str(workspace / "thread_exports")
        if spelling == "absolute"
        else "${MINDROOM_STORAGE_PATH}/agents/alpha/workspace/thread_exports"
    )
    config = Config(
        agents={"alpha": AgentConfig(display_name="Alpha", knowledge_bases=["threads"])},
        knowledge_bases={"threads": KnowledgeBaseConfig(path=path)},
    )
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=storage)
    (workspace / "thread_exports").mkdir()

    assert (
        resolve_knowledge_binding("threads", config, runtime_paths, execution_identity=None).knowledge_path
        == (workspace / "thread_exports").resolve()
    )

    (workspace / "thread_exports").rmdir()
    (workspace / "thread_exports").symlink_to(victim, target_is_directory=True)

    with pytest.raises(ValueError, match="must stay within"):
        resolve_knowledge_binding("threads", config, runtime_paths, execution_identity=None)


def test_shared_knowledge_outside_workspaces_keeps_operator_links(tmp_path: Path) -> None:
    """Operator-owned knowledge outside any agent workspace still follows its configured link."""
    storage = tmp_path / "storage"
    target = tmp_path / "docs-target"
    target.mkdir()
    storage.mkdir()
    (storage / "docs").symlink_to(target, target_is_directory=True)
    config = Config(knowledge_bases={"docs": KnowledgeBaseConfig(path=str(storage / "docs"))})
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=storage)

    binding = resolve_knowledge_binding("docs", config, runtime_paths, execution_identity=None)

    assert binding.knowledge_path == target.resolve()


def test_listing_refuses_a_knowledge_root_swapped_after_binding(tmp_path: Path) -> None:
    """A directory on the bound root's path replaced by a link lists nothing instead of the link target."""
    workspace = tmp_path / "workspace"
    root = workspace / "docs" / "kb"
    root.mkdir(parents=True)
    (root / "own.md").write_text("own notes", encoding="utf-8")
    victim = tmp_path / "victim"
    (victim / "kb").mkdir(parents=True)
    (victim / "kb" / "secret.md").write_text(_VICTIM_NOTE, encoding="utf-8")
    canonical_root = root.resolve()
    config = Config(knowledge_bases={"kb": KnowledgeBaseConfig(path=str(canonical_root))})
    assert [path.name for path in list_knowledge_files(config, "kb", canonical_root)] == ["own.md"]

    (workspace / "docs").rename(workspace / "docs-moved")
    (workspace / "docs").symlink_to(victim, target_is_directory=True)

    assert list_knowledge_files(config, "kb", canonical_root) == []
    assert knowledge_files_from_relative_paths(config, "kb", canonical_root, ["secret.md"]) == []


@pytest.mark.parametrize("planted", ["link", "fifo"])
def test_knowledge_reads_refuse_a_file_swapped_after_listing(tmp_path: Path, planted: str) -> None:
    """Signatures and reader snapshots refuse a listed file replaced by a link or FIFO instead of following it."""
    root = tmp_path / "kb"
    root.mkdir()
    listed = root / "own.md"
    listed.write_text("own notes", encoding="utf-8")
    secret = tmp_path / "secret.md"
    secret.write_text(_VICTIM_NOTE, encoding="utf-8")
    config = Config(knowledge_bases={"kb": KnowledgeBaseConfig(path=str(root.resolve()))})
    [listed_path] = list_knowledge_files(config, "kb", root.resolve())
    listed.unlink()
    if planted == "link":
        listed.symlink_to(secret)
    else:
        os.mkfifo(listed)

    with pytest.raises((OSError, ValueError)):
        knowledge_manager_module._file_signature(listed_path)
    with pytest.raises((OSError, ValueError)), knowledge_manager_module._knowledge_source_snapshot(listed_path):
        pytest.fail("a swapped knowledge file was copied")
