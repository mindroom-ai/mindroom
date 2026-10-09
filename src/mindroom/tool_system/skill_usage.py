"""Usage records of workspace skills, which the skill learner's archival reads as an inactivity clock.

Records live in ``skills/.usage.json`` inside the workspace, which worker code can write, so they only ever make the
learner edit or archive skills it could already change, and an unreadable file or record reads as absent.
"""

from __future__ import annotations

import json
import threading
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, BaseModel, ValidationError

from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root, read_regular_file_within_root

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

logger = get_logger(__name__)

_USAGE_FILENAME = ".usage.json"
_MAX_USAGE_FILE_BYTES = 1 << 20
# Process-local on purpose: a lock inside the worker-shared workspace could be held by worker code to stall the primary.
_USAGE_LOCK = threading.Lock()


class SkillUsage(BaseModel):
    """Provenance and activity for one workspace skill directory."""

    created_by: Literal["learner"] | None = None
    created_at: AwareDatetime | None = None
    last_used_at: AwareDatetime | None = None
    last_patched_at: AwareDatetime | None = None

    def last_activity_at(self) -> datetime | None:
        """Return the newest creation, use, or ``skill_manage`` edit."""
        return max(filter(None, (self.created_at, self.last_used_at, self.last_patched_at)), default=None)


def _records(root_fd: int) -> dict[str, object] | None:
    """Return the raw records, empty without a file, or None for a file that is not a readable JSON object."""
    try:
        records = json.loads(read_regular_file_within_root(root_fd, _USAGE_FILENAME, max_bytes=_MAX_USAGE_FILE_BYTES))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable skill usage records", error=str(exc))
        return None
    return records if isinstance(records, dict) else None


def _usage(record: object) -> SkillUsage | None:
    with suppress(ValidationError):
        return SkillUsage.model_validate(record)
    return None


def load_skill_usage(root_fd: int) -> dict[str, SkillUsage]:
    """Return usage by skill directory; an unreadable file or record reads as absent."""
    usage = {name: _usage(record) for name, record in (_records(root_fd) or {}).items()}
    return {name: record for name, record in usage.items() if record is not None}


def update_skill_usages(root_fd: int, updates: Mapping[str, Callable[[SkillUsage], SkillUsage]]) -> None:
    """Replace some skills' records in one atomic write; telemetry never fails the change it records."""
    if not updates:
        return
    with _USAGE_LOCK:
        records = _records(root_fd)
        if records is None:
            # Rewriting an unreadable file would drop every record in it; a person can still repair it.
            return
        for name, update in updates.items():
            records[name] = update(_usage(records.get(name)) or SkillUsage()).model_dump(
                mode="json",
                exclude_defaults=True,
            )
        _write(root_fd, records)


def forget_missing_skill_usage(root_fd: int, present: set[str]) -> None:
    """Drop records of skill directories that are gone, so a name reused later starts as a new skill."""
    with _USAGE_LOCK:
        records = _records(root_fd)
        if records is not None and not records.keys() <= present:
            _write(root_fd, {name: record for name, record in records.items() if name in present})


def _write(root_fd: int, records: dict[str, object]) -> None:
    try:
        atomic_write_bytes_at(root_fd, _USAGE_FILENAME, json.dumps(records, separators=(",", ":")).encode())
    except OSError as exc:
        logger.warning("Could not write skill usage records", error=str(exc))


def record_skill_use(skill_path: Path) -> None:
    """Record one agent load of a workspace skill; telemetry failures never fail the load."""
    now = datetime.now(UTC)
    try:
        with open_directory_within_root(skill_path.parent.parent, skill_path.parent.name) as root_fd:
            update_skill_usages(
                root_fd,
                {skill_path.name: lambda usage: usage.model_copy(update={"last_used_at": now})},
            )
    except OSError as exc:
        logger.warning("Could not record workspace skill use", path=str(skill_path), error=str(exc))
