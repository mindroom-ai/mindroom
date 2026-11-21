"""Tests for the files the primary projects into a worker for its sshd."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mindroom.tailnet_ssh.projection import write_projection
from mindroom.tailnet_ssh.store import LinkBook

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tailnet_ssh.store import Link


_SPIKE_LINES = [
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
]


def _public_key(comment: str = "") -> str:
    line = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    return f"{line} {comment}".strip()


def _link() -> Link:
    return LinkBook().ensure("v1:default:shared:mind")


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_sshd_config_is_the_spikes(tmp_path: Path) -> None:
    """The projected config holds the spike's lines in order and no path the runner passes as an option."""
    projection = tmp_path / ".runtime" / "ssh"
    write_projection(projection, _link(), [])

    lines = _lines(projection / "sshd_config")
    assert lines == _SPIKE_LINES
    assert not [line for line in lines if line.startswith(("HostKey", "AuthorizedKeysFile", "ForceCommand"))]
    assert stat.S_IMODE((projection / "sshd_config").stat().st_mode) == 0o644
    assert stat.S_IMODE(projection.stat().st_mode) == 0o755


def test_authorized_keys_are_restricted_and_deduplicated(tmp_path: Path) -> None:
    """Keys from all admitted requesters are projected with restrictions, once per key identity."""
    alice, bob = _public_key(), _public_key("other login")
    projection = tmp_path / "ssh"
    write_projection(projection, _link(), [alice, f"{alice} duplicate", bob])
    assert _lines(projection / "authorized_keys") == [f"restrict,pty {alice}", f"restrict,pty {bob}"]
    assert stat.S_IMODE((projection / "authorized_keys").stat().st_mode) == 0o644


def test_host_key_is_0600_and_matches_link(tmp_path: Path) -> None:
    """The host key is the link's own, readable by its owner only, so known_hosts survives worker restarts."""
    link = _link()
    projection = tmp_path / "ssh"
    write_projection(projection, link, [])

    key_path = projection / "ssh_host_ed25519_key"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    private_key = serialization.load_ssh_private_key(key_path.read_bytes(), password=None)
    public_line = private_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    )
    assert public_line.startswith(b"ssh-ed25519 ")
    assert key_path.read_bytes() == link.host_key.encode()


def test_rewrite_replaces_removed_key(tmp_path: Path) -> None:
    """A second write of the same link without a key drops its line, keeps the host key, and leaves no temporary file."""
    kept, removed = _public_key(), _public_key()
    projection = tmp_path / "ssh"
    link = _link()
    write_projection(projection, link, [kept, removed])
    host_key = (projection / "ssh_host_ed25519_key").read_bytes()
    write_projection(projection, link, [kept])

    assert _lines(projection / "authorized_keys") == [f"restrict,pty {kept}"]
    assert (projection / "ssh_host_ed25519_key").read_bytes() == host_key == link.host_key.encode()
    assert sorted(path.name for path in projection.iterdir()) == [
        "authorized_keys",
        "ssh_host_ed25519_key",
        "sshd_config",
    ]
