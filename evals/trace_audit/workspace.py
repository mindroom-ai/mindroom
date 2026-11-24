"""Isolated scratch workspace, config/roster copy, and symlink-safe skills helpers for trace-audit backtests."""

from __future__ import annotations

import shutil
from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.config.main import load_config
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.skills import SKILL_FILENAME, parse_skill_markdown

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def ensure_disjoint_skills_dir(skills_dir: Path, scratch: Path) -> None:
    """Raise ``ValueError`` if ``skills_dir`` equals, contains, or is inside ``scratch``."""
    src, dst = skills_dir.resolve(), scratch.resolve()
    if src == dst or dst.is_relative_to(src) or src.is_relative_to(dst):
        msg = f"--skills-dir ({skills_dir}) must not overlap scratch directory ({scratch})"
        raise ValueError(msg)


def copy_dir_no_symlinks(src: Path, dst: Path) -> None:
    """Recursively copy regular files/directories from ``src`` to ``dst``, skipping all symlinks."""
    dst.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        return
    for entry in sorted(src.iterdir()):
        if entry.is_symlink():
            continue
        target = dst / entry.name
        if entry.is_dir():
            copy_dir_no_symlinks(entry, target)
        elif entry.is_file():
            shutil.copy2(entry, target)


def prepare_scratch_skills(skills_dir: Path | None, scratch: Path) -> dict[str, str]:
    """Reset ``scratch`` safely, copy ``skills_dir`` without following symlinks, and return initial skill text."""
    if skills_dir is not None:
        ensure_disjoint_skills_dir(skills_dir, scratch)
    if scratch.exists():
        shutil.rmtree(scratch)
    if skills_dir is not None and skills_dir.exists():
        copy_dir_no_symlinks(skills_dir, scratch)
    else:
        scratch.mkdir(parents=True, exist_ok=True)
    return read_skills_snapshot(scratch)


def read_skills_snapshot(skills_root: Path) -> dict[str, str]:
    """Read non-symlinked ``SKILL.md`` files under ``skills_root`` keyed by skill name."""
    snapshot: dict[str, str] = {}
    if not skills_root.exists():
        return snapshot
    for child in sorted(skills_root.iterdir()):
        skill_file = child / SKILL_FILENAME
        if child.is_symlink() or not child.is_dir() or skill_file.is_symlink() or not skill_file.is_file():
            continue
        text = skill_file.read_text(encoding="utf-8")
        try:
            raw = parse_skill_markdown(text)[0].get("name")
            name = raw.strip() if isinstance(raw, str) and raw.strip() else child.name
        except Exception:
            name = child.name
        snapshot[name] = text
    return snapshot


def prepare_isolated_runtime(
    *,
    out_dir: Path,
    config_path: Path,
    roster_path: Path | None = None,
) -> tuple[Config, RuntimePaths]:
    """Create 0700 output/scratch storage, copy config & roster, and disable LLM request logging."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    scratch_storage = out_dir / "_runtime"
    scratch_storage.mkdir(parents=True, exist_ok=True)

    scratch_config = scratch_storage / "config.yaml"
    shutil.copy2(config_path, scratch_config)
    scratch_config.chmod(0o600)

    roster_src = (
        roster_path
        if roster_path is not None
        else config_path.parent / "mindroom_data" / "tracking" / "personal_agents.json"
    )
    if roster_path is not None and not roster_src.is_file():
        msg = f"--roster-path not found: {roster_src}"
        raise ValueError(msg)
    if roster_src.is_file() and not roster_src.is_symlink():
        roster_dst = scratch_storage / "tracking" / "personal_agents.json"
        roster_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(roster_src, roster_dst)
        roster_dst.chmod(0o600)

    source_paths = resolve_runtime_paths(config_path=config_path, storage_path=scratch_storage)
    scratch_paths = resolve_runtime_paths(config_path=scratch_config, storage_path=scratch_storage)
    runtime_paths = replace(
        scratch_paths,
        env_path=source_paths.env_path,
        env_file_values=source_paths.env_file_values,
    )
    loaded = load_config(runtime_paths, tolerate_plugin_load_errors=True)
    isolated = loaded.model_copy(
        update={
            "debug": loaded.debug.model_copy(
                update={"log_llm_requests": False, "llm_request_log_dir": None},
            ),
        },
    )
    return isolated, runtime_paths
