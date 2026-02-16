"""Markdown workspace helpers for agent memory and identity."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .logging_config import get_logger

if TYPE_CHECKING:
    from .config import Config

logger = get_logger(__name__)

WORKSPACE_DIRNAME = "workspace"
SOUL_FILENAME = "SOUL.md"
AGENTS_FILENAME = "AGENTS.md"
MEMORY_FILENAME = "MEMORY.md"

_DEFAULT_SOUL_FALLBACK = """# SOUL.md

## Core Truths

- You are an AI agent in MindRoom.
- You collaborate clearly, directly, and honestly.
- You prioritize practical outcomes and reliable execution.

## Boundaries

- Do not fabricate facts, outcomes, or tool results.
- Ask for clarification when critical details are missing.
- Keep sensitive context private to the current conversation scope.

## Vibe

- Calm, focused, and concise.
- Respectful and collaborative.
- Technical when needed, plain language when possible.
"""

_DEFAULT_AGENTS_FALLBACK = """# AGENTS.md

## Startup Protocol

Before responding:
1. Read `SOUL.md`.
2. Read `MEMORY.md` for long-term context in private conversations.
3. Read recent daily logs in `memory/`.

## Working Rules

- If a fact should persist, write it to memory files.
- Keep notes concrete and useful for future responses.
- Prefer simple, direct solutions over elaborate abstractions.
- Follow room context and privacy boundaries.
"""

_DEFAULT_MEMORY_TEMPLATE = """# MEMORY.md

