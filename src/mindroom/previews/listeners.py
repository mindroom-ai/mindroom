"""Which TCP sockets listen on this machine's ports, and whether only the worker's own processes hold them (Linux).

The kernel's socket diagnostics report only the listening sockets, however many connections the network namespace holds.
Where the kernel offers none, as under gVisor, ``/proc/net`` answers instead: it lists every connection, and in a Linux
kernel reading it walks the whole connection table that every network namespace on the machine shares.
A socket is the worker's when only processes of the runner's process tree, or of its cgroup, hold it: a dedicated
worker's container, where a server outlives the command that started it, is one cgroup.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_PROC = Path("/proc")
_TCP_LISTEN = 10
# A netlink message header: length, type, flags, sequence number, and port ID, in host byte order.
_NETLINK_HEADER = struct.Struct("=IHHII")
_NETLINK_SOCK_DIAG = 4
_SOCK_DIAG_BY_FAMILY = 20
_NLM_F_REQUEST_DUMP = 0x301
_NLMSG_ERROR = 2
_NLMSG_DONE = 3
# inet_diag_req_v2 up to its socket ID: family, protocol, extensions, padding, and the states to report.
_DIAG_REQUEST = struct.Struct("=BBxxI")
# The socket ID that follows it: source port, destination port, addresses, interface, and cookie, all left to match any.
_DIAG_SOCKET_ID_BYTES = 48
# Offsets into an inet_diag_msg answer.
_DIAG_PORT = 4
_DIAG_ADDRESS = 8
_DIAG_INODE = 68


@dataclass(frozen=True, slots=True)
class _Listener:
    """One TCP socket listening on ``address`` and ``port``, known by its socket inode."""

    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    port: int
    inode: int


def _unmapped(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    # A dual-stack socket bound to an IPv4 address, as Java binds localhost, listens on ::ffff:<address>.
    return (address.ipv4_mapped or address) if isinstance(address, ipaddress.IPv6Address) else address


def _replies(diag: socket.socket) -> Iterator[bytes]:
    """Yield each answer to the dump request just sent, until the kernel says it is done."""
    while True:
        data = diag.recv(65536)
        offset = 0
        while offset < len(data):
            length, kind, _, _, _ = _NETLINK_HEADER.unpack_from(data, offset)
            payload = data[offset + _NETLINK_HEADER.size : offset + length]
            if kind in {_NLMSG_DONE, _NLMSG_ERROR}:
                # Both lead with an error number, negated: an error answer, or a dump the kernel ended in one.
                error = -int.from_bytes(payload[:4], sys.byteorder, signed=True)
                if error:
                    raise OSError(error, os.strerror(error))
                return
            yield payload
            offset += (length + 3) & ~3


def _diagnosed_listeners(port: int | None) -> list[_Listener]:
    """Ask the kernel's socket diagnostics for the TCP sockets listening on ``port``, or on any port for None."""
    found: list[_Listener] = []
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _NETLINK_SOCK_DIAG) as diag:
        for family, address_bytes in ((socket.AF_INET, 4), (socket.AF_INET6, 16)):
            request = _DIAG_REQUEST.pack(family, socket.IPPROTO_TCP, 1 << _TCP_LISTEN) + bytes(_DIAG_SOCKET_ID_BYTES)
            header = _NETLINK_HEADER.pack(
                _NETLINK_HEADER.size + len(request),
                _SOCK_DIAG_BY_FAMILY,
                _NLM_F_REQUEST_DUMP,
                0,
                0,
            )
            diag.sendall(header + request)
            for answer in _replies(diag):
                address = answer[_DIAG_ADDRESS : _DIAG_ADDRESS + address_bytes]
                found.append(
                    _Listener(
                        _unmapped(ipaddress.ip_address(address)),
                        int.from_bytes(answer[_DIAG_PORT : _DIAG_PORT + 2], "big"),
                        int.from_bytes(answer[_DIAG_INODE : _DIAG_INODE + 4], sys.byteorder),
                    ),
                )
    return [listener for listener in found if port is None or listener.port == port]


def _address(hex_words: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Decode a /proc/net address: 32-bit words in hex, each in host byte order."""
    words = (int(hex_words[start : start + 8], 16) for start in range(0, len(hex_words), 8))
    return _unmapped(ipaddress.ip_address(b"".join(word.to_bytes(4, sys.byteorder) for word in words)))


def _proc_net_listeners(port: int | None) -> list[_Listener]:
    """Read the TCP sockets listening on ``port``, or on any port for None, from ``/proc/net``."""
    found: list[_Listener] = []
    tables = 0
    for table in ("tcp", "tcp6"):
        try:
            lines = (_PROC / "net" / table).read_text(encoding="ascii").splitlines()[1:]
        except OSError:  # No IPv6 in this network namespace.
            continue
        tables += 1
        for line in lines:
            _, local, _, state, _, _, _, _, _, inode, *_ = line.split()
            hex_address, _, hex_port = local.partition(":")
            if state == f"{_TCP_LISTEN:02X}" and port in {None, int(hex_port, 16)}:
                found.append(_Listener(_address(hex_address), int(hex_port, 16), int(inode)))
    if not tables:
        msg = "No source names this machine's listening sockets."
        raise OSError(msg)
    return found


def listening_sockets(port: int | None = None) -> list[_Listener]:
    """Return the TCP sockets listening on ``port`` at any address, or on any port for None.

    Blocking. Raises OSError when neither the kernel nor ``/proc/net`` can say.
    """
    try:
        return _diagnosed_listeners(port)
    except OSError:
        return _proc_net_listeners(port)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _parent(pid: int) -> int | None:
    stat = _read(_PROC / str(pid) / "stat")
    # The fields after the name's last parenthesis are plain, whatever the name holds; the parent is the second.
    fields = [] if stat is None else stat[stat.rfind(")") + 1 :].split()
    return int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None


def _holders(sockets: frozenset[int]) -> dict[int, set[int]]:
    """Return the processes holding each of ``sockets``, among those whose descriptors this process may read."""
    wanted = {f"socket:[{inode}]": inode for inode in sockets}
    holders: dict[int, set[int]] = {inode: set() for inode in sockets}
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            descriptors = list((entry / "fd").iterdir())
        except OSError:  # Gone, or another user's.
            continue
        for descriptor in descriptors:
            try:
                target = str(descriptor.readlink())
            except OSError:
                continue
            if target in wanted:
                holders[wanted[target]].add(int(entry.name))
    return holders


def _of_this_worker(pid: int, runner: int, cgroup: str | None) -> bool:
    if cgroup is not None and _read(_PROC / str(pid) / "cgroup") == cgroup:
        return True
    seen: set[int] = set()
    current: int | None = pid
    while current is not None and current > 0 and current not in seen:
        if current == runner:
            return True
        seen.add(current)
        current = _parent(current)
    return False


def held_by_this_worker(sockets: frozenset[int]) -> bool:
    """Return whether processes of this worker, and only they, hold each of ``sockets``.

    Blocking: reads every process's descriptors. Raises OSError when ``/proc`` cannot be read.
    """
    runner = os.getpid()
    cgroup = _read(_PROC / "self" / "cgroup")
    return all(
        pids and all(_of_this_worker(pid, runner, cgroup) for pid in pids) for pids in _holders(sockets).values()
    )
