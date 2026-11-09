"""Who listens on a TCP port of this machine, and whether only the worker's own processes do."""

from __future__ import annotations

import errno
import ipaddress
import os
import socket
import struct
import sys
from typing import TYPE_CHECKING, NoReturn

import pytest

from mindroom.previews import listeners
from mindroom.previews.listeners import held_by_this_worker, listening_sockets

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Listening sockets and their holders are read from Linux.",
)

_WORKER = "0::/\n"
_SIDECAR = "0::/../sidecar.scope\n"
_RUNNER = os.getpid()
_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def _process(
    proc: Path,
    pid: int,
    *,
    parent: int,
    cgroup: str | None,
    sockets: tuple[int, ...] = (),
    name: str = "app",
) -> None:
    """Add a process to the fake ``/proc``, holding each of ``sockets``; a None ``cgroup`` leaves it unreadable."""
    entry = proc / str(pid)
    (entry / "fd").mkdir(parents=True)
    (entry / "stat").write_text(f"{pid} ({name}) S {parent} {pid} {pid} 0 -1 4194560\n")
    if cgroup is not None:
        (entry / "cgroup").write_text(cgroup)
    (entry / "fd" / "0").symlink_to("/dev/null")
    for fd, inode in enumerate(sockets, start=3):
        (entry / "fd" / str(fd)).symlink_to(f"socket:[{inode}]")


def _table_line(address: str, port: int, state: str, inode: int) -> str:
    """One ``/proc/net/tcp`` or ``tcp6`` row: each 32-bit word of the address in hex, in host byte order."""
    packed = ipaddress.ip_address(address).packed
    words = "".join(
        f"{int.from_bytes(packed[start : start + 4], sys.byteorder):08X}" for start in range(0, len(packed), 4)
    )
    return f"   0: {words}:{port:04X} {'0' * len(words)}:0000 {state} 00000000:00000000 00:00000000 00000000  1000 0 {inode} 1\n"


@pytest.fixture
def proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``/proc`` holding the runner, in the worker's cgroup, and nothing else yet."""
    root = tmp_path / "proc"
    (root / "self").mkdir(parents=True)
    (root / "self" / "cgroup").write_text(_WORKER)
    _process(root, _RUNNER, parent=1, cgroup=_WORKER)
    monkeypatch.setattr(listeners, "_PROC", root)
    return root


@pytest.fixture
def no_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the kernel's socket diagnostics unavailable, as under gVisor."""

    def unsupported(_port: int | None) -> NoReturn:
        raise OSError(93, "Protocol not supported")

    monkeypatch.setattr(listeners, "_diagnosed_listeners", unsupported)


