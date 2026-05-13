"""Tests for MindRoom user service installation helpers."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from mindroom.cli.main import app
from mindroom.services.config import (
    InstallResult,
    ServiceActionResult,
    ServiceManager,
    ServiceStatus,
    build_service_command,
    find_uv,
    install_uv,
)
from mindroom.services.launchd import _generate_plist
from mindroom.services.launchd import _get_log_command as _get_launchd_log_command
from mindroom.services.launchd import _restart_service as _restart_launchd_service
from mindroom.services.launchd import _start_service as _start_launchd_service
from mindroom.services.launchd import _stop_service as _stop_launchd_service
from mindroom.services.manager import get_service_manager
from mindroom.services.runtime import ServiceConfigMissingError, resolve_service_environment
from mindroom.services.systemd import _generate_unit_file, _get_unit_name
from mindroom.services.systemd import _restart_service as _restart_systemd_service
from mindroom.services.systemd import _start_service as _start_systemd_service
from mindroom.services.systemd import _stop_service as _stop_systemd_service

runner = CliRunner(env={"NO_COLOR": "1", "TERM": "dumb"})


def test_build_service_command_runs_mindroom_with_uv_tool(tmp_path: Path) -> None:
    """The service command runs the published MindRoom CLI through uv."""
    uv_path = tmp_path / "uv"
    uv_path.touch()

    command = build_service_command(uv_path)

    assert command == [
        str(uv_path),
        "tool",
        "run",
        "--from",
        "mindroom",
        "mindroom",
        "run",
    ]


def test_find_uv_prefers_extra_paths(tmp_path: Path) -> None:
    """find_uv returns an executable passed through extra_paths first."""
    uv_path = tmp_path / "uv"
    uv_path.touch()
    uv_path.chmod(0o755)

    assert find_uv(extra_paths=[uv_path]) == uv_path


@patch("subprocess.run")
def test_install_uv_success(mock_run: MagicMock) -> None:
    """install_uv reports success when curl and sh both succeed."""
    mock_run.return_value = MagicMock(stdout="install script")

    success, message = install_uv()

    assert success is True
    assert message == "uv installed successfully"
    curl_call, shell_call = mock_run.call_args_list
    assert curl_call.kwargs["text"] is True
    assert isinstance(shell_call.kwargs["input"], str)
    assert shell_call.kwargs["text"] is True


@patch("subprocess.run")
def test_install_uv_failure(mock_run: MagicMock) -> None:
    """install_uv returns a user-facing failure message on subprocess errors."""
    mock_run.side_effect = subprocess.CalledProcessError(1, "curl")

    success, message = install_uv()

    assert success is False
    assert "Failed to install uv" in message


@patch("mindroom.services.manager.platform.system", return_value="Darwin")
def test_get_service_manager_macos(mock_system: MagicMock) -> None:
    """Darwin platforms use the launchd manager."""
    manager = get_service_manager()

    assert isinstance(manager, ServiceManager)
    mock_system.assert_called_once()


@patch("mindroom.services.manager.platform.system", return_value="Linux")
def test_get_service_manager_linux(mock_system: MagicMock) -> None:
    """Linux platforms use the systemd manager."""
    manager = get_service_manager()

    assert isinstance(manager, ServiceManager)
    mock_system.assert_called_once()


@patch("mindroom.services.manager.platform.system", return_value="Windows")
def test_get_service_manager_unsupported(mock_system: MagicMock) -> None:
    """Unsupported platforms fail with a clear RuntimeError."""
    with pytest.raises(RuntimeError, match="Unsupported platform"):
        get_service_manager()

    mock_system.assert_called_once()


def test_systemd_unit_runs_mindroom() -> None:
    """The generated systemd unit starts MindRoom and restarts on failure."""
    unit = _generate_unit_file(
        Path("/usr/bin/uv"),
        {
            "MINDROOM_CONFIG_PATH": "/Users/test/Mind Room/config.yaml",
            "MINDROOM_STORAGE_PATH": "/Users/test/Mind Room/data%root",
            "PATH": "/Users/test/.local/bin:/usr/bin",
        },
    )

    assert _get_unit_name() == "mindroom.service"
    assert "Description=MindRoom" in unit
    assert "ExecStart=/usr/bin/uv tool run --from mindroom mindroom run" in unit
    assert 'Environment="MINDROOM_CONFIG_PATH=/Users/test/Mind Room/config.yaml"' in unit
    assert 'Environment="MINDROOM_STORAGE_PATH=/Users/test/Mind Room/data%%root"' in unit
    assert 'Environment="PATH=/Users/test/.local/bin:/usr/bin"' in unit
    assert "Restart=on-failure" in unit


def test_launchd_plist_runs_mindroom(tmp_path: Path) -> None:
    """The generated launchd plist starts MindRoom and writes logs."""
    service_environment = {
        "MINDROOM_CONFIG_PATH": str(tmp_path / "config.yaml"),
        "MINDROOM_STORAGE_PATH": str(tmp_path / "mindroom_data"),
        "PATH": f"{tmp_path}/bin:/usr/bin",
    }
    plist_data = _generate_plist(tmp_path / "uv", tmp_path, tmp_path / "logs", service_environment)
    rendered = plistlib.dumps(plist_data)

    assert plist_data["Label"] == "chat.mindroom.local"
    assert plist_data["ProgramArguments"] == [
        str(tmp_path / "uv"),
        "tool",
        "run",
        "--from",
        "mindroom",
        "mindroom",
        "run",
    ]
    assert plist_data["EnvironmentVariables"] == service_environment
    assert b"StandardOutPath" in rendered


def test_launchd_log_command_uses_explicit_files() -> None:
    """The launchd log command avoids zsh nomatch failures from an empty glob."""
    command = _get_launchd_log_command()

    assert "*.log" not in command
    assert "stdout.log" in command
    assert "stderr.log" in command


def test_resolve_service_environment_captures_active_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Installed services capture the same config and storage context as the invoking CLI."""
    config_path = tmp_path / "local config.yaml"
    storage_path = tmp_path / "local storage"
    uv_path = tmp_path / "custom uv bin" / "uv"
    uv_path.parent.mkdir()
    uv_path.touch()
    config_path.write_text("agents: {}\n", encoding="utf-8")
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(storage_path))
    monkeypatch.setenv("PATH", "/existing/bin")

    service_environment = resolve_service_environment(uv_path)

    assert service_environment["MINDROOM_CONFIG_PATH"] == str(config_path.resolve())
    assert service_environment["MINDROOM_STORAGE_PATH"] == str(storage_path.resolve())
    path_entries = service_environment["PATH"].split(":")
    assert path_entries[0] == str(uv_path.parent)
    assert str(Path.home() / ".local" / "bin") in path_entries
    assert "/opt/homebrew/bin" in path_entries
    assert "/usr/local/bin" in path_entries
    assert "/existing/bin" in path_entries


