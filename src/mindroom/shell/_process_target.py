"""Process-target resolution, Linux session scanning, and verified cgroup v2 scope signalling."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_PROC_ROOT = Path("/proc")
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_SELF_CGROUP_PATH = Path("/proc/self/cgroup")
_SCOPE_UNIT_PREFIX = "mindroom-shell-"
_SCOPE_UNIT_SUFFIX = ".scope"
_DEAD_PROC_STATES = frozenset({"Z", "X", "x"})
_MAX_CGROUP_DEPTH = 16
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR_OPEN_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
_READ_OPEN_FLAGS = os.O_RDONLY | _O_NOFOLLOW
_WRITE_OPEN_FLAGS = os.O_WRONLY | _O_NOFOLLOW


@dataclass(frozen=True)
class ProcessScope:
    """Verified systemd scope metadata attached to one launched shell command."""

    unit_name: str
    slice_name: str
    slice_cgroup_dir: Path | None


def make_scope_unit_name(*, probe: bool = False) -> str:
    """Return a fresh unique systemd scope unit name owned by MindRoom."""
    return f"{_SCOPE_UNIT_PREFIX}{'probe-' if probe else ''}{uuid.uuid4().hex[:16]}{_SCOPE_UNIT_SUFFIX}"


def _parse_cgroup_v2_relpath(text: str) -> Path | None:
    for line in text.splitlines():
        if line.startswith("0::"):
            rel = line[3:].strip()
            if not rel.startswith("/") or "\x00" in rel:
                return None
            path = Path(rel)
            return None if ".." in path.parts else path
    return None


def parse_probe_slice_cgroup_dir(
    stdout: str,
    *,
    probe_unit: str,
    slice_name: str,
    cgroup_root: Path = _CGROUP_ROOT,
) -> Path | None:
    """Derive the verified slice cgroup directory from a probe command's ``/proc/self/cgroup`` output."""
    rel = _parse_cgroup_v2_relpath(stdout)
    if rel is None or rel.name != probe_unit or rel.parent.name != slice_name:
        return None
    return cgroup_root / rel.parent.relative_to("/")


def _parse_proc_stat(raw: str) -> tuple[str, int, int] | None:
    """Parse ``(state, pgid, sid)`` from one Linux ``/proc/<pid>/stat`` line, or ``None`` if malformed."""
    if ")" not in raw:
        return None
    fields = raw.rsplit(")", 1)[1].split()
    if len(fields) < 4:
        return None
    try:
        return fields[0], int(fields[2]), int(fields[3])
    except ValueError:
        return None


def _is_safe_unit_name(name: str, *, prefix: str, suffix: str) -> bool:
    return (
        name.startswith(prefix)
        and name.endswith(suffix)
        and len(name) > len(prefix) + len(suffix)
        and (bool(prefix) or not name.startswith("-"))
        and not any(bad in name for bad in ("/", "\\", "..", "\x00"))
        and not any(ch.isspace() for ch in name)
    )