@pytest.fixture
def listener() -> Iterator[socket.socket]:
    """A real TCP socket listening at loopback."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        yield server


def _inode(server: socket.socket) -> int:
    return os.fstat(server.fileno()).st_ino


def _found(port: int | None = None) -> list[tuple[str, int, int]]:
    """Return the address, port, and inode of each socket listening on ``port``, or on any port for None."""
    return [(str(found.address), found.port, found.inode) for found in listening_sockets(port)]


def test_the_kernel_names_a_real_listener_and_its_holder(listener: socket.socket) -> None:
    """A socket this process listens on is found on its port, and only there, and is held by the worker."""
    port = listener.getsockname()[1]
    assert _found(port) == [("127.0.0.1", port, _inode(listener))]
    assert ("127.0.0.1", port, _inode(listener)) in _found()
    assert held_by_this_worker(frozenset({_inode(listener)}))


@pytest.mark.usefixtures("no_diagnostics")
def test_proc_net_names_a_real_listener_where_the_kernel_offers_no_diagnostics(listener: socket.socket) -> None:
    """Without socket diagnostics, ``/proc/net`` finds the same socket."""
    port = listener.getsockname()[1]
    assert _found(port) == [("127.0.0.1", port, _inode(listener))]


def test_the_kernel_names_a_real_ipv6_listener() -> None:
    """A socket listening on ``[::1]`` is found with its IPv6 address."""
    with socket.socket(socket.AF_INET6) as server:
        try:
            server.bind(("::1", 0))
        except OSError:
            pytest.skip("No IPv6 on this host.")
        server.listen()
        port = server.getsockname()[1]
        assert _found(port) == [("::1", port, _inode(server))]


@pytest.mark.usefixtures("no_diagnostics")
def test_proc_net_lists_only_listening_sockets(proc: Path) -> None:
    """Rows in other states and on other ports are left out, and an IPv4-mapped address reads as IPv4."""
    (proc / "net").mkdir()
    (proc / "net" / "tcp").write_text(
        _HEADER
        + _table_line("127.0.0.1", 5173, "0A", 11)
        + _table_line("127.0.0.1", 5173, "01", 12)
        + _table_line("0.0.0.0", 8000, "0A", 13),  # noqa: S104 - a fake socket on every address
    )
    (proc / "net" / "tcp6").write_text(_HEADER + _table_line("::ffff:127.0.0.1", 5173, "0A", 14))
    assert _found(5173) == [
        ("127.0.0.1", 5173, 11),
        ("127.0.0.1", 5173, 14),
    ]
    assert {inode for _, _, inode in _found()} == {11, 13, 14}
    # A network namespace without IPv6 has no tcp6 table.
    (proc / "net" / "tcp6").unlink()
    assert _found(5173) == [("127.0.0.1", 5173, 11)]


def test_an_error_the_kernel_answers_a_dump_with_is_raised() -> None:
    """A netlink error answer, as for a family without socket diagnostics, raises its error, so ``/proc/net`` answers."""
    answer = struct.pack("=IHHII", 36, 2, 0, 0, 0) + (-errno.ENOENT).to_bytes(4, sys.byteorder, signed=True) + bytes(16)
    kernel, runner = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    with kernel, runner:
        kernel.send(answer)
        with pytest.raises(FileNotFoundError):
            list(listeners._replies(runner))


@pytest.mark.usefixtures("no_diagnostics")
def test_no_diagnostics_and_no_proc_net_cannot_say(proc: Path) -> None:
    """Without socket diagnostics or ``/proc/net``, finding the listeners fails instead of finding none."""
    assert not (proc / "net").exists()
    with pytest.raises(OSError, match="No source"):
        listening_sockets(5173)


def test_a_socket_the_runners_process_tree_holds_is_the_workers(proc: Path) -> None:
    """A server the runner's child started, in any cgroup, is the worker's."""
    _process(proc, 100, parent=_RUNNER, cgroup=_SIDECAR)
    _process(proc, 101, parent=100, cgroup=_SIDECAR, sockets=(11,))
    assert held_by_this_worker(frozenset({11}))


def test_a_socket_a_process_in_the_runners_cgroup_holds_is_the_workers(proc: Path) -> None:
    """A server that outlived the command that started it, left to init in the worker's container, is the worker's."""
    _process(proc, 200, parent=1, cgroup=_WORKER, sockets=(11,))
    assert held_by_this_worker(frozenset({11}))


@pytest.mark.parametrize(
    ("holders", "sockets"),
    [
        pytest.param({300: (11,)}, {11}, id="a sidecar holds it"),
        pytest.param({100: (11,), 300: (11,)}, {11}, id="a sidecar holds it too"),
        pytest.param({100: (12,), 300: (11,)}, {11, 12}, id="a sidecar holds one of two"),
        pytest.param({}, {11}, id="no visible process holds it"),
    ],
)
def test_a_socket_any_other_process_holds_is_not_the_workers(
    proc: Path,
    holders: dict[int, tuple[int, ...]],
    sockets: set[int],
) -> None:
    """Sockets count only when the worker's processes, and only they, hold each of them."""
    _process(proc, 1, parent=0, cgroup=_SIDECAR)
    _process(proc, 100, parent=_RUNNER, cgroup=_WORKER, sockets=holders.get(100, ()))
    _process(proc, 300, parent=1, cgroup=_SIDECAR, sockets=holders.get(300, ()))
    assert not held_by_this_worker(frozenset(sockets))


def test_a_process_name_cannot_claim_the_runner_as_its_parent(proc: Path) -> None:
    """The parent is read after the name's last parenthesis, so a crafted name neither hides nor fakes it."""
    _process(proc, 100, parent=_RUNNER, cgroup=_SIDECAR, sockets=(11,), name="dev) S 1 (server")
    _process(proc, 300, parent=1, cgroup=_SIDECAR, sockets=(12,), name=f"x) S {_RUNNER} (y")
    assert held_by_this_worker(frozenset({11}))
    assert not held_by_this_worker(frozenset({12}))


def test_unreadable_cgroups_admit_no_process_by_cgroup(proc: Path) -> None:
    """When the runner's cgroup can't be read, only its process tree counts, even for a process whose cgroup can't either."""
    (proc / "self" / "cgroup").unlink()
    _process(proc, 200, parent=1, cgroup=_WORKER, sockets=(11,))
    _process(proc, 300, parent=1, cgroup=None, sockets=(12,))
    _process(proc, 100, parent=_RUNNER, cgroup=None, sockets=(13,))
    assert not held_by_this_worker(frozenset({11}))
    assert not held_by_this_worker(frozenset({12}))
    assert held_by_this_worker(frozenset({13}))


def test_a_parent_loop_ends_outside_the_worker(proc: Path) -> None:
    """Processes whose parents name each other are outside the runner's tree."""
    _process(proc, 300, parent=301, cgroup=_SIDECAR, sockets=(11,))
    _process(proc, 301, parent=300, cgroup=_SIDECAR)
    assert not held_by_this_worker(frozenset({11}))


def test_missing_proc_cannot_say(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``/proc``, telling whose socket it is fails instead of finding no holder."""
    monkeypatch.setattr(listeners, "_PROC", tmp_path / "missing")
    with pytest.raises(FileNotFoundError):
        held_by_this_worker(frozenset({11}))
