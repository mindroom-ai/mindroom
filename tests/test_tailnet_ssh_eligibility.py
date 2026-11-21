"""Tests for tailnet SSH configuration and dedicated worker marks."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mindroom.config.main import Config
from mindroom.config.tailnet_ssh import _normalize_public_key
from mindroom.tool_system import plugins
from mindroom.tool_system.registry import ToolRegistry, builtin_tool_registry
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
    assert _normalize_public_key(f"  {key} me@host\r\n") == f"{key} me@host"
    assert _normalize_public_key(key) == key
    with pytest.raises(ValueError, match="option"):
        _normalize_public_key(f'command="x" {key}')
    with pytest.raises(ValueError, match="private key"):
        _normalize_public_key("-----BEGIN OPENSSH PRIVATE KEY-----")
    with pytest.raises(ValueError, match="private key"):
        _normalize_public_key("-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----")
    # Only the armor line marks a private key; a comment may say anything.
    assert _normalize_public_key(f"{key} PRIVATE KEY backup") == f"{key} PRIVATE KEY backup"
    # OpenSSH decodes the blob strictly, so a character outside base64 makes the key unusable.
    key_type, blob = key.split()
    with pytest.raises(ValueError, match=r"Expected an SSH public key"):
        _normalize_public_key(f"{key_type} {blob[:20]}!{blob[20:]}")
    with pytest.raises(ValueError, match=r"Expected an SSH public key"):
        _normalize_public_key(f"{key_type} {blob[:-1]}")
    with pytest.raises(ValueError, match=r"\."):
        _normalize_public_key("")
    with pytest.raises(ValueError, match=r"\."):
        _normalize_public_key("ssh-ed25519 AAAAnotakey me@host")
    with pytest.raises(ValueError, match=r"\."):
        _normalize_public_key(f'{key} me\ncommand="x" {key}')


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


@pytest.mark.parametrize(
    ("tools", "opted_in", "refused"),
    [
        (["shell", "example_tool"], True, False),
        (["example_tool", "shell"], True, True),
        pytest.param(
            [{"example_tool": {"enable_run_shell_command": False}}, "shell"],
            True,
            True,
            id="authored-disable-can-be-overridden-by-stored-all",
        ),
        (["example_tool", "shell"], False, False),
    ],
)
def test_tailnet_ssh_requires_native_shell_to_win(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: list[str | dict[str, dict[str, bool]]],
    opted_in: bool,
    refused: bool,
) -> None:
    """Only earlier native declarations matter, even if authored settings disable them."""
    entries = dict(builtin_tool_registry().entries)
    entries["example_tool"] = replace(entries["shell"], name="example_tool")
    monkeypatch.setattr(plugins, "candidate_tool_registry", lambda *_: (ToolRegistry(entries), ()))
    data = _data()
    data["agents"]["mine"] = {"display_name": "Mine", "tools": tools, "tailnet_ssh": opted_in}
    paths = test_runtime_paths(tmp_path)

    if refused:
        with pytest.raises(ValueError, match=r"mine.*tailnet_ssh.*example_tool.*run_shell_command"):
            _load(paths, data)
    else:
        assert _load(paths, data).agents["mine"].tailnet_ssh is opted_in


@pytest.mark.parametrize("tools", [["shell", "mcp_example"], ["mcp_example", "shell"]])
def test_tailnet_ssh_allows_a_possible_mcp_collision(tmp_path: Path, tools: list[str]) -> None:
    """A remote shell_command would never shadow the native run_shell_command."""
    data = _data()
    data["mcp_servers"] = {
        "example": {
            "transport": "streamable-http",
            "url": "https://example.org/mcp",
            "tool_prefix": "run",
        },
    }
    data["agents"]["mine"] = {"display_name": "Mine", "tools": tools, "tailnet_ssh": True}
    assert _load(test_runtime_paths(tmp_path), data).agents["mine"].tailnet_ssh


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
