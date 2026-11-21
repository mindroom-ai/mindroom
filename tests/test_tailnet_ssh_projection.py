"""Tests for the files the primary projects into a worker for its sshd."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.tailnet_ssh.projection import authorized_key_lines, clear_authorized_keys, signin_keys, write_projection
from mindroom.tailnet_ssh.store import new_link

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tailnet_ssh.store import Link

ALICE = "@alice:example.org"
BOB = "@bob:example.org"

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


def _config(tmp_path: Path, authorized_keys: dict[str, list[str]]) -> Config:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    data: dict[str, Any] = {
        "models": {"default": {"provider": "openai", "id": "m"}},
        "agents": {},
        "tailnet_ssh": {"authorized_keys": authorized_keys},
    }
    return Config.validate_with_runtime(data, paths)


def _link() -> Link:
    return new_link(ALICE, "mind_alice")


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


def test_authorized_keys_are_config_only_restricted_and_deduplicated(tmp_path: Path) -> None:
    """Only the requester's config keys are projected, once per identity and with restrictions."""
    alice, other_alice, bob = _public_key(), _public_key("other device"), _public_key()
    config = _config(tmp_path, {ALICE: [alice, f"{alice} duplicate", other_alice], BOB: [bob]})
    lines = authorized_key_lines(signin_keys(config, ALICE))
    assert lines == [f"restrict,pty {alice}", f"restrict,pty {other_alice}"]
    assert authorized_key_lines(signin_keys(config, BOB)) == [f"restrict,pty {bob}"]
    assert authorized_key_lines(signin_keys(config, "@carol:example.org")) == []

    projection = tmp_path / "ssh"
    write_projection(projection, _link(), lines)
    assert _lines(projection / "authorized_keys") == lines
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
    kept, removed = f"restrict,pty {_public_key()}", f"restrict,pty {_public_key()}"
    projection = tmp_path / "ssh"
    link = _link()
    write_projection(projection, link, [kept, removed])
    host_key = (projection / "ssh_host_ed25519_key").read_bytes()
    write_projection(projection, link, [kept])

    assert _lines(projection / "authorized_keys") == [kept]
    assert (projection / "ssh_host_ed25519_key").read_bytes() == host_key == link.host_key.encode()
    assert sorted(path.name for path in projection.iterdir()) == [
        "authorized_keys",
        "ssh_host_ed25519_key",
        "sshd_config",
    ]


def test_no_keys_writes_empty_authorized_keys(tmp_path: Path) -> None:
    """With no key, the file exists and is empty, so sshd admits nobody."""
    projection = tmp_path / "ssh"
    write_projection(projection, _link(), [f"restrict,pty {_public_key()}"])
    write_projection(projection, _link(), [])

    assert (projection / "authorized_keys").read_bytes() == b""


def test_clear_empties_authorized_keys_only(tmp_path: Path) -> None:
    """A removed link's worker admits nobody; its sshd still has its config and host key, so a restart would not fail."""
    projection = tmp_path / "ssh"
    link = _link()
    write_projection(projection, link, [f"restrict,pty {_public_key()}"])
    config = (projection / "sshd_config").read_bytes()

    clear_authorized_keys(projection)

    assert (projection / "authorized_keys").read_bytes() == b""
    assert stat.S_IMODE((projection / "authorized_keys").stat().st_mode) == 0o644
    assert (projection / "ssh_host_ed25519_key").read_bytes() == link.host_key.encode()
    assert (projection / "sshd_config").read_bytes() == config
    assert sorted(path.name for path in projection.iterdir()) == [
        "authorized_keys",
        "ssh_host_ed25519_key",
        "sshd_config",
    ]
