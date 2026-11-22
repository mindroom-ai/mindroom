"""The primary's read-only sshd projection under each worker's ``.runtime/ssh``."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.durable_write import create_directory_durable

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from mindroom.tailnet_ssh.store import Link

_SSHD_CONFIG = b"""Port 2222
PidFile none
UsePAM no
StrictModes yes
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowUsers mindroom
PermitUserEnvironment no
PermitUserRC no
LoginGraceTime 60
Subsystem sftp internal-sftp
AllowTcpForwarding no
AllowStreamLocalForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
GatewayPorts no
"""


def write_projection(projection_dir: Path, link: Link, keys: Iterable[str]) -> None:
    """Atomically replace the worker's files, deduplicating restricted keys by type and blob."""
    unique: dict[str, str] = {}
    for key in keys:
        unique.setdefault(" ".join(key.split()[:2]), key)
    files = (
        ("ssh_host_ed25519_key", link.host_key.encode(), 0o600),
        ("authorized_keys", "".join(f"restrict,pty {line}\n" for line in unique.values()).encode(), 0o644),
        ("sshd_config", _SSHD_CONFIG, 0o644),
    )
    create_directory_durable(projection_dir, mode=0o755)
    directory_fd = os.open(projection_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name, content, mode in files:
            atomic_write_bytes_at(directory_fd, name, content, file_mode=mode, temp_prefix=f".{name}.")
    finally:
        os.close(directory_fd)
