"""Isolated scratch workspace, config/roster loading, and symlink-safe skills helpers for trace-audit backtests."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from mindroom.config.legacy_access import migrate_access_config_data
from mindroom.config.main import Config
from mindroom.config.yaml_includes import load_yaml_config_source_with_digests
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.skills import SKILL_FILENAME, parse_skill_markdown

if TYPE_CHECKING:
    from pathlib import Path

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
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.parent.chmod(0o700)
    if scratch.exists():
        shutil.rmtree(scratch)
    if skills_dir is not None and skills_dir.exists():
        copy_dir_no_symlinks(skills_dir, scratch)
    else:
        scratch.mkdir(parents=True, exist_ok=True)
    scratch.chmod(0o700)
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


def _copy_private_tracking_file(src: Path, dst: Path) -> None:
    if src.is_file() and not src.is_symlink():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        dst.chmod(0o600)


def prepare_isolated_runtime(
    *,
    out_dir: Path,
    config_path: Path,
    roster_path: Path | None = None,
) -> tuple[Config, RuntimePaths]:
    """Create private scratch dirs, load config in memory (supporting ``!include``), and disable request logging."""
    existed = out_dir.exists()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not existed:
        out_dir.chmod(0o700)

    scratch_storage = out_dir / "_runtime"
    scratch_storage.mkdir(parents=True, exist_ok=True)
    scratch_storage.chmod(0o700)

    scratch_skills = out_dir / "scratch_skills"
    scratch_skills.mkdir(parents=True, exist_ok=True)
    scratch_skills.chmod(0o700)

    default_tracking = config_path.parent / "mindroom_data" / "tracking"
    roster_src = roster_path if roster_path is not None else default_tracking / "personal_agents.json"
    if roster_path is not None and not roster_src.is_file():
        msg = f"--roster-path not found: {roster_src}"
        raise ValueError(msg)
    tracking_dst = scratch_storage / "tracking"
    _copy_private_tracking_file(roster_src, tracking_dst / "personal_agents.json")
    for contacts_src in (roster_src.parent / "contacts.json", default_tracking / "contacts.json"):
        if contacts_src.is_file() and not contacts_src.is_symlink():
            _copy_private_tracking_file(contacts_src, tracking_dst / "contacts.json")
            break

    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=scratch_storage)
    data, source_digests, uses_includes = load_yaml_config_source_with_digests(config_path)
    migrated = migrate_access_config_data(data)
    loaded = Config.validate_with_runtime(
        migrated.data,
        runtime_paths,
        tolerate_plugin_load_errors=True,
    )
    loaded.record_source_metadata(config_path, source_digests, uses_includes=uses_includes)
    isolated = loaded.model_copy(
        update={
            "debug": loaded.debug.model_copy(
                update={"log_llm_requests": False, "llm_request_log_dir": None},
            ),
        },
    )
    return isolated, runtime_paths
