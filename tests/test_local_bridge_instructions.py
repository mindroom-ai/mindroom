"""Check the bridge manager's printed registration instructions and generated bridge access."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shlex
import socket
import subprocess
import sys
from types import ModuleType
from typing import TYPE_CHECKING

import dotenv
import pytest
import yaml

if TYPE_CHECKING:
    from pathlib import Path
from rich.console import Console
from typer.main import get_command
from typer.testing import CliRunner, Result


@pytest.fixture
def bridge_manager(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the local helper without credentials, network, or subprocess access."""

    def blocked(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Registration instructions must not use sockets or subprocesses")

    monkeypatch.setattr(subprocess, "run", blocked)
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    monkeypatch.setitem(sys.modules, "matty", ModuleType("matty"))
    monkeypatch.syspath_prepend("local/instances/deploy")
    spec = importlib.util.spec_from_file_location("mindroom_bridge_instructions", "local/instances/deploy/bridge.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.console = Console(file=io.StringIO(), record=True, width=200, color_system=None)
    return module


@pytest.mark.parametrize("trailing_newline", ["", "\n"])
def test_tuwunel_instructions_contain_one_complete_registration_message(
    bridge_manager: ModuleType,
    trailing_newline: str,
) -> None:
    """Copying the printed message must retain all YAML and both fence boundaries."""
    registration = "id: example\nnamespaces:\n  aliases: []" + trailing_newline
    bridge = bridge_manager.BridgeConfig(
        bridge_type="telegram",
        instance_name="alpha",
        port=29317,
        data_dir="instance_data/alpha/bridges/telegram",
        matrix_domain="m-alpha.example.com",
    )

    assert bridge_manager._register_with_tuwunel(bridge, registration)

    output = bridge_manager.console.export_text()
    message = re.search(r"^!admin appservices register\n```(?:yaml)?\n(.*?)\n```$", output, re.MULTILINE | re.DOTALL)
    assert message is not None, "No complete command-plus-fenced-YAML message to copy"
    assert message.group(1) == "id: example\nnamespaces:\n  aliases: []"


def test_synapse_instructions_restart_selected_instance(
    bridge_manager: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copying the restart hint must keep the bridge owner's instance selected."""
    synapse_dir = tmp_path / "synapse"
    synapse_dir.mkdir()
    (synapse_dir / "homeserver.yaml").write_text("server_name: m-alpha.example.com\n")
    monkeypatch.setattr(bridge_manager, "load_instances", lambda: {"alpha": {"data_dir": str(tmp_path)}})
    bridge = bridge_manager.BridgeConfig(
        bridge_type="telegram",
        instance_name="alpha",
        port=29317,
        data_dir=str(tmp_path),
    )

    assert bridge_manager._register_with_synapse(bridge, tmp_path / "registration.yaml")

    output = bridge_manager.console.export_text()
    assert "./deploy.py restart alpha --only-matrix" in output


@pytest.mark.parametrize("operation", ["write", "protect", "register"])
def test_bridge_refuses_linked_secrets(
    bridge_manager: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Secret updates and Synapse registration refuse planted links without touching their target."""
    victim = tmp_path / "victim.yaml"
    victim.write_text("server_name: unchanged\n")
    victim.chmod(0o640)
    before = victim.stat()
    parent = tmp_path / ("synapse" if operation == "register" else "data")
    parent.mkdir()
    link = parent / ("homeserver.yaml" if operation == "register" else "config.yaml")
    link.symlink_to(victim)
    monkeypatch.setattr(bridge_manager, "load_instances", lambda: {"alpha": {"data_dir": str(tmp_path)}})
    bridge = bridge_manager.BridgeConfig(
        bridge_type="telegram",
        instance_name="alpha",
        port=29317,
        data_dir=str(tmp_path),
    )

    actions = {
        "write": lambda: bridge_manager._write_private_file(link, "replacement"),
        "protect": lambda: bridge_manager._protect_bridge_secret_files(bridge),
        "register": lambda: bridge_manager._register_with_synapse(bridge, tmp_path / "registration.yaml"),
    }
    with pytest.raises((OSError, ValueError)):
        actions[operation]()

    after = victim.stat()
    assert (after.st_uid, after.st_gid, after.st_mode) == (before.st_uid, before.st_gid, before.st_mode)
    assert victim.read_text() == "server_name: unchanged\n"
    assert link.is_symlink()


def test_tuwunel_start_hint_parses_for_selected_instance(bridge_manager: ModuleType) -> None:
    """The copied start command must use a valid bridge type and the selected instance."""
    bridge = bridge_manager.BridgeConfig(
        bridge_type="telegram",
        instance_name="alpha",
        port=29317,
        data_dir="unused",
    )
    assert bridge_manager._register_with_tuwunel(bridge, "id: example\n")
    output = bridge_manager.console.export_text()
    start_line = next(line.strip() for line in output.splitlines() if line.strip().startswith("./bridge.py start "))
    command = get_command(bridge_manager.app).commands["start"]

    with command.make_context("start", shlex.split(start_line)[2:]) as context:
        assert context.params["bridge_type"] == bridge_manager.BridgeType.TELEGRAM
        assert context.params["instance"] == "alpha"


_BRIDGE_ADMIN = "@alice:m-alpha.example.com"
_BRIDGE_CREDENTIAL_ARGS = {
    "telegram": [
        "--api-id",
        "12345",
        "--api-hash",
        "telegram-hash-for-tests",
        "--bot-token",
        "telegram-token-for-tests",
    ],
    "slack": ["--app-token", "slack-app-for-tests", "--bot-token", "slack-bot-for-tests", "--team-id", "T0TEST"],
}
# Levels each bridge's config loader accepts: legacy mautrix-telegram and bridgev2 mautrix-slack.
_BRIDGE_PERMISSION_LEVELS = {
    "telegram": {"relaybot", "user", "puppeting", "full", "admin"},
    "slack": {"block", "relay", "commands", "user", "admin"},
}
_BRIDGE_RELAY_LEVEL = {"telegram": "relaybot", "slack": "relay"}


def _add_bridge(
    bridge_manager: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
) -> Result:
    monkeypatch.setattr(bridge_manager, "BRIDGE_REGISTRY_FILE", tmp_path / "bridge_instances.json")
    monkeypatch.setattr(
        bridge_manager,
        "load_instances",
        lambda: {"alpha": {"matrix_type": "synapse", "domain": "alpha.example.com", "data_dir": str(tmp_path)}},
    )
    monkeypatch.setattr(bridge_manager, "_find_next_port", lambda *_args: 29317)
    return CliRunner().invoke(bridge_manager.app, ["add", *args, "--instance", "alpha"])


@pytest.mark.parametrize("bridge_type", ["telegram", "slack"])
def test_added_bridge_grants_admin_only_to_the_designated_operator(
    bridge_manager: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bridge_type: str,
) -> None:
    """Self-registered homeserver accounts, including an unreserved @admin, get only the bridge's relay level."""
    credential_args = _BRIDGE_CREDENTIAL_ARGS[bridge_type]
    result = _add_bridge(
        bridge_manager,
        tmp_path,
        monkeypatch,
        bridge_type,
        *credential_args,
        "--admin",
        _BRIDGE_ADMIN,
    )

    assert result.exit_code == 0, result.output
    config_file = tmp_path / "bridges" / bridge_type / "data" / "config.yaml"
    permissions = yaml.safe_load(config_file.read_text())["bridge"]["permissions"]
    assert permissions == {"*": _BRIDGE_RELAY_LEVEL[bridge_type], _BRIDGE_ADMIN: "admin"}
    assert set(permissions.values()) <= _BRIDGE_PERMISSION_LEVELS[bridge_type]
    assert config_file.stat().st_mode & 0o777 == 0o600
    registry = (tmp_path / "bridge_instances.json").read_text()
    assert all(value not in registry for value in credential_args[1::2])


def test_bridge_add_rejects_an_admin_that_is_not_a_matrix_user_id(
    bridge_manager: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare localpart would silently grant nobody, or whoever later registers a matching account."""
    result = _add_bridge(
        bridge_manager,
        tmp_path,
        monkeypatch,
        "telegram",
        *_BRIDGE_CREDENTIAL_ARGS["telegram"],
        "--admin",
        "admin",
    )

    assert result.exit_code == 1
    assert not (tmp_path / "bridges").exists()
    assert not (tmp_path / "bridge_instances.json").exists()


def test_registration_rewrite_keeps_appservice_tokens_owner_only(bridge_manager: ModuleType, tmp_path: Path) -> None:
    """The registration holds the homeserver and appservice tokens, so it must not stay world-readable."""
    registration_file = tmp_path / "registration.yaml"
    registration_file.write_text("id: telegram\nas_token: as-token-for-tests\nurl: http://localhost:29317\n")
    registration_file.chmod(0o644)
    bridge = bridge_manager.BridgeConfig(
        bridge_type="telegram",
        instance_name="alpha",
        port=29317,
        data_dir=str(tmp_path),
    )

    bridge_manager._point_registration_at_bridge_container(bridge, registration_file)

    assert yaml.safe_load(registration_file.read_text()) == {
        "id": "telegram",
        "as_token": "as-token-for-tests",
        "url": "http://alpha-telegram-bridge:29317",
    }
    assert registration_file.stat().st_mode & 0o777 == 0o600


def test_start_strips_tokens_and_world_read_left_by_older_versions(
    bridge_manager: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older registries copied platform tokens, and older bridge files were written at the umask."""
    data_dir = tmp_path / "bridges" / "telegram"
    (data_dir / "data").mkdir(parents=True)
    secret_files = [data_dir / "data" / "config.yaml", data_dir / "data" / "registration.yaml"]
    for path in secret_files:
        path.write_text("as_token: as-token-for-tests\n")
        path.chmod(0o644)
    registry_file = tmp_path / "bridge_instances.json"
    bridge = {
        "bridge_type": "telegram",
        "instance_name": "alpha",
        "port": 29317,
        "data_dir": str(data_dir),
        "credentials": {"bot_token": "telegram-token-for-tests"},
    }
    registry_file.write_text(json.dumps({"bridges": {"alpha": [bridge]}}))
    registry_file.chmod(0o644)
    monkeypatch.setattr(bridge_manager, "BRIDGE_REGISTRY_FILE", registry_file)
    monkeypatch.setattr(
        bridge_manager.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess("docker compose up -d", 0, "", ""),
    )

    result = CliRunner().invoke(bridge_manager.app, ["start", "telegram", "--instance", "alpha"])

    assert result.exit_code == 0, result.output
    assert "telegram-token-for-tests" not in registry_file.read_text()
    assert {path.name: path.stat().st_mode & 0o777 for path in [*secret_files, registry_file]} == {
        "config.yaml": 0o600,
        "registration.yaml": 0o600,
        "bridge_instances.json": 0o600,
    }


def test_owner_only_bridge_files_need_no_read_permission(
    bridge_manager: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Already-private container files need no descriptor or spurious operator warning."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in ("config.yaml", "registration.yaml"):
        path = data_dir / name
        path.write_text("private")
        path.chmod(0o600)
    bridge = bridge_manager.BridgeConfig(
        bridge_type="telegram",
        instance_name="alpha",
        port=29317,
        data_dir=str(tmp_path),
    )

    def denied_open(*_args: object, **_kwargs: object) -> int:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(bridge_manager.os, "open", denied_open)
    bridge_manager._protect_bridge_secret_files(bridge)

    assert bridge_manager.console.export_text() == ""


def test_unrestrictable_bridge_file_asks_for_a_root_rerun(
    bridge_manager: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A readable bridge file the operator cannot chmod asks for a root rerun, never a sudo chmod of a swappable path."""
    config_file = tmp_path / "data" / "config.yaml"
    config_file.parent.mkdir()
    config_file.write_text("bridge: {}\n")
    config_file.chmod(0o644)
    registration_file = tmp_path / "data" / "registration.yaml"
    registration_file.write_text("as_token: as-token-for-tests\n")
    registration_file.chmod(0o600)
    bridge = bridge_manager.BridgeConfig(
        bridge_type="telegram",
        instance_name="alpha",
        port=29317,
        data_dir=str(tmp_path),
    )

    def _refuse_chmod(*_args: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(bridge_manager.os, "fchmod", _refuse_chmod)

    bridge_manager._protect_bridge_secret_files(bridge)

    output = bridge_manager.console.export_text()
    assert "Rerun this bridge.py command as root" in output
    assert "sudo" not in output
    assert "registration.yaml" not in output