Store durable, high-value facts that should persist across private conversations.
"""


def _load_template(filename: str, fallback: str) -> str:
    template_path = Path(__file__).with_name("templates") / filename
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return fallback


DEFAULT_SOUL_TEMPLATE = _load_template(SOUL_FILENAME, _DEFAULT_SOUL_FALLBACK)
DEFAULT_AGENTS_TEMPLATE = _load_template(AGENTS_FILENAME, _DEFAULT_AGENTS_FALLBACK)
DEFAULT_MEMORY_TEMPLATE = _DEFAULT_MEMORY_TEMPLATE


def get_agent_workspace_path(agent_name: str, storage_path: Path) -> Path:
    """Return the per-agent workspace path."""
    return storage_path / WORKSPACE_DIRNAME / agent_name


def _workspace_max_file_size(config: Config) -> int:
    return config.memory.workspace.max_file_size


def _workspace_enabled(config: Config) -> bool:
    return config.memory.workspace.enabled


def _normalize_markdown(value: str) -> str:
    return value.strip()


def _is_default_template(content: str, default_template: str) -> bool:
    return _normalize_markdown(content) == _normalize_markdown(default_template)


def _read_workspace_file(path: Path, max_file_size: int) -> str | None:
    if not path.exists() or not path.is_file():
        return None

    file_size = path.stat().st_size
    if file_size > max_file_size:
        logger.warning(
            "Workspace file exceeds configured size limit; skipping context load",
            path=str(path),
            file_size=file_size,
            max_file_size=max_file_size,
        )
        return None

    return path.read_text(encoding="utf-8")


def _read_custom_workspace_file(path: Path, default_template: str, max_file_size: int) -> str | None:
    content = _read_workspace_file(path, max_file_size)
    if content is None:
        return None
    if _is_default_template(content, default_template):
        return None
    return content


def _write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _safe_room_filename(room_id: str) -> str:
    safe_room = re.sub(r"[^A-Za-z0-9._-]", "_", room_id)
    return f"{safe_room}.md"


def _daily_log_scope(room_id: str | None) -> str:
    if room_id is None:
        return "_global"
    return re.sub(r"[^A-Za-z0-9._-]", "_", room_id)


def _daily_log_dir(workspace_dir: Path, room_id: str | None) -> Path:
    return workspace_dir / "memory" / _daily_log_scope(room_id)


def _prune_daily_logs(memory_dir: Path, retention_days: int) -> None:
    if retention_days < 0:
        return

    cutoff = datetime.now(UTC).date() - timedelta(days=retention_days)
    for log_path in memory_dir.rglob("*.md"):
        try:
            log_date = date.fromisoformat(log_path.stem)
        except ValueError:
            continue
        if log_date < cutoff:
            log_path.unlink(missing_ok=True)


def ensure_workspace(agent_name: str, storage_path: Path, config: Config) -> None:
    """Ensure workspace directories and base markdown files exist for an agent."""
    if not _workspace_enabled(config):
        return

    workspace_dir = get_agent_workspace_path(agent_name, storage_path)
    memory_dir = workspace_dir / "memory"
    rooms_dir = workspace_dir / "rooms"
    memory_dir.mkdir(parents=True, exist_ok=True)
    rooms_dir.mkdir(parents=True, exist_ok=True)

    _write_if_missing(workspace_dir / SOUL_FILENAME, DEFAULT_SOUL_TEMPLATE)
    _write_if_missing(workspace_dir / AGENTS_FILENAME, DEFAULT_AGENTS_TEMPLATE)
    _write_if_missing(workspace_dir / MEMORY_FILENAME, DEFAULT_MEMORY_TEMPLATE)

    _prune_daily_logs(memory_dir, config.memory.workspace.daily_log_retention_days)


def load_soul(agent_name: str, storage_path: Path, config: Config) -> str | None:
    """Load custom SOUL.md content when present and not the default template."""
    if not _workspace_enabled(config):
        return None

    workspace_dir = get_agent_workspace_path(agent_name, storage_path)
    return _read_custom_workspace_file(
        workspace_dir / SOUL_FILENAME,
        DEFAULT_SOUL_TEMPLATE,
        _workspace_max_file_size(config),
    )


def load_agents_md(agent_name: str, storage_path: Path, config: Config) -> str | None:
    """Load custom AGENTS.md content when present and not the default template."""
    if not _workspace_enabled(config):
        return None

    workspace_dir = get_agent_workspace_path(agent_name, storage_path)
    return _read_custom_workspace_file(
        workspace_dir / AGENTS_FILENAME,
        DEFAULT_AGENTS_TEMPLATE,
        _workspace_max_file_size(config),
    )


def load_workspace_memory(
    agent_name: str,
    storage_path: Path,
    config: Config,
    *,
    room_id: str | None = None,
    is_dm: bool = False,
) -> str:
    """Load workspace memory blocks to prepend to the user prompt."""
    if not _workspace_enabled(config):
        return ""

    workspace_dir = get_agent_workspace_path(agent_name, storage_path)
    max_file_size = _workspace_max_file_size(config)
    sections: list[str] = []

    if is_dm:
        memory_content = _read_custom_workspace_file(
            workspace_dir / MEMORY_FILENAME,
            DEFAULT_MEMORY_TEMPLATE,
            max_file_size,
        )
        if memory_content:
            sections.append(f"### Long-Term Memory\n{memory_content}")

    today = datetime.now(UTC).date()
    log_dir = _daily_log_dir(workspace_dir, room_id)
    for log_day in (today, today - timedelta(days=1)):
        log_path = log_dir / f"{log_day.isoformat()}.md"
        log_content = _read_workspace_file(log_path, max_file_size)
        if log_content:
            sections.append(f"### Daily Log {log_day.isoformat()}\n{log_content}")

    room_content = load_room_context(agent_name, room_id, storage_path, config)
    if room_content and room_id:
        sections.append(f"### Room Context ({room_id})\n{room_content}")

    if not sections:
        return ""

    return "## Workspace Context\n\n" + "\n\n".join(sections)


def load_room_context(
    agent_name: str,
    room_id: str | None,
    storage_path: Path,
    config: Config,
) -> str | None:
    """Load per-room context markdown for an agent."""
    if not room_id or not _workspace_enabled(config):
        return None

    room_path = get_agent_workspace_path(agent_name, storage_path) / "rooms" / _safe_room_filename(room_id)
    return _read_workspace_file(room_path, _workspace_max_file_size(config))


def load_team_workspace(team_name: str, storage_path: Path, config: Config, *, is_dm: bool = False) -> str:
    """Load team workspace context for prompt injection."""
    if not _workspace_enabled(config):
        return ""

    workspace_dir = storage_path / WORKSPACE_DIRNAME / team_name
    max_file_size = _workspace_max_file_size(config)
    sections: list[str] = []

    soul_content = _read_workspace_file(workspace_dir / SOUL_FILENAME, max_file_size)
    if soul_content:
        sections.append(f"### Team Soul\n{soul_content}")

    memory_content = _read_workspace_file(workspace_dir / MEMORY_FILENAME, max_file_size)
    if memory_content and is_dm:
        sections.append(f"### Team Memory\n{memory_content}")

    if not sections:
        return ""
    return "## Team Workspace Context\n\n" + "\n\n".join(sections)


def append_daily_log(
    agent_name: str | list[str],
    storage_path: Path,
    config: Config,
    content: str,
    *,
    room_id: str | None = None,
) -> None:
    """Append content to today's daily memory log for one or many agents."""
    if not _workspace_enabled(config) or not content.strip():
        return

    agent_names = [agent_name] if isinstance(agent_name, str) else agent_name
    today = datetime.now(UTC).date().isoformat()
    max_file_size = _workspace_max_file_size(config)

    for name in agent_names:
        memory_dir = _daily_log_dir(get_agent_workspace_path(name, storage_path), room_id)
        memory_dir.mkdir(parents=True, exist_ok=True)

        log_path = memory_dir / f"{today}.md"
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        updated = f"{existing}{content}"
        updated_size = len(updated.encode("utf-8"))
        if updated_size > max_file_size:
            msg = f"Daily log write exceeds max file size ({max_file_size} bytes): {log_path.as_posix()}"
            raise ValueError(msg)
        log_path.write_text(updated, encoding="utf-8")


