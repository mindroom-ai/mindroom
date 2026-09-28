"""Knowledge listing, binding, and reads never follow links agent code plants in a workspace."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge import manager as knowledge_manager_module
from mindroom.knowledge.file_listing import knowledge_files_from_relative_paths, list_knowledge_files
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.registry import _published_index_key_from_binding
from mindroom.runtime_resolution import ResolvedKnowledgeBinding, resolve_knowledge_binding

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


@pytest.mark.parametrize("area", ["private_instances/scope/alpha", "workers/w1"])
def test_shared_knowledge_binding_refuses_links_below_what_workers_write(tmp_path: Path, area: str) -> None:
    """A shared base inside a private instance or worker root is bound without following links there either."""
    storage = tmp_path / "storage"
    victim = _victim_notes(tmp_path)
    (storage / area).mkdir(parents=True)
    (storage / area / "exports").symlink_to(victim, target_is_directory=True)
    config = Config(knowledge_bases={"threads": KnowledgeBaseConfig(path=f"${{MINDROOM_STORAGE_PATH}}/{area}/exports")})
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=storage)

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


def test_manager_and_index_key_never_re_resolve_a_bound_root(tmp_path: Path) -> None:
    """A bound root swapped for a link before the manager is built is refused, never indexed at its target."""
    storage = tmp_path / "storage"
    root = (tmp_path / "workspace" / "kb").resolve()
    root.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "secret.md").write_text(_VICTIM_NOTE, encoding="utf-8")
    config = Config(knowledge_bases={"kb": KnowledgeBaseConfig(path=str(root))})
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=storage)
    binding = ResolvedKnowledgeBinding(
        base_id="kb",
        storage_root=storage,
        knowledge_path=root,
        incremental_sync_on_access=False,
    )
    root.rename(root.with_name("kb-moved"))
    root.symlink_to(victim, target_is_directory=True)

    assert _published_index_key_from_binding("kb", binding, config=config).knowledge_path == str(root)
    with pytest.raises(ValueError, match="link"):
        KnowledgeManager("kb", config=config, runtime_paths=runtime_paths, knowledge_path=root)


def test_listing_warns_instead_of_silently_listing_nothing(tmp_path: Path) -> None:
    """A knowledge root that became a link is reported, and a missing one stays quiet."""
    root = tmp_path / "kb"
    config = Config(knowledge_bases={"kb": KnowledgeBaseConfig(path=str(root))})
    with capture_logs() as logs:
        assert list_knowledge_files(config, "kb", root) == []
    assert logs == []

    victim = tmp_path / "victim"
    victim.mkdir()
    root.symlink_to(victim, target_is_directory=True)
    with capture_logs() as logs:
        assert list_knowledge_files(config, "kb", root) == []
    assert [entry["log_level"] for entry in logs] == ["warning"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_listing_walks_through_search_only_ancestors(tmp_path: Path) -> None:
    """Ancestors that grant this process only search permission, like another user's 0711 home, still work."""
    home = tmp_path / "home"
    root = home / "kb"
    root.mkdir(parents=True)
    (root / "own.md").write_text("own notes", encoding="utf-8")
    config = Config(knowledge_bases={"kb": KnowledgeBaseConfig(path=str(root))})
    home.chmod(0o111)
    try:
        listed = list_knowledge_files(config, "kb", root)
    finally:
        home.chmod(0o755)

    assert [path.name for path in listed] == ["own.md"]


def test_knowledge_files_above_the_read_cap_are_skipped(tmp_path: Path) -> None:
    """A sparse or huge file is left out with a warning, and one that grows after listing is never copied."""
    root = (tmp_path / "kb").resolve()
    root.mkdir()
    (root / "grows.md").write_text("small for now", encoding="utf-8")
    with (root / "huge.md").open("wb") as huge:
        huge.truncate(65 << 20)
    config = Config(knowledge_bases={"kb": KnowledgeBaseConfig(path=str(root))})

    with capture_logs() as logs:
        [listed] = list_knowledge_files(config, "kb", root)
    assert listed.name == "grows.md"
    assert [entry["log_level"] for entry in logs] == ["warning"]

    with (root / "grows.md").open("r+b") as grows:
        grows.truncate(65 << 20)
    with pytest.raises(ValueError, match="size limit"), knowledge_manager_module._knowledge_source_snapshot(listed):
        pytest.fail("an oversized knowledge file was copied")
