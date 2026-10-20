"""Tests for pointing symlinks into an agent's state root at the root's new place when the agent is renamed."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_rename.entity_state import entity_state_steps
from mindroom.agent_rename.links import link_steps
from mindroom.agent_rename.steps import (
    BACKUPS_DIRECTORY_NAME,
    FileRewrite,
    RenameApplyError,
    RenamePlan,
    RenameRefusedError,
    RetargetLink,
    apply_plan,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.history.replay import scope_visible_runs
from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import create_session_storage, get_agent_session
from mindroom.tool_system.worker_routing import agent_state_root_path
from tests.agent_rename_fixtures.entity_state import SESSION, seed_agent
from tests.agent_rename_helpers import tree_rows, tree_snapshot
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_rename.steps import Step
    from mindroom.constants import RuntimePaths

OLD = "atlas"
NEW = "nova"
# Shares the renamed agent's state root through the link agents/sidekick -> atlas, as a real install does.
SHARER = "sidekick"
SHARER_CONFIG = Config(agents={OLD: AgentConfig(display_name="Atlas"), SHARER: AgentConfig(display_name="Flash")})
NOW = datetime(2026, 10, 20, 9, 30, 0, tzinfo=UTC)


def _install(tmp_path: Path) -> RuntimePaths:
    """Seed the agent, link the sharing agent's state root to it, and seed the sharer's history through the link."""
    runtime_paths = test_runtime_paths(tmp_path)
    seed_agent(runtime_paths, OLD)
    agents = runtime_paths.storage_root / "agents"
    (agents / SHARER).symlink_to(OLD)
    seed_agent(runtime_paths, SHARER)
    (agents / OLD / "workspace").mkdir()
    (agents / OLD / "workspace" / "notes.md").write_text("notes\n", encoding="utf-8")
    return runtime_paths


def _links(runtime_paths: RuntimePaths, *, old: str = OLD, new: str = NEW) -> list[RetargetLink]:
    return link_steps(runtime_paths, old_name=old, new_name=new)


def _plan(runtime_paths: RuntimePaths, *extra: Step) -> RenamePlan:
    """Move the state root, retarget the links after it, then run ``extra``."""
    return RenamePlan(
        storage_root=runtime_paths.storage_root,
        steps=(*entity_state_steps(runtime_paths, old_name=OLD, new_name=NEW), *_links(runtime_paths), *extra),
        reports=(),
    )


def _link_texts(root: Path) -> dict[str, bytes | str]:
    """Map every symlink under ``root`` outside the backups to its text."""
    return {
        path: value
        for path, value in tree_snapshot(root).items()
        if isinstance(value, str) and value.startswith("symlink:") and BACKUPS_DIRECTORY_NAME not in path.split("/")
    }


def _fail(content: bytes) -> bytes:  # noqa: ARG001
    msg = "boom"
    raise RuntimeError(msg)


def test_a_sibling_link_follows_the_moved_state_root(tmp_path: Path) -> None:
    """agents/sidekick -> atlas becomes -> nova, so sidekick still finds its history in the moved root."""
    runtime_paths = _install(tmp_path)
    storage = runtime_paths.storage_root
    link = storage / "agents" / SHARER

    (step,) = _links(runtime_paths)
    assert (step.path, step.old_target, step.target) == (link, OLD, NEW)
    apply_plan(_plan(runtime_paths), kind="rename", now=NOW)

    assert link.readlink().as_posix() == NEW
    sessions = agent_state_root_path(storage, SHARER).resolve() / "sessions" / f"{SHARER}.db"
    assert sessions == storage / "agents" / NEW / "sessions" / f"{SHARER}.db"
    assert sessions.is_file()
    # MindRoom's own resolution opens the same database and finds sidekick's history in it.
    database = create_session_storage(SHARER, SHARER_CONFIG, runtime_paths, execution_identity=None)
    try:
        assert getattr(database, "db_file", None) == str(sessions)
        session = get_agent_session(database, SESSION)
        assert session is not None
        # The seed summarized r1, so r2 is the run history replays.
        assert [run.run_id for run in scope_visible_runs(session, HistoryScope(kind="agent", scope_id=SHARER))] == [
            "r2",
        ]
    finally:
        database.close()
    # Opening sidekick's store went through the retargeted link, so nothing recreated the old root.
    assert not os.path.lexists(storage / "agents" / OLD)


def test_nested_absolute_and_config_directory_links_keep_their_form(tmp_path: Path) -> None:
    """Relative links stay relative and absolute links keep their prefix; only the agents/<OLD> component changes."""
    runtime_paths = _install(tmp_path)
    storage = runtime_paths.storage_root
    old_root = storage / "agents" / OLD
    links = {
        storage / "workers" / "w1" / "workspace": "../../agents/atlas/workspace",
        storage / "plugins" / "demo" / "notes.md": f"{old_root}/workspace/notes.md",
        storage / "agents" / "alias": "./atlas/",
        runtime_paths.config_dir / "atlas-workspace": "mindroom_data/agents/atlas/workspace",
        # Inside the moved root: one link steps out of it and back in, the other stays inside and keeps working.
        old_root / "sessions" / "back": "../../atlas/workspace",
        old_root / "inner": "workspace/notes.md",
    }
    for link, text in links.items():
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(text)
    new_root = storage / "agents" / NEW

    planned = {step.path: (step.old_target, step.target) for step in _links(runtime_paths)}
    apply_plan(_plan(runtime_paths), kind="rename", now=NOW)

    assert planned == {
        storage / "agents" / SHARER: (OLD, NEW),
        storage / "workers" / "w1" / "workspace": (
            "../../agents/atlas/workspace",
            "../../agents/nova/workspace",
        ),
        storage / "plugins" / "demo" / "notes.md": (
            f"{old_root}/workspace/notes.md",
            f"{new_root}/workspace/notes.md",
        ),
        storage / "agents" / "alias": ("./atlas/", "./nova/"),
        runtime_paths.config_dir / "atlas-workspace": (
            "mindroom_data/agents/atlas/workspace",
            "mindroom_data/agents/nova/workspace",
        ),
        new_root / "sessions" / "back": ("../../atlas/workspace", "../../nova/workspace"),
    }
    moved = {(new_root / link.relative_to(old_root) if link.is_relative_to(old_root) else link) for link in links}
    # Exact texts, which Path.readlink would normalize.
    assert {link: os.readlink(link) for link in moved} == {  # noqa: PTH115
        storage / "workers" / "w1" / "workspace": "../../agents/nova/workspace",
        storage / "plugins" / "demo" / "notes.md": f"{new_root}/workspace/notes.md",
        storage / "agents" / "alias": "./nova/",
        runtime_paths.config_dir / "atlas-workspace": "mindroom_data/agents/nova/workspace",
        new_root / "sessions" / "back": "../../nova/workspace",
        new_root / "inner": "workspace/notes.md",
    }
    assert all(link.exists() for link in moved)


def test_links_to_a_longer_agent_name_are_untouched(tmp_path: Path) -> None:
    """Renaming code plans nothing for links into agents/code_team or other names that start like it."""
    runtime_paths = test_runtime_paths(tmp_path)
    agents = runtime_paths.storage_root / "agents"
    (agents / "code_team" / "workspace").mkdir(parents=True)
    (agents / "helper").symlink_to("code_team")
    (agents / "codex").symlink_to("code_team/workspace")
    (runtime_paths.storage_root / "team-workspace").symlink_to("agents/code_team/workspace")
    (runtime_paths.config_dir / "code_team").symlink_to(agents / "code_team")
    (runtime_paths.config_dir / "code-dir").symlink_to(agents / "codex")
    (agents / "linked").symlink_to("code")

    assert [step.path for step in _links(runtime_paths, old="code", new="coder")] == [agents / "linked"]


def test_undo_restores_the_original_link_text(tmp_path: Path) -> None:
    """A later failing step undoes the retargets with the move, leaving every link as it was."""
    runtime_paths = _install(tmp_path)
    (runtime_paths.config_dir / "workspace").symlink_to(runtime_paths.storage_root / "agents" / OLD / "workspace")
    # Restoring a database from its backup keeps its rows but not its header bytes, so databases compare by rows.
    before = (tree_rows(runtime_paths.storage_root), _link_texts(tmp_path))
    plan = _plan(runtime_paths, FileRewrite(runtime_paths.config_path, _fail, "rewrite the config"))
    assert any(isinstance(step, RetargetLink) for step in plan.steps)

    with pytest.raises(RenameApplyError, match="rewrite the config"):
        apply_plan(plan, kind="rename", now=NOW)

    assert (tree_rows(runtime_paths.storage_root), _link_texts(tmp_path)) == before


def test_planning_changes_nothing(tmp_path: Path) -> None:
    """Planning and describing the retargets, as a dry run does, leaves every link and file as it was."""
    runtime_paths = _install(tmp_path)
    before = tree_snapshot(tmp_path)

    lines = _plan(runtime_paths).describe()

    assert f"Point the symlink {runtime_paths.storage_root / 'agents' / SHARER} at {NEW} instead of {OLD}" in "\n".join(
        lines,
    )
    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("change", ["retargeted", "replaced by a directory"])
def test_a_link_changed_since_planning_refuses(tmp_path: Path, change: str) -> None:
    """A link that no longer has its planned text refuses the step, and the state root moves back."""
    runtime_paths = _install(tmp_path)
    storage = runtime_paths.storage_root
    link = storage / "agents" / SHARER
    plan = _plan(runtime_paths)
    link.unlink()
    if change == "retargeted":
        link.symlink_to("elsewhere")
    else:
        link.mkdir()

    with pytest.raises(RenameRefusedError, match="changed since the rename was planned"):
        apply_plan(plan, kind="rename", now=NOW)

    assert (storage / "agents" / OLD / "workspace" / "notes.md").is_file()
    assert not os.path.lexists(storage / "agents" / NEW)
    if change == "retargeted":
        assert link.readlink().as_posix() == "elsewhere"
    else:
        assert link.is_dir()
        assert not link.is_symlink()


def test_skipped_and_linked_directories_are_not_walked(tmp_path: Path) -> None:
    """Links in the backups, the crypto store, worker virtualenvs and caches, or behind linked directories stay."""
    runtime_paths = test_runtime_paths(tmp_path)
    storage = runtime_paths.storage_root
    old_root = storage / "agents" / OLD
    old_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "link").symlink_to(old_root)
    (storage / "linked-dir").symlink_to(outside, target_is_directory=True)
    for relative in (
        f"{BACKUPS_DIRECTORY_NAME}/20261019T120000Z-rename/000/link",
        "encryption_keys/link",
        "workers/w1/venv/link",
        "workers/w1/cache/link",
    ):
        link = storage / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(old_root)

    assert _links(runtime_paths) == []
