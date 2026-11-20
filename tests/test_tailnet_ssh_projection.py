"""Tests for the files the primary projects into a worker for its sshd."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.tailnet_ssh.projection import authorized_key_lines, write_projection
from mindroom.tailnet_ssh.store import LinkBook, new_link, set_keys

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tailnet_ssh.store import Link

ALICE = "@alice:example.org"
BOB = "@bob:example.org"
ALICE_KEY = "v1:default:user_agent:~@alice:example.org:mind_alice"

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
    return new_link(ALICE, "mind_alice", ALICE_KEY, "mind-alice", now=1.0)


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


def test_authorized_keys_are_restricted_and_merged(tmp_path: Path) -> None:
    """Portal keys come first, then the operator's, each restricted, and a key in both appears once."""
    portal, shared, operator = _public_key("bas@laptop"), _public_key("bas@desk"), _public_key()
    shared_base = " ".join(shared.split()[:2])
    config = _config(tmp_path, {ALICE: [f"{shared_base} from-operator", operator], BOB: [_public_key()]})
    book = LinkBook()
    set_keys(book, ALICE, [portal, shared], now=1.0)
    set_keys(book, BOB, [_public_key()], now=1.0)

    lines = authorized_key_lines(config, book, ALICE)
    assert lines == [f"restrict,pty {portal}", f"restrict,pty {shared}", f"restrict,pty {operator}"]

    projection = tmp_path / "ssh"
    write_projection(projection, _link(), lines)
    written = _lines(projection / "authorized_keys")
    assert written == lines
    assert all(line.startswith("restrict,pty ssh-") for line in written)
    assert stat.S_IMODE((projection / "authorized_keys").stat().st_mode) == 0o644

    assert authorized_key_lines(config, LinkBook(), "@carol:example.org") == []
    assert authorized_key_lines(config, LinkBook(), ALICE) == [
        f"restrict,pty {shared_base} from-operator",
        f"restrict,pty {operator}",
    ]


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
    assert public_line.decode() == link.host_public_key


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
