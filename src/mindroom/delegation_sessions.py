"""Runtime-owned subagent handles, separate from editable workspace audit exports."""

from __future__ import annotations

import asyncio
import fcntl
import json
import re
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.delegation_state import DelegationChild
from mindroom.delegation_storage import freeze_delegation_storage
from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

_UNAVAILABLE = "Subagent ID is not available in this conversation."
_TERMINAL = frozenset({"completed", "failed", "cancelled", "denied"})


class SubagentSessionError(ValueError):
    """A handle cannot be used by this caller or already owns another turn."""


def _path(subagent_id: str, paths: RuntimePaths) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", subagent_id):
        raise SubagentSessionError(_UNAVAILABLE)
    root = paths.storage_root / "subagent_sessions"
    path = root / f"{subagent_id}.json"
    if (
        root.is_symlink()
        or path.is_symlink()
        or path.with_suffix(".lock").is_symlink()
        or path.with_suffix(".active.lock").is_symlink()
    ):
        msg = "Subagent storage must not use symlinks."
        raise SubagentSessionError(msg)
    return path


def _owner(identity: ToolExecutionIdentity) -> dict[str, object]:
    # A root event and a later thread reply have different raw thread IDs.
    # Their canonical conversation, requester, and transport scope are identical.
    return serialize_tool_execution_identity(replace(identity, thread_id=identity.resolved_thread_id))


def _read(path: Path, owner: ToolExecutionIdentity) -> DelegationChild:
    if not owner.session_id or not path.exists():
        raise SubagentSessionError(_UNAVAILABLE)
    payload = json.loads(path.read_text())
    if payload["owner"] != _owner(owner):
        raise SubagentSessionError(_UNAVAILABLE)
    return DelegationChild(**payload["child"])


async def load_subagent(
    subagent_id: str,
    *,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
) -> DelegationChild:
    """Resolve an authorized conversation handle without claiming another turn."""
    child = await asyncio.to_thread(_read, _path(subagent_id, runtime_paths), owner)
    if child.subagent_id != subagent_id or child.caller_agent_name != owner.agent_name or child.depth != depth + 1:
        raise SubagentSessionError(_UNAVAILABLE)
    if any(name not in config.agents for name in child.storage_bindings):
        msg = "Subagent target or caller is no longer configured."
        raise SubagentSessionError(msg)
    if child.storage_bindings != freeze_delegation_storage(config, child.storage_bindings):
        msg = "Subagent storage scope changed; start a new subagent."
        raise SubagentSessionError(msg)
    if child.status == "running":
        with _recovery_lock(_path(subagent_id, runtime_paths)) as acquired:
            if acquired:
                child = await asyncio.to_thread(_read, _path(subagent_id, runtime_paths), owner)
                if child.status == "running":
                    # Recovery needs the execution driver's exact-run settlement; defer the cycle.
                    from mindroom.delegation_execution import recover_subagent_turn  # noqa: PLC0415

                    await recover_subagent_turn(child, config=config, runtime_paths=runtime_paths)
    return child


@contextmanager
def _recovery_lock(path: Path) -> Iterator[bool]:
    """Recover only while no process is executing or claiming a turn on this handle."""
    with path.with_suffix(".active.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def subagent_liveness(child: DelegationChild, runtime_paths: RuntimePaths) -> AsyncIterator[None]:
    """Hold an OS-released liveness claim from reservation through child settlement."""
    if child.subagent_id is None:
        yield
        return
    path = _path(child.subagent_id, runtime_paths)
    create_directory_durable(path.parent, mode=0o700)
    with path.with_suffix(".active.lock").open("a") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


async def reserve_subagent_turn(
    child: DelegationChild,
    *,
    owner: ToolExecutionIdentity,
    runtime_paths: RuntimePaths,
) -> None:
    """Atomically reserve a fresh turn; only its exact owner may re-enter a paused turn."""
    if child.subagent_id is None:
        return
    path = _path(child.subagent_id, runtime_paths)

    def reserve() -> None:
        create_directory_durable(path.parent, mode=0o700)
        with advisory_file_lock(path.with_suffix(".lock")):
            if path.exists():
                previous = _read(path, owner)
                if previous.delegation_id == child.delegation_id:
                    # The parent snapshot can predate a retry or model switch.
                    child.run_id = previous.run_id
                    child.model_name = previous.model_name
                    return
                if previous.status not in _TERMINAL:
                    msg = "Subagent is busy or awaiting approval. Finish its current turn before sending a follow-up."
                    raise SubagentSessionError(msg)
                if child.previous_delegation_id != previous.delegation_id:
                    msg = "Subagent changed since this follow-up was prepared; retry the follow-up."
                    raise SubagentSessionError(msg)
            elif child.previous_delegation_id is not None:
                raise SubagentSessionError(_UNAVAILABLE)
            write_json_file_durable(path, {"owner": _owner(owner), "child": asdict(child)}, strict_atomic_replace=True)

    await run_blocking_until_complete(reserve)


async def update_subagent_turn(child: DelegationChild, runtime_paths: RuntimePaths) -> None:
    """Publish a turn snapshot before propagating cancellation."""
    await run_blocking_until_complete(update_subagent_turn_sync, child, runtime_paths)


def update_subagent_turn_sync(child: DelegationChild, runtime_paths: RuntimePaths) -> None:
    """Retain the exact attempt before execution; never overwrite a newer follow-up."""
    if child.subagent_id is None:
        return
    path = _path(child.subagent_id, runtime_paths)
    with advisory_file_lock(path.with_suffix(".lock")):
        if not path.exists():
            return
        payload = json.loads(path.read_text())
        if payload["child"]["delegation_id"] != child.delegation_id:
            return
        payload["child"] = asdict(child)
        write_json_file_durable(path, payload, strict_atomic_replace=True)