def workspace_context_report(
    agent_name: str,
    storage_path: Path,
    config: Config,
    *,
    room_id: str | None = None,
    is_dm: bool = False,
) -> dict[str, Any]:
    """Build an observability report for workspace context loading."""
    report: dict[str, Any] = {
        "agent_name": agent_name,
        "room_id": room_id,
        "is_dm": is_dm,
        "loaded_files": [],
        "warnings": [],
    }

    if not _workspace_enabled(config):
        report["warnings"].append("Workspace memory is disabled")
        return report

    max_file_size = _workspace_max_file_size(config)
    workspace_dir = get_agent_workspace_path(agent_name, storage_path)

    def inspect(path: Path, *, required: bool, category: str) -> None:
        if not path.exists() or not path.is_file():
            if required:
                report["warnings"].append(f"Missing {category}: {path.relative_to(workspace_dir).as_posix()}")
            return

        size_bytes = path.stat().st_size
        if size_bytes > max_file_size:
            report["warnings"].append(
                f"Skipped oversized {category}: {path.relative_to(workspace_dir).as_posix()} ({size_bytes} bytes)",
            )
            return

        report["loaded_files"].append(
            {
                "filename": path.relative_to(workspace_dir).as_posix(),
                "size_bytes": size_bytes,
                "category": category,
            },
        )

    inspect(workspace_dir / SOUL_FILENAME, required=True, category="soul")
    inspect(workspace_dir / AGENTS_FILENAME, required=True, category="agents")

    if is_dm:
        inspect(workspace_dir / MEMORY_FILENAME, required=False, category="memory")

    today = datetime.now(UTC).date()
    log_dir = _daily_log_dir(workspace_dir, room_id)
    for log_day in (today, today - timedelta(days=1)):
        inspect(log_dir / f"{log_day.isoformat()}.md", required=False, category="daily_log")

    if room_id:
        inspect(workspace_dir / "rooms" / _safe_room_filename(room_id), required=False, category="room")

    return report
