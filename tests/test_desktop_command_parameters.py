"""Tests for strict, bounded desktop command parameters."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.desktop.filesystem import DesktopFilesystem
from tests.desktop_bridge_helpers import (
    APP_ID,
    PRIVATE_COMMAND,
    _command,
    _event,
    _handle,
    _local_bridge,
    _local_shell,
    _response,
    _root_id,
)
from tests.desktop_bridge_helpers import selected_root as selected_root  # noqa: PLC0414
from tests.desktop_bridge_helpers import transport as transport  # noqa: PLC0414

if TYPE_CHECKING:
    from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_long_local_paths_reach_folder_reads_and_shell_cwd(transport: AsyncMock, tmp_path: Path) -> None:
    """Paths inside selected folders and working directories are not limited to identifier length."""
    nested = Path(*["d" * 60] * 5)
    root = (tmp_path / "selected").resolve()
    (root / nested).mkdir(parents=True)
    (root / nested / "note.txt").write_text("deep", encoding="utf-8")
    files = DesktopFilesystem((root,))
    shell = _local_shell()
    shell.grant(60)
    bridge = _local_bridge(filesystem=files, shell=shell)
    read = _command("read_file", parameters={"root_id": _root_id(files), "path": str(nested / "note.txt")})
    await _handle(bridge, _event(read))
    assert _response(transport).result["text"] == "deep"
    run = _command("run_shell", request_id="r2", sequence=2, parameters={"command": "pwd", "cwd": str(root / nested)})
    await _handle(bridge, _event(run))
    assert _response(transport).result["output"] == f"{root / nested}\n"
    await bridge.stop()
    bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "parameters", "error"),
    [
        ("list_folders", {"observation": "both"}, "Unexpected desktop parameters: observation."),
        ("list_directory", {"offset": 0}, "Unexpected desktop parameters: offset."),
        ("read_file", {"path": "note.txt", "app": APP_ID}, "Unexpected desktop parameters: app."),
        ("read_file", {"path": "note.txt", "offset": "8"}, "Desktop parameter offset must be an integer."),
        ("run_shell", {"command": PRIVATE_COMMAND, "app": APP_ID}, "Unexpected desktop parameters: app."),
        (
            "run_shell",
            {"command": PRIVATE_COMMAND, "observation": "both"},
            "Unexpected desktop parameters: observation.",
        ),
        (
            "run_shell",
            {"command": PRIVATE_COMMAND, "timeout_seconds": "5"},
            "Desktop parameter timeout_seconds must be an integer.",
        ),
        ("run_shell", {"command": ""}, "Desktop parameter command must be a non-empty string."),
        ("check_shell", {}, "Desktop parameter handle must be a non-empty string."),
        ("check_shell", {"handle": "shell:1", "force": True}, "Unexpected desktop parameters: force."),
        ("kill_shell", {"handle": "shell:1", "force": "yes"}, "Desktop parameter force must be a boolean."),
        ("kill_shell", {"handle": "shell:1", "command": PRIVATE_COMMAND}, "Unexpected desktop parameters: command."),
    ],
)
async def test_local_actions_reject_unrelated_or_malformed_parameters(
    transport: AsyncMock,
    selected_root: Path,
    tmp_path: Path,
    action: str,
    parameters: dict[str, object],
    error: str,
) -> None:
    """Strict parameters are checked before any read or approval request exists."""
    files = DesktopFilesystem((selected_root,))
    shell = _local_shell()
    bridge = _local_bridge(filesystem=files, shell=shell)
    if action in {"list_directory", "read_file"}:
        parameters = {"root_id": _root_id(files), **parameters}
    if action == "run_shell":
        parameters = {**parameters, "cwd": str(tmp_path)}
    await _handle(bridge, _event(_command(action, parameters=parameters)))
    assert _response(transport).error == error
    assert shell.status()["pending"] is None
    assert not (tmp_path / "marker").exists()
    bridge.close()
