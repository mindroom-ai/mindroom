"""The primary's read-only sshd projection under each worker's ``.runtime/ssh``."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.durable_write import create_directory_durable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.tailnet_ssh.store import Link

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


def signin_keys(config: Config, requester_id: str) -> dict[str, str]:
    """Return the requester's configured keys, deduplicated by type and blob (not comment)."""
    keys: dict[str, str] = {}
    for line in config.tailnet_ssh.authorized_keys.get(requester_id, []):
        keys.setdefault(" ".join(line.split()[:2]), line)
    return keys


def authorized_key_lines(keys: Mapping[str, str]) -> list[str]:
    """Prefix each deduplicated key with the enforced sshd restrictions."""
    return [f"restrict,pty {line}" for line in keys.values()]


def _text(lines: Sequence[str]) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode()


def _write_files(projection_dir: Path, files: Sequence[tuple[str, bytes, int]]) -> None:
    """Replace each named file of ``projection_dir`` atomically, so sshd never reads half a file."""
    create_directory_durable(projection_dir, mode=0o755)
    directory_fd = os.open(projection_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name, content, mode in files:
            atomic_write_bytes_at(directory_fd, name, content, file_mode=mode, temp_prefix=f".{name}.")
    finally:
        os.close(directory_fd)


def clear_authorized_keys(projection_dir: Path) -> None:
    """Replace ``authorized_keys`` with an empty file, which admits nobody, and leave the other files as they are."""
    _write_files(projection_dir, [("authorized_keys", b"", 0o644)])


def write_projection(projection_dir: Path, link: Link, key_lines: Sequence[str]) -> None:
    """Replace sshd's files atomically; OpenSSH requires mode 0600 on the host key."""
    _write_files(
        projection_dir,
        [
            ("ssh_host_ed25519_key", link.host_key.encode(), 0o600),
            ("authorized_keys", _text(key_lines), 0o644),
            ("sshd_config", _text(_SSHD_CONFIG), 0o644),
        ],
    )