def test_resolve_service_environment_requires_existing_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Service install fails before writing a service when no active config exists."""
    config_path = tmp_path / "missing.yaml"
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(config_path))

    with pytest.raises(ServiceConfigMissingError, match="mindroom config init"):
        resolve_service_environment(tmp_path / "uv")


@patch("mindroom.services.systemd.subprocess.run")
def test_systemd_lifecycle_actions_use_systemctl(mock_run: MagicMock, tmp_path: Path) -> None:
    """systemd lifecycle actions preserve the unit and call systemctl."""
    unit_path = tmp_path / "mindroom.service"
    unit_path.touch()
    mock_run.return_value = MagicMock(returncode=0, stderr="")

    with patch("mindroom.services.systemd._get_unit_path", return_value=unit_path):
        assert _start_systemd_service().success is True
        assert _stop_systemd_service().success is True
        assert _restart_systemd_service().success is True

    assert [call.args[0] for call in mock_run.call_args_list] == [
        ["systemctl", "--user", "start", "mindroom.service"],
        ["systemctl", "--user", "stop", "mindroom.service"],
        ["systemctl", "--user", "restart", "mindroom.service"],
    ]


def test_systemd_lifecycle_action_requires_installed_unit(tmp_path: Path) -> None:
    """Starting a missing systemd service should tell users to install first."""
    with patch("mindroom.services.systemd._get_unit_path", return_value=tmp_path / "missing.service"):
        result = _start_systemd_service()

    assert result.success is False
    assert "service install" in result.message


@patch("mindroom.services.launchd.os.getuid", return_value=501)
@patch("mindroom.services.launchd.subprocess.run")
def test_launchd_lifecycle_actions_use_launchctl(
    mock_run: MagicMock,
    mock_getuid: MagicMock,
    tmp_path: Path,
) -> None:
    """launchd lifecycle actions load and unload the existing plist."""
    plist_path = tmp_path / "chat.mindroom.local.plist"
    plist_path.touch()
    mock_run.return_value = MagicMock(returncode=0, stderr="")

    status = ServiceStatus(installed=True, running=True, pid=123)
    with (
        patch("mindroom.services.launchd._get_plist_path", return_value=plist_path),
        patch("mindroom.services.launchd._get_service_status", return_value=status),
    ):
        assert _start_launchd_service().success is True
        assert _stop_launchd_service().success is True
        assert _restart_launchd_service().success is True

    assert mock_getuid.call_count == 2
    assert [call.args[0] for call in mock_run.call_args_list] == [
        ["launchctl", "bootout", "gui/501", str(plist_path)],
        ["launchctl", "bootout", "gui/501", str(plist_path)],
        ["launchctl", "bootstrap", "gui/501", str(plist_path)],
    ]


@patch("mindroom.services.launchd.subprocess.run")
def test_launchd_start_is_idempotent_when_already_running(mock_run: MagicMock, tmp_path: Path) -> None:
    """Starting an already running launchd service should not re-bootstrap it."""
    plist_path = tmp_path / "chat.mindroom.local.plist"
    plist_path.touch()
    status = ServiceStatus(installed=True, running=True, pid=123)

    with (
        patch("mindroom.services.launchd._get_plist_path", return_value=plist_path),
        patch("mindroom.services.launchd._get_service_status", return_value=status),
    ):
        result = _start_launchd_service()

    assert result.success is True
    assert result.message == "Service already running"
    mock_run.assert_not_called()


@patch("mindroom.services.launchd.os.getuid", return_value=501)
@patch("mindroom.services.launchd.subprocess.run")
def test_launchd_restart_starts_stopped_service(
    mock_run: MagicMock,
    mock_getuid: MagicMock,
    tmp_path: Path,
) -> None:
    """Restarting a stopped launchd service should start it without requiring bootout."""
    plist_path = tmp_path / "chat.mindroom.local.plist"
    plist_path.touch()
    mock_run.return_value = MagicMock(returncode=0, stderr="")
    status = ServiceStatus(installed=True, running=False)

    with (
        patch("mindroom.services.launchd._get_plist_path", return_value=plist_path),
        patch("mindroom.services.launchd._get_service_status", return_value=status),
    ):
        result = _restart_launchd_service()

    assert result.success is True
    mock_getuid.assert_called_once_with()
    assert [call.args[0] for call in mock_run.call_args_list] == [
        ["launchctl", "bootstrap", "gui/501", str(plist_path)],
    ]


def test_launchd_lifecycle_action_requires_installed_plist(tmp_path: Path) -> None:
    """Starting a missing launchd service should tell users to install first."""
    with patch("mindroom.services.launchd._get_plist_path", return_value=tmp_path / "missing.plist"):
        result = _start_launchd_service()

    assert result.success is False
    assert "service install" in result.message


def test_service_help_is_registered() -> None:
    """The top-level CLI exposes the service command group."""
    result = runner.invoke(app, ["service", "--help"])

    assert result.exit_code == 0
    assert "install" in result.output
    assert "start" in result.output
    assert "stop" in result.output
    assert "restart" in result.output
    assert "uninstall" in result.output
    assert "status" in result.output


@patch("mindroom.cli.service._get_service_manager")
def test_service_status_not_installed(mock_get_manager: MagicMock) -> None:
    """Service status renders a not-installed service without logs."""
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.get_service_status.return_value = ServiceStatus(installed=False, running=False)
    mock_manager.get_log_command.return_value = "tail logs"
    mock_get_manager.return_value = mock_manager

    result = runner.invoke(app, ["service", "status"])

    assert result.exit_code == 0
    assert "not installed" in result.output


@patch("mindroom.cli.service._get_service_manager")
def test_service_install_no_confirm(mock_get_manager: MagicMock) -> None:
    """Service install -y installs without interactive prompts."""
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.check_uv_installed.return_value = (True, Path("/usr/bin/uv"))
    mock_manager.install_service.return_value = InstallResult(success=True, message="Installed and started")
    mock_manager.get_log_command.return_value = "journalctl --user -u mindroom -f"
    mock_get_manager.return_value = mock_manager

    result = runner.invoke(app, ["service", "install", "-y"])

    assert result.exit_code == 0
    assert "Installed and started" in result.output
    mock_manager.install_service.assert_called_once_with()


@patch("mindroom.cli.service._get_service_manager")
def test_service_start_command_succeeds(mock_get_manager: MagicMock) -> None:
    """Service start calls the platform manager start action."""
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.start_service.return_value = ServiceActionResult(success=True, message="Service started")
    mock_get_manager.return_value = mock_manager

    result = runner.invoke(app, ["service", "start"])

    assert result.exit_code == 0
    assert "Service started" in result.output
    mock_manager.start_service.assert_called_once_with()


@patch("mindroom.cli.service._get_service_manager")
def test_service_stop_command_succeeds(mock_get_manager: MagicMock) -> None:
    """Service stop calls the platform manager stop action."""
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.stop_service.return_value = ServiceActionResult(success=True, message="Service stopped")
    mock_get_manager.return_value = mock_manager

    result = runner.invoke(app, ["service", "stop"])

    assert result.exit_code == 0
    assert "Service stopped" in result.output
    mock_manager.stop_service.assert_called_once_with()


@patch("mindroom.cli.service._get_service_manager")
def test_service_restart_command_succeeds(mock_get_manager: MagicMock) -> None:
    """Service restart calls the platform manager restart action."""
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.restart_service.return_value = ServiceActionResult(success=True, message="Service restarted")
    mock_get_manager.return_value = mock_manager

    result = runner.invoke(app, ["service", "restart"])

    assert result.exit_code == 0
    assert "Service restarted" in result.output
    mock_manager.restart_service.assert_called_once_with()


@patch("mindroom.cli.service._get_service_manager")
def test_service_start_failure_exits_with_message(mock_get_manager: MagicMock) -> None:
    """Service lifecycle failures should print a concise error and exit non-zero."""
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.start_service.return_value = ServiceActionResult(
        success=False,
        message="Service is not installed. Run `mindroom service install` first.",
    )
    mock_get_manager.return_value = mock_manager

    result = runner.invoke(app, ["service", "start"])

    assert result.exit_code == 1
    assert "Service is not installed" in result.output
