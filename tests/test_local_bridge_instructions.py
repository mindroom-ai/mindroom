"""Check that printed registration instructions preserve their executable payloads."""

from __future__ import annotations

import importlib.util
import io
import re
import shlex
import socket
import subprocess
import sys
from types import ModuleType
from typing import TYPE_CHECKING

import dotenv
import pytest

if TYPE_CHECKING:
    from pathlib import Path
from rich.console import Console
from typer.main import get_command


@pytest.fixture
def bridge_manager(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the local helper without credentials, network, or subprocess access."""

    def blocked(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Registration instructions must not use sockets or subprocesses")

    monkeypatch.setattr(subprocess, "run", blocked)
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    monkeypatch.setitem(sys.modules, "matty", ModuleType("matty"))
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
