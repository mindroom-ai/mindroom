"""One-time move of the records the primary trusts out of state roots that sandbox runners mount."""

# LEGACY_COMPAT: Primary records kept inside worker-mounted state roots.
# Legacy format: `invited_rooms.json`, `pending_room_invites.json`, and `personal_rooms/<sha256>.json` in `agents/<entity>/`, and `agent_modes.json` in `agents/<agent>/` or `private_instances/<scope>/<agent>/`, which the static-runner sidecar mounts read-write.
# Last legacy release: v2026.10.23; replacement: the next release keeps them at the same relative paths below `tracking/`.
# Handling: before serving, once per storage root, every old file that is a regular file reached without links and holds a valid record moves below `tracking/`, unless a record already exists there; anything else stays behind with a warning, and a receipt stops later starts from reading worker-written entries again.
# Coverage: tests/test_legacy_state_root_records.py::test_startup_moves_valid_records_once,
# tests/test_legacy_state_root_records.py::test_planted_entries_stay_behind_without_stopping_startup.

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger
from mindroom.matrix.personal_room_store import PersonalRoomRecord, personal_room_digest
from mindroom.path_confinement import read_regular_file_within_root, write_file_within_root

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.constants import RuntimePaths

_RECEIPT = "state_root_records_moved.json"
_MAX_RECORD_BYTES = 4 << 20
logger = get_logger(__name__)


async def migrate_state_root_records(runtime_paths: RuntimePaths) -> None:
    """Finish the one-time move before any bot reads these records."""
    await run_blocking_until_complete(_migrate, runtime_paths)


def _migrate(runtime_paths: RuntimePaths) -> None:
    records_root = tracking_dir(runtime_paths)
    receipt = records_root / _RECEIPT
    if receipt.exists():
        return
    storage_root = runtime_paths.storage_root
    for relative in _legacy_records(storage_root):
        payload = _read_valid(storage_root, relative)
        if payload is None:
            continue
        if not (records_root / relative).exists():
            write_file_within_root(records_root, relative, payload)
        try:
            (storage_root / relative).unlink(missing_ok=True)
        except OSError as error:
            logger.warning("Left a moved record in a worker-mounted state root", path=str(relative), error=str(error))
    write_json_file_durable(receipt, {"version": 1}, strict_atomic_replace=True)


def _legacy_records(storage_root: Path) -> Iterator[Path]:
    for agent in _directories(storage_root / "agents"):
        base = Path("agents", agent.name)
        yield from (base / name for name in ("invited_rooms.json", "pending_room_invites.json", "agent_modes.json"))
        records = (path.name for path in _children(agent / "personal_rooms") if path.suffix == ".json")
        yield from (base / "personal_rooms" / name for name in records)
    for scope in _directories(storage_root / "private_instances"):
        yield from (
            Path("private_instances", scope.name, agent.name, "agent_modes.json") for agent in _directories(scope)
        )


def _directories(directory: Path) -> list[Path]:
    return [child for child in _children(directory) if not child.is_symlink() and child.is_dir()]


def _children(directory: Path) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        return []
    try:
        return sorted(directory.iterdir())
    except OSError as error:
        logger.warning("Skipped an unlistable worker-mounted directory", path=str(directory), error=str(error))
        return []


def _read_valid(storage_root: Path, relative: Path) -> bytes | None:
    """Read one old record without following links or blocking, and only if it is valid."""
    try:
        payload = read_regular_file_within_root(storage_root, relative, max_bytes=_MAX_RECORD_BYTES)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        logger.warning("Left an unreadable record in a worker-mounted state root", path=str(relative), error=str(error))
        return None
    try:
        if relative.parent.name == "personal_rooms":
            valid = (
                relative.name == f"{personal_room_digest(PersonalRoomRecord.model_validate_json(payload).user_id)}.json"
            )
        else:
            value = json.loads(payload)
            if relative.name == "invited_rooms.json":
                valid = isinstance(value, list) and all(isinstance(room_id, str) for room_id in value)
            elif relative.name == "pending_room_invites.json":
                valid = isinstance(value, dict) and all(isinstance(inviter, str) for inviter in value.values())
            else:
                # The agent-mode reader keeps only the valid choices of an object.
                valid = isinstance(value, dict)
    except (RecursionError, ValueError):
        valid = False
    if not valid:
        logger.warning("Left an invalid record in a worker-mounted state root", path=str(relative))
        return None
    return payload
