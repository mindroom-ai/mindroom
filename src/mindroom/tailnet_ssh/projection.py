"""The files a worker's sshd reads, written by the primary into ``<worker root>/.runtime/ssh/``.

``.runtime`` is mounted read-only into the worker, so worker code cannot rewrite them.
The runner passes ``HostKey``, ``AuthorizedKeysFile`` and ``ForceCommand`` to sshd as options,
because only the worker knows its mount path and interpreter, so ``sshd_config`` names no path.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.durable_write import create_directory_durable
from mindroom.tailnet_ssh.store import key_identity

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.tailnet_ssh.store import Link, LinkBook

_SSHD_CONFIG = (
    "Port 2222",
    "PidFile none",
    "UsePAM no",
    "StrictModes yes",
    "PubkeyAuthentication yes",
    "PasswordAuthentication no",
    "KbdInteractiveAuthentication no",
    "AllowUsers mindroom",
    "PermitUserEnvironment no",
    "PermitUserRC no",
    "LoginGraceTime 60",
    "Subsystem sftp internal-sftp",
    "AllowTcpForwarding no",
    "AllowStreamLocalForwarding no",
    "AllowAgentForwarding no",
    "X11Forwarding no",
    "PermitTunnel no",
    "GatewayPorts no",
)


def authorized_key_lines(config: Config, book: LinkBook, requester_id: str) -> list[str]:
    """Return the person's portal keys, then the operator's, each once and each prefixed ``restrict,pty``.

    A key in both counts once, its portal line kept: two lines are one key when type and base64 match.
    """
    keys: dict[str, str] = {}
    for line in [*book.keys.get(requester_id, []), *config.tailnet_ssh.authorized_keys.get(requester_id, [])]:
        keys.setdefault(key_identity(line), line)
    return [f"restrict,pty {line}" for line in keys.values()]


def _text(lines: Sequence[str]) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode()


def write_projection(projection_dir: Path, link: Link, key_lines: Sequence[str]) -> None:
    """Replace ``sshd_config``, ``authorized_keys`` and the link's host key, each atomically, so sshd never reads half a file.

    sshd's ``StrictModes`` requires the host key to be 0600 and owned by the worker's uid, which the primary's uid is.
    """
    create_directory_durable(projection_dir, mode=0o755)
    directory_fd = os.open(projection_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name, content, mode in (
            ("ssh_host_ed25519_key", link.host_key.encode(), 0o600),
            ("authorized_keys", _text(key_lines), 0o644),
            ("sshd_config", _text(_SSHD_CONFIG), 0o644),
        ):
            atomic_write_bytes_at(directory_fd, name, content, file_mode=mode, temp_prefix=f".{name}.")
    finally:
        os.close(directory_fd)