def _resolve_scope_cgroup_dir(
    scope: ProcessScope | None,
    *,
    cgroup_root: Path = _CGROUP_ROOT,
    self_cgroup_path: Path = _SELF_CGROUP_PATH,
) -> Path | None:
    """Return the verified real cgroup directory for *scope*, or ``None`` if missing or unsafe."""
    if (
        scope is None
        or scope.slice_cgroup_dir is None
        or not _is_safe_unit_name(scope.unit_name, prefix=_SCOPE_UNIT_PREFIX, suffix=_SCOPE_UNIT_SUFFIX)
        or not _is_safe_unit_name(scope.slice_name, prefix="", suffix=".slice")
        or not scope.slice_cgroup_dir.is_absolute()
        or ".." in scope.slice_cgroup_dir.parts
    ):
        return None
    try:
        rel_slice = scope.slice_cgroup_dir.relative_to(cgroup_root)
        root_resolved = cgroup_root.resolve(strict=True)
        self_rel = _parse_cgroup_v2_relpath(self_cgroup_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not rel_slice.parts or self_rel is None:
        return None

    current = cgroup_root
    with contextlib.suppress(OSError):
        for part in (*rel_slice.parts, scope.unit_name):
            current = current / part
            if current.is_symlink() or not current.is_dir():
                return None
        candidate = current.resolve(strict=True)
        slice_resolved = scope.slice_cgroup_dir.resolve(strict=True)
        self_cg = (root_resolved / self_rel.relative_to("/")).resolve()
        if (
            candidate != root_resolved
            and candidate.is_relative_to(root_resolved)
            and candidate.parent == slice_resolved
            and candidate.name == scope.unit_name
            and candidate.parent.name == scope.slice_name
            and candidate != self_cg
            and candidate not in self_cg.parents
        ):
            return candidate
    return None


def _safe_leader_pid(leader_pid: int) -> bool:
    if leader_pid <= 1 or leader_pid == os.getpid() or leader_pid == os.getpgrp():
        return False
    if sys.platform != "linux":
        return True
    with contextlib.suppress(OSError):
        if leader_pid == os.getsid(0):
            return False
    return True


def _read_proc_stat(pid: int, *, proc_root: Path = _PROC_ROOT) -> tuple[str, int, int] | None:
    try:
        parsed = _parse_proc_stat((proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return None if parsed is None or parsed[0] in _DEAD_PROC_STATES else parsed


def _scan_session_members(leader_pid: int, *, proc_root: Path = _PROC_ROOT) -> list[tuple[int, int]]:
    """Return live ``(pid, pgid)`` pairs in Linux POSIX session *leader_pid*, excluding self and zombies."""
    if not _safe_leader_pid(leader_pid):
        return []
    self_pid = os.getpid()
    members: list[tuple[int, int]] = []
    with contextlib.suppress(OSError), os.scandir(proc_root) as entries:
        for entry in entries:
            if entry.name.isascii() and entry.name.isdigit() and (pid := int(entry.name)) > 1 and pid != self_pid:
                parsed = _read_proc_stat(pid, proc_root=proc_root)
                if parsed is not None and parsed[2] == leader_pid:
                    members.append((pid, parsed[1]))
    return members


@contextlib.contextmanager
def _open_dir_fd(path: Path | str, *, parent_fd: int | None = None) -> Iterator[int]:
    fd = os.open(path, _DIR_OPEN_FLAGS) if parent_fd is None else os.open(str(path), _DIR_OPEN_FLAGS, dir_fd=parent_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def _read_dir_file(dir_fd: int, name: str) -> str:
    with os.fdopen(os.open(name, _READ_OPEN_FLAGS, dir_fd=dir_fd), "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _collect_cgroup_pids(dir_fd: int, pids: set[int], *, depth: int = 0) -> None:
    with contextlib.suppress(OSError):
        pids.update(int(t) for t in _read_dir_file(dir_fd, "cgroup.procs").split() if t.isascii() and t.isdigit())
    if depth >= _MAX_CGROUP_DEPTH:
        return
    with contextlib.suppress(OSError), os.scandir(dir_fd) as entries:
        for entry in entries:
            with contextlib.suppress(OSError):
                if entry.is_dir(follow_symlinks=False):
                    with _open_dir_fd(entry.name, parent_fd=dir_fd) as child_fd:
                        _collect_cgroup_pids(child_fd, pids, depth=depth + 1)


def _scope_is_populated(scope_dir: Path) -> bool:
    with contextlib.suppress(OSError), _open_dir_fd(scope_dir) as dir_fd:
        with contextlib.suppress(OSError):
            for line in _read_dir_file(dir_fd, "cgroup.events").splitlines():
                key, _, value = line.partition(" ")
                if key == "populated":
                    return value.strip() == "1"
        pids: set[int] = set()
        _collect_cgroup_pids(dir_fd, pids)
        return bool(pids)
    return False


def _send_signal(sender: Callable[[int, int], None], target_id: int, sig: int) -> bool:
    try:
        sender(target_id, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _kill_scope_cgroup(scope_dir: Path) -> bool:
    with contextlib.suppress(OSError), _open_dir_fd(scope_dir) as dir_fd:
        with contextlib.suppress(OSError):
            with os.fdopen(os.open("cgroup.kill", _WRITE_OPEN_FLAGS, dir_fd=dir_fd), "wb", buffering=0) as handle:
                handle.write(b"1")
            return True
        self_pid, delivered = os.getpid(), False
        for _ in range(2):
            pids: set[int] = set()
            _collect_cgroup_pids(dir_fd, pids)
            targets = [pid for pid in pids if pid > 1 and pid != self_pid]
            if not targets:
                break
            for pid in targets:
                delivered |= _send_signal(os.kill, pid, signal.SIGKILL)
        return delivered
    return False


def _extra_scope_members(scope_dir: Path, *, leader_pid: int) -> list[tuple[int, int, int]]:
    pids: set[int] = set()
    with contextlib.suppress(OSError), _open_dir_fd(scope_dir) as dir_fd:
        _collect_cgroup_pids(dir_fd, pids)
    self_pid, self_pgid = os.getpid(), os.getpgrp()
    return [
        (pid, parsed[1], parsed[2])
        for pid in pids
        if pid > 1
        and pid != self_pid
        and (parsed := _read_proc_stat(pid)) is not None
        and parsed[1] not in (leader_pid, self_pgid)
    ]


def _signal_extra_pgids(members: list[tuple[int, int]], sig: signal.Signals, signaled_pgids: set[int]) -> bool:
    self_pgid, delivered = os.getpgrp(), False
    for _pid, pgid in members:
        if pgid > 1 and pgid != self_pgid and pgid not in signaled_pgids:
            signaled_pgids.add(pgid)
            delivered |= _send_signal(os.killpg, pgid, sig)
    return delivered


def command_processes_exist(leader_pid: int, *, scope: ProcessScope | None = None) -> bool:
    """Return whether any process of the command still exists in its group, Linux session, or verified scope."""
    if not _safe_leader_pid(leader_pid):
        return False
    if _send_signal(os.killpg, leader_pid, 0):
        return True
    if sys.platform != "linux":
        return False
    if _scan_session_members(leader_pid):
        return True
    scope_dir = _resolve_scope_cgroup_dir(scope)
    return scope_dir is not None and _scope_is_populated(scope_dir)


def _signal_scope_extras(
    scope_dir: Path,
    sig: signal.Signals,
    *,
    leader_pid: int,
    signaled_pgids: set[int],
    delivered: bool,
) -> bool:
    extra = _extra_scope_members(scope_dir, leader_pid=leader_pid)
    if sig == signal.SIGKILL:
        if delivered or extra:
            delivered |= _kill_scope_cgroup(scope_dir)
        return delivered
    same_session = [(pid, pgid) for pid, pgid, sid in extra if sid == leader_pid and pgid > 1]
    delivered |= _signal_extra_pgids(same_session, sig, signaled_pgids)
    for pid, pgid, sid in extra:
        if sid != leader_pid or pgid <= 1:
            delivered |= _send_signal(os.kill, pid, sig)
    return delivered


def signal_command(leader_pid: int, sig: signal.Signals, *, scope: ProcessScope | None = None) -> bool:
    """Send *sig* once to *leader_pid*'s group and any extra Linux session/scope members; report delivery."""
    if not _safe_leader_pid(leader_pid):
        return False
    delivered = _send_signal(os.killpg, leader_pid, sig)
    if sys.platform != "linux":
        return delivered
    signaled_pgids = {leader_pid}
    delivered |= _signal_extra_pgids(_scan_session_members(leader_pid), sig, signaled_pgids)
    scope_dir = _resolve_scope_cgroup_dir(scope)
    if scope_dir is None:
        return delivered
    return _signal_scope_extras(
        scope_dir,
        sig,
        leader_pid=leader_pid,
        signaled_pgids=signaled_pgids,
        delivered=delivered,
    )


def kill_command_processes(leader_pid: int, *, scope: ProcessScope | None = None) -> None:
    """Force-kill a command's process group, Linux session members, and verified systemd scope."""
    if not _safe_leader_pid(leader_pid):
        return
    if sys.platform == "linux" and (scope_dir := _resolve_scope_cgroup_dir(scope)) is not None:
        _kill_scope_cgroup(scope_dir)
    _send_signal(os.killpg, leader_pid, signal.SIGKILL)
    if sys.platform != "linux":
        return
    self_pid = os.getpid()
    signaled_pgids = {leader_pid}
    for attempt in range(2):
        members = _scan_session_members(leader_pid)
        if not members:
            break
        _signal_extra_pgids(members, signal.SIGKILL, signaled_pgids)
        for member_pid, pgid in members:
            if member_pid > 1 and member_pid != self_pid and (attempt > 0 or pgid not in signaled_pgids):
                _send_signal(os.kill, member_pid, signal.SIGKILL)
