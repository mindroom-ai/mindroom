"""Tests for tailnet SSH configuration and dedicated worker marks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mindroom.config.main import Config
from mindroom.config.tailnet_ssh import normalize_public_key
from mindroom.workers.backends._dedicated_worker_common import tailnet_ssh_marked
from mindroom.workers.runtime import build_worker_launch_config
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ALICE = "@alice:example.org"
BOB = "@bob:example.org"


def _data() -> dict[str, Any]:
    return {"models": {"default": {"provider": "openai", "id": "m"}}, "agents": {}}


def _load(paths: RuntimePaths, data: dict[str, Any]) -> Config:
    return Config.validate_with_runtime(data, paths)


def _public_key() -> str:
    raw = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH,
            serialization.PublicFormat.OpenSSH,
        )
    )
    return raw.decode()


def test_normalize_public_key_shapes() -> None:
    """One public key line is kept; options, private keys, empty lines, and junk are refused with a sentence."""
    key = _public_key()
    assert normalize_public_key(f"  {key} me@host\r\n") == f"{key} me@host"
    assert normalize_public_key(key) == key
    with pytest.raises(ValueError, match="option"):
        normalize_public_key(f'command="x" {key}')
    with pytest.raises(ValueError, match="private key"):
        normalize_public_key("-----BEGIN OPENSSH PRIVATE KEY-----")
    with pytest.raises(ValueError, match="private key"):
        normalize_public_key("-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----")
    # Only the armor line marks a private key; a comment may say anything.
    assert normalize_public_key(f"{key} PRIVATE KEY backup") == f"{key} PRIVATE KEY backup"
    # OpenSSH decodes the blob strictly, so a character outside base64 makes the key unusable.
    key_type, blob = key.split()
    with pytest.raises(ValueError, match=r"Expected an SSH public key"):
        normalize_public_key(f"{key_type} {blob[:20]}!{blob[20:]}")
    with pytest.raises(ValueError, match=r"Expected an SSH public key"):
        normalize_public_key(f"{key_type} {blob[:-1]}")
    with pytest.raises(ValueError, match=r"\."):
        normalize_public_key("")
    with pytest.raises(ValueError, match=r"\."):
        normalize_public_key("ssh-ed25519 AAAAnotakey me@host")
    with pytest.raises(ValueError, match=r"\."):
        normalize_public_key(f'{key} me\ncommand="x" {key}')


def test_config_authorized_keys_are_normalized_and_keyed_by_concrete_ids(tmp_path: Path) -> None:
    """Operator keys are stored normalized, and every person is named by a concrete Matrix ID."""
    key = _public_key()
    data = _data()
    data["tailnet_ssh"] = {"logins": {ALICE: "alice@github"}, "authorized_keys": {ALICE: [f"{key} me@host\r\n"]}}
    config = _load(test_runtime_paths(tmp_path), data)
    assert config.tailnet_ssh.authorized_keys == {ALICE: [f"{key} me@host"]}
    assert config.tailnet_ssh.logins == {ALICE: "alice@github"}

    for section in (
        {"authorized_keys": {"@*:example.org": [key]}},
        {"logins": {"@*:example.org": "everyone@github"}},
        {"logins": {ALICE: ""}},
        {"logins": {ALICE: "  "}},
        {"authorized_keys": {ALICE: ["-----BEGIN OPENSSH PRIVATE KEY-----"]}},
        {"unknown": {}},
    ):
        data["tailnet_ssh"] = section
        with pytest.raises(ValueError, match="tailnet_ssh"):
            _load(test_runtime_paths(tmp_path), data)


@pytest.mark.parametrize(
    ("logins", "detail"),
    [
        ({ALICE: " alice@github"}, "whitespace"),
        ({ALICE: "alice@github "}, "whitespace"),
        ({ALICE: "alice@github", BOB: "alice@github"}, "distinct"),
    ],
)
def test_tailnet_logins_are_trimmed_and_unique(tmp_path: Path, logins: dict[str, str], detail: str) -> None:
    """Reject ambiguous or unreachable login mappings at config load."""
    data = _data()
    data["tailnet_ssh"] = {"logins": logins}
    with pytest.raises(ValueError, match=detail):
        _load(test_runtime_paths(tmp_path), data)


def test_empty_tailnet_ssh_section_is_left_out_of_authored_config(tmp_path: Path) -> None:
    """A null section loads empty and is not written back."""
    data = _data()
    data["tailnet_ssh"] = None
    config = _load(test_runtime_paths(tmp_path), data)
    assert config.tailnet_ssh.logins == {}
    assert "tailnet_ssh" not in config.authored_model_dump()


def test_personal_agent_template_accepts_tailnet_ssh(tmp_path: Path) -> None:
    """An opted-in personal agent template uses ordinary config validation."""
    data = _data()
    data["personal_agents"] = {
        "pa": {"agent": {"display_name": "PA", "worker_scope": "user_agent", "tailnet_ssh": True}},
    }
    config = _load(test_runtime_paths(tmp_path), data)
    assert config.personal_agents["pa"].agent.tailnet_ssh is True


@pytest.mark.parametrize(
    ("scope", "key"),
    [
        ("shared", "v1:default:shared:mine"),
        ("user", "v1:default:user:~@alice:example.org"),
        ("user_agent", "v1:default:user_agent:~@alice:example.org:mine"),
        (None, "v1:default:unscoped:mine"),
    ],
)
def test_opt_in_marks_workers_of_any_scope(tmp_path: Path, scope: str | None, key: str) -> None:
    """Access rules do not drive provisioning, and normal name normalization still applies."""
    data = _data()
    data["agents"]["mine_"] = {"display_name": "Mine", "worker_scope": scope, "tailnet_ssh": True}
    paths = test_runtime_paths(tmp_path)
    assert tailnet_ssh_marked(key, build_worker_launch_config(_load(paths, data), tool_validation_snapshot={}), None)
    data["agents"]["mine_"]["tailnet_ssh"] = False
    assert not tailnet_ssh_marked(
        key,
        build_worker_launch_config(_load(paths, data), tool_validation_snapshot={}),
        None,
    )
