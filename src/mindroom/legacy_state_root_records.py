"""One-time move of the records the primary trusts out of state roots that sandbox runners mount."""

# LEGACY_COMPAT: Primary records kept inside worker-mounted state roots.
# Legacy format: `invited_rooms.json`, `pending_room_invites.json`, and `personal_rooms/<sha256>.json` in `agents/<entity>/`, and `agent_modes.json` in `agents/<agent>/` or `private_instances/<scope>/<agent>/`, which the static-runner sidecar mounts read-write.
# Last legacy release: v2026.10.34; replacement: the next release keeps them at the same relative paths below `tracking/`.
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
    from collections.abc import Callable, Iterator

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
    for relative, is_valid in _legacy_records(storage_root):
        payload = _read_valid(storage_root, relative, is_valid)
        if payload is None:
            continue
        if not (records_root / relative).exists():
            write_file_within_root(records_root, relative, payload)
        try:
            (storage_root / relative).unlink(missing_ok=True)
        except OSError as error:
            logger.warning("Left a moved record in a worker-mounted state root", path=str(relative), error=str(error))
    write_json_file_durable(receipt, {"version": 1}, strict_atomic_replace=True)


def _legacy_records(storage_root: Path) -> Iterator[tuple[Path, Callable[[Path, bytes], bool]]]:
    for agent in _directories(storage_root / "agents"):
        base = Path("agents", agent.name)
        yield base / "invited_rooms.json", _is_room_list
        yield base / "pending_room_invites.json", _is_invite_map
        yield base / "agent_modes.json", _is_object
        for record in _children(agent / "personal_rooms"):
            if record.suffix == ".json":
                yield base / "personal_rooms" / record.name, _is_personal_room
    for scope in _directories(storage_root / "private_instances"):
        for agent in _directories(scope):
            yield Path("private_instances", scope.name, agent.name, "agent_modes.json"), _is_object


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


def _read_valid(storage_root: Path, relative: Path, is_valid: Callable[[Path, bytes], bool]) -> bytes | None:
    """Read one old record without following links or blocking, and only if it is valid."""
    try:
        payload = read_regular_file_within_root(storage_root, relative, max_bytes=_MAX_RECORD_BYTES)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        logger.warning("Left an unreadable record in a worker-mounted state root", path=str(relative), error=str(error))
        return None
    try:
        valid = is_valid(relative, payload)
    except (RecursionError, ValueError):
        valid = False
    if not valid:
        logger.warning("Left an invalid record in a worker-mounted state root", path=str(relative))
        return None
    return payload


def _json(payload: bytes) -> object:
    # Decode strictly as the runtime readers do; json.loads on bytes also accepts a BOM and UTF-16 or UTF-32 text.
    return json.loads(payload.decode("utf-8"))


def _is_room_list(_relative: Path, payload: bytes) -> bool:
    value = _json(payload)
    return isinstance(value, list) and all(isinstance(room_id, str) for room_id in value)


def _is_invite_map(_relative: Path, payload: bytes) -> bool:
    value = _json(payload)
    return isinstance(value, dict) and all(isinstance(inviter, str) for inviter in value.values())


def _is_object(_relative: Path, payload: bytes) -> bool:
    # The agent-mode reader keeps only the valid choices of an object.
    return isinstance(_json(payload), dict)


def _is_personal_room(relative: Path, payload: bytes) -> bool:
    record = PersonalRoomRecord.model_validate_json(payload)
    return relative.name == f"{personal_room_digest(record.user_id)}.json"
