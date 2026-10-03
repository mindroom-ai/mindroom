"""Tests for MindRoom user service installation helpers."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import typer
from typer.testing import CliRunner

from mindroom.api.auth import dashboard_requires_credential
from mindroom.cli.main import app
from mindroom.cli.service import require_login_service, start_login_service
from mindroom.constants import PROVIDER_ENV_KEYS, RuntimePaths, resolve_primary_runtime_paths
from mindroom.services.config import (
    InstallResult,
    ServiceActionResult,
    ServiceManager,
    ServiceStatus,
    build_service_command,
    find_uv,
    install_service_runtime,
    install_uv,
)
from mindroom.services.launchd import _generate_plist
from mindroom.services.launchd import _get_log_args as _get_launchd_log_args
from mindroom.services.launchd import _get_log_command as _get_launchd_log_command
from mindroom.services.launchd import _get_service_environment as _get_launchd_service_environment
from mindroom.services.launchd import _restart_service as _restart_launchd_service
from mindroom.services.launchd import _start_service as _start_launchd_service
from mindroom.services.launchd import _stop_service as _stop_launchd_service
from mindroom.services.manager import get_service_manager
from mindroom.services.runtime import ServiceConfigMissingError, resolve_service_environment
from mindroom.services.systemd import _generate_unit_file, _get_unit_name
from mindroom.services.systemd import _get_log_args as _get_systemd_log_args
from mindroom.services.systemd import _get_service_environment as _get_systemd_service_environment
from mindroom.services.systemd import _install_service as _install_systemd_service
from mindroom.services.systemd import _restart_service as _restart_systemd_service
from mindroom.services.systemd import _start_service as _start_systemd_service
from mindroom.services.systemd import _stop_service as _stop_systemd_service

runner = CliRunner()


def test_build_service_command_pins_mindroom_version(tmp_path: Path) -> None:
    """The service command must not resolve a newer MindRoom release on restart."""
    uv_path = tmp_path / "uv"
    uv_path.touch()

    command = build_service_command(uv_path, package_version="2026.8.1")

    assert command == [
        str(uv_path),
        "tool",
        "run",
        "--from",
        "mindroom==2026.8.1",
        "mindroom",
        "run",
    ]


@pytest.mark.parametrize(
    ("package_version", "expected_requirement"),
    [
        ("2026.8.1.post1.dev0+g7b7439571.d20260801", "mindroom==2026.8.1"),
        ("2026.8.1rc1.post1.dev0+g7b7439571.d20260801", "mindroom==2026.8.1rc1"),
    ],
)
def test_build_service_command_pins_source_checkout_to_published_version(
    tmp_path: Path,
    package_version: str,
    expected_requirement: str,
) -> None:
    """A VCS development version should resolve to its published predecessor."""
    command = build_service_command(
        tmp_path / "uv",
        package_version=package_version,
    )

    assert command[4] == expected_requirement


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


@pytest.mark.parametrize("error", [subprocess.CalledProcessError(1, "curl"), FileNotFoundError("curl")])
@patch("subprocess.run")
def test_install_uv_failure(mock_run: MagicMock, error: Exception) -> None:
    """install_uv returns a user-facing failure message on subprocess errors and on a machine without curl."""
    mock_run.side_effect = error

    success, message = install_uv()

    assert success is False
    assert "Failed to install uv" in message


@pytest.mark.parametrize(
    ("list_returncode", "installed_tools", "install_returncode", "installs", "expected"),
    [
        (0, "", 0, True, True),
        (0, "mindroom v2026.7.9\n- mindroom\n", 0, True, True),
        (0, "mindroom v2026.8.1\n- mindroom\n", 0, False, True),
        (0, "", 1, True, False),
        (2, "", 0, False, False),
    ],
    ids=["absent", "other-version", "same-version", "install-fails", "list-fails"],
)
def test_install_service_runtime_installs_the_pinned_version_as_a_uv_tool(
    list_returncode: int,
    installed_tools: str,
    install_returncode: int,
    installs: bool,
    expected: bool,
) -> None:
    """The pinned version becomes a persistent uv tool; an installed one, or one that cannot be listed, is left alone."""
    uv_path = Path("/usr/bin/uv")
    run = MagicMock(
        side_effect=[
            subprocess.CompletedProcess(["uv"], list_returncode, stdout=installed_tools),
            subprocess.CompletedProcess(["uv"], install_returncode),
        ],
    )

    with patch("mindroom.services.config.subprocess.run", run):
        assert install_service_runtime(uv_path, package_version="2026.8.1") is expected

    assert run.call_args_list[0].args[0] == [str(uv_path), "tool", "list"]
    if installs:
        assert run.call_args_list[1].args[0] == [str(uv_path), "tool", "install", "mindroom==2026.8.1"]
    else:
        assert run.call_count == 1


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
    with patch("mindroom.services.config.distribution_version", return_value="2026.8.1"):
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
    assert "ExecStart=/usr/bin/uv tool run --from mindroom==2026.8.1 mindroom run" in unit
    assert 'Environment="MINDROOM_CONFIG_PATH=/Users/test/Mind Room/config.yaml"' in unit
    assert 'Environment="MINDROOM_STORAGE_PATH=/Users/test/Mind Room/data%%root"' in unit
    assert 'Environment="PATH=/Users/test/.local/bin:/usr/bin"' in unit
    assert "Restart=on-failure" in unit


@pytest.mark.parametrize(
    "value",
    [
        "/srv/mindroom\nExecStartPre=/bin/sh -c id\n#",
        "/srv/mindroom\rExecStartPre=/bin/sh -c id",
        "/srv/mindroom\x00",
        "/srv/mindroom\x1b[2J",
        "/srv/mindroom\x7f",
    ],
)
def test_systemd_unit_refuses_control_characters_in_environment(value: str) -> None:
    """An Environment= value can never start another unit directive."""
    with (
        patch("mindroom.services.config.distribution_version", return_value="2026.8.1"),
        pytest.raises(ValueError, match="MINDROOM_STORAGE_PATH"),
    ):
        _generate_unit_file(Path("/usr/bin/uv"), {"MINDROOM_STORAGE_PATH": value, "PATH": "/usr/bin"})


def test_systemd_install_reports_an_unwritable_environment_without_touching_the_unit(tmp_path: Path) -> None:
    """Installation fails with a message, and neither writes the unit nor runs systemctl."""
    unit_path = tmp_path / "mindroom.service"
    systemctl = MagicMock()
    with patch.multiple(
        "mindroom.services.systemd",
        find_uv=MagicMock(return_value=Path("/usr/bin/uv")),
        _get_unit_path=MagicMock(return_value=unit_path),
        resolve_service_environment=MagicMock(
            return_value={"MINDROOM_STORAGE_PATH": "/srv/mindroom\nExecStartPre=/bin/id"},
        ),
        subprocess=systemctl,
    ):
        result = _install_systemd_service()

    assert result.success is False
    assert "MINDROOM_STORAGE_PATH" in result.message
    assert not unit_path.exists()
    systemctl.run.assert_not_called()


def test_launchd_plist_runs_mindroom(tmp_path: Path) -> None:
    """The generated launchd plist starts MindRoom and writes logs."""
    service_environment = {
        "MINDROOM_CONFIG_PATH": str(tmp_path / "config.yaml"),
        "MINDROOM_STORAGE_PATH": str(tmp_path / "mindroom_data"),
        "PATH": f"{tmp_path}/bin:/usr/bin",
    }
    with patch("mindroom.services.config.distribution_version", return_value="2026.8.1"):
        plist_data = _generate_plist(tmp_path / "uv", tmp_path, tmp_path / "logs", service_environment)
    rendered = plistlib.dumps(plist_data)

    assert plist_data["Label"] == "chat.mindroom.local"
    assert plist_data["ProgramArguments"] == [
        str(tmp_path / "uv"),
        "tool",
        "run",
        "--from",
        "mindroom==2026.8.1",
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


def test_launchd_log_args_use_explicit_files() -> None:
    """The launchd log args execute the same explicit files without shell parsing."""
    args = _get_launchd_log_args()

    assert args[0:2] == ["tail", "-f"]
    assert str(Path.home() / "Library" / "Logs" / "mindroom" / "stdout.log") in args
    assert str(Path.home() / "Library" / "Logs" / "mindroom" / "stderr.log") in args


def test_systemd_log_args_follow_user_unit() -> None:
    """The systemd log args follow the MindRoom user unit."""
    assert _get_systemd_log_args() == ["journalctl", "--user", "-u", "mindroom", "-f"]


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
    assert service_environment["NO_COLOR"] == "1"
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
    """Systemd lifecycle actions preserve the unit and call systemctl."""
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
    """Missing systemd lifecycle actions should tell users to install first."""
    with patch("mindroom.services.systemd._get_unit_path", return_value=tmp_path / "missing.service"):
        start_result = _start_systemd_service()
        stop_result = _stop_systemd_service()
        restart_result = _restart_systemd_service()

    expected_message = "Service is not installed. Run `mindroom service install` first."
    assert start_result == ServiceActionResult(success=False, message=expected_message)
    assert stop_result == ServiceActionResult(success=False, message=expected_message)
    assert restart_result == ServiceActionResult(success=False, message=expected_message)


@patch("mindroom.services.systemd.subprocess.run")
def test_systemd_lifecycle_actions_propagate_systemctl_errors(mock_run: MagicMock, tmp_path: Path) -> None:
    """Systemd lifecycle actions surface non-zero systemctl results."""
    unit_path = tmp_path / "mindroom.service"
    unit_path.touch()
    mock_run.return_value = MagicMock(returncode=1, stderr="unit failed")

    with patch("mindroom.services.systemd._get_unit_path", return_value=unit_path):
        start_result = _start_systemd_service()
        stop_result = _stop_systemd_service()
        restart_result = _restart_systemd_service()

    assert start_result.success is False
    assert start_result.message == "Failed to start service: unit failed"
    assert stop_result.success is False
    assert stop_result.message == "Failed to stop service: unit failed"
    assert restart_result.success is False
    assert restart_result.message == "Failed to restart service: unit failed"


@patch("mindroom.services.launchd.os.getuid", return_value=501)
@patch("mindroom.services.launchd.subprocess.run")
def test_launchd_lifecycle_actions_use_launchctl(
    mock_run: MagicMock,
    mock_getuid: MagicMock,
    tmp_path: Path,
) -> None:
    """Launchd lifecycle actions load and unload the existing plist."""
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
    """Restarting a stopped launchd service should still clear any loaded job."""
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
        ["launchctl", "bootout", "gui/501", str(plist_path)],
        ["launchctl", "bootstrap", "gui/501", str(plist_path)],
    ]


def test_launchd_lifecycle_action_requires_installed_plist(tmp_path: Path) -> None:
    """Missing launchd lifecycle actions should tell users to install first."""
    with patch("mindroom.services.launchd._get_plist_path", return_value=tmp_path / "missing.plist"):
        start_result = _start_launchd_service()
        stop_result = _stop_launchd_service()
        restart_result = _restart_launchd_service()

    expected_message = "Service is not installed. Run `mindroom service install` first."
    assert start_result == ServiceActionResult(success=False, message=expected_message)
    assert stop_result == ServiceActionResult(success=False, message=expected_message)
    assert restart_result == ServiceActionResult(success=False, message=expected_message)


@patch("mindroom.services.launchd.os.getuid", return_value=501)
@patch("mindroom.services.launchd.subprocess.run")
def test_launchd_lifecycle_actions_propagate_launchctl_errors(
    mock_run: MagicMock,
    mock_getuid: MagicMock,
    tmp_path: Path,
) -> None:
    """Launchd lifecycle actions surface non-zero launchctl results."""
    plist_path = tmp_path / "chat.mindroom.local.plist"
    plist_path.touch()
    status = ServiceStatus(installed=True, running=False)
    mock_run.return_value = MagicMock(returncode=1, stderr="bootstrap failed")

    with (
        patch("mindroom.services.launchd._get_plist_path", return_value=plist_path),
        patch("mindroom.services.launchd._get_service_status", return_value=status),
    ):
        start_result = _start_launchd_service()

    assert start_result.success is False
    assert start_result.message == "Failed to start service: bootstrap failed"
    mock_getuid.assert_called_once_with()


@patch("mindroom.services.launchd.os.getuid", return_value=501)
@patch("mindroom.services.launchd.subprocess.run")
def test_launchd_stop_service_propagates_launchctl_errors(
    mock_run: MagicMock,
    mock_getuid: MagicMock,
    tmp_path: Path,
) -> None:
    """Launchd stop surfaces bootout failures."""
    plist_path = tmp_path / "chat.mindroom.local.plist"
    plist_path.touch()
    status = ServiceStatus(installed=True, running=True, pid=123)
    mock_run.return_value = MagicMock(returncode=1, stderr="bootout failed")

    with (
        patch("mindroom.services.launchd._get_plist_path", return_value=plist_path),
        patch("mindroom.services.launchd._get_service_status", return_value=status),
    ):
        stop_result = _stop_launchd_service()

    assert stop_result.success is False
    assert stop_result.message == "Failed to stop service: bootout failed"
    mock_getuid.assert_called_once_with()


@patch("mindroom.services.launchd.os.getuid", return_value=501)
@patch("mindroom.services.launchd.subprocess.run")
def test_launchd_restart_service_propagates_bootstrap_errors(
    mock_run: MagicMock,
    mock_getuid: MagicMock,
    tmp_path: Path,
) -> None:
    """Launchd restart ignores bootout failures but surfaces bootstrap failures."""
    plist_path = tmp_path / "chat.mindroom.local.plist"
    plist_path.touch()
    status = ServiceStatus(installed=True, running=True, pid=123)
    mock_run.side_effect = [
        MagicMock(returncode=1, stderr="ignored bootout failure"),
        MagicMock(returncode=1, stderr="bootstrap failed"),
    ]

    with (
        patch("mindroom.services.launchd._get_plist_path", return_value=plist_path),
        patch("mindroom.services.launchd._get_service_status", return_value=status),
    ):
        restart_result = _restart_launchd_service()

    assert restart_result.success is False
    assert restart_result.message == "Failed to restart service: bootstrap failed"
    mock_getuid.assert_called_once_with()


@patch("mindroom.services.launchd.subprocess.run")
def test_launchd_stop_service_already_stopped(mock_run: MagicMock, tmp_path: Path) -> None:
    """Stopping an installed but stopped launchd service should be a no-op."""
    plist_path = tmp_path / "chat.mindroom.local.plist"
    plist_path.touch()
    status = ServiceStatus(installed=True, running=False)

    with (
        patch("mindroom.services.launchd._get_plist_path", return_value=plist_path),
        patch("mindroom.services.launchd._get_service_status", return_value=status),
    ):
        result = _stop_launchd_service()

    assert result == ServiceActionResult(success=True, message="Service already stopped")
    mock_run.assert_not_called()


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
    assert "logs" in result.output


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


@pytest.mark.parametrize(("env_file", "pairing_line"), [("", True), ("MATRIX_REGISTRATION_TOKEN=t\n", False)])
@patch("mindroom.cli.service._get_service_manager")
def test_service_status_reports_pending_pairing(
    mock_get_manager: MagicMock,
    tmp_path: Path,
    env_file: str,
    pairing_line: bool,
) -> None:
    """A running service still waiting for pairing is reported so the macOS app does not call it ready."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"MINDROOM_PROVISIONING_URL=https://mindroom.chat\n{env_file}", encoding="utf-8")
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.get_service_status.return_value = ServiceStatus(installed=True, running=True, pid=123)
    mock_manager.get_service_environment.return_value = {
        "MINDROOM_CONFIG_PATH": str(config_path),
        "MINDROOM_STORAGE_PATH": str(tmp_path / "data"),
    }
    mock_manager.get_recent_logs.return_value = []
    mock_manager.get_log_command.return_value = "tail logs"
    mock_get_manager.return_value = mock_manager

    result = runner.invoke(app, ["service", "status"])

    assert result.exit_code == 0, result.output
    assert "MindRoom service: running (pid 123)" in result.output
    assert ("pairing: required" in result.output) is pairing_line


@pytest.mark.parametrize(("service_paired", "pairing_line"), [(False, True), (True, False)])
@patch("mindroom.cli.service._get_service_manager")
def test_service_status_decides_pairing_from_the_installed_service_runtime(
    mock_get_manager: MagicMock,
    tmp_path: Path,
    service_paired: bool,
    pairing_line: bool,
) -> None:
    """The service's saved config and storage decide pairing, not the runtime of whoever runs `service status`."""
    paired_credentials = "MINDROOM_LOCAL_CLIENT_ID=id\nMINDROOM_LOCAL_CLIENT_SECRET=secret\n"
    config_paths: dict[str, Path] = {}
    for name, paired in (("service", service_paired), ("caller", not service_paired)):
        config_dir = tmp_path / name
        config_dir.mkdir()
        config_paths[name] = config_dir / "config.yaml"
        config_paths[name].write_text("agents: {}\n", encoding="utf-8")
        (config_dir / ".env").write_text(
            f"MINDROOM_PROVISIONING_URL=https://mindroom.chat\n{paired_credentials if paired else ''}",
            encoding="utf-8",
        )
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.get_service_status.return_value = ServiceStatus(installed=True, running=True, pid=123)
    mock_manager.get_service_environment.return_value = {
        "MINDROOM_CONFIG_PATH": str(config_paths["service"]),
        "MINDROOM_STORAGE_PATH": str(tmp_path / "service" / "data"),
        "PATH": "/usr/bin",
    }
    mock_manager.get_recent_logs.return_value = []
    mock_manager.get_log_command.return_value = "tail logs"
    mock_get_manager.return_value = mock_manager

    result = runner.invoke(
        app,
        ["service", "status"],
        env={"MINDROOM_CONFIG_PATH": str(config_paths["caller"]), "MINDROOM_STORAGE_PATH": str(tmp_path / "data")},
    )

    assert result.exit_code == 0, result.output
    assert ("pairing: required" in result.output) is pairing_line


@patch("mindroom.cli.service._get_service_manager")
def test_service_status_shows_logs_when_the_service_env_file_is_undecodable(
    mock_get_manager: MagicMock,
    tmp_path: Path,
) -> None:
    """An undecodable service .env stops the service with its own error, so status still reports and shows logs."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_bytes(b"MINDROOM_PROVISIONING_URL=\xff\n")
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.get_service_status.return_value = ServiceStatus(installed=True, running=True, pid=123)
    mock_manager.get_service_environment.return_value = {"MINDROOM_CONFIG_PATH": str(config_path)}
    mock_manager.get_recent_logs.return_value = ["service log line"]
    mock_manager.get_log_command.return_value = "tail logs"
    mock_get_manager.return_value = mock_manager

    result = runner.invoke(app, ["service", "status"])

    assert result.exit_code == 0, result.output
    assert "pairing: required" not in result.output
    assert "service log line" in result.output


def test_systemd_service_environment_reads_the_installed_unit(tmp_path: Path) -> None:
    """The unit's Environment= assignments round-trip, including spaces, quotes, and percent signs."""
    service_environment = {
        "MINDROOM_CONFIG_PATH": '/home/test/Mind Room/"quoted"/config.yaml',
        "MINDROOM_STORAGE_PATH": "/home/test/Mind Room/data%root",
        "PATH": "/home/test/.local/bin:/usr/bin",
    }
    unit_path = tmp_path / "mindroom.service"
    with patch("mindroom.services.config.distribution_version", return_value="2026.8.1"):
        unit_path.write_text(_generate_unit_file(Path("/usr/bin/uv"), service_environment), encoding="utf-8")

    with patch("mindroom.services.systemd._get_unit_path", return_value=unit_path):
        assert _get_systemd_service_environment() == service_environment
    with patch("mindroom.services.systemd._get_unit_path", return_value=tmp_path / "missing.service"):
        assert _get_systemd_service_environment() == {}


def test_launchd_service_environment_reads_the_installed_plist(tmp_path: Path) -> None:
    """The plist's EnvironmentVariables are the service's saved runtime, the same source the macOS app reads."""
    service_environment = {
        "MINDROOM_CONFIG_PATH": str(tmp_path / "config.yaml"),
        "MINDROOM_STORAGE_PATH": str(tmp_path / "mindroom_data"),
    }
    plist_path = tmp_path / "chat.mindroom.local.plist"
    with patch("mindroom.services.config.distribution_version", return_value="2026.8.1"):
        plist_path.write_bytes(
            plistlib.dumps(_generate_plist(tmp_path / "uv", tmp_path, tmp_path / "logs", service_environment)),
        )

    with patch("mindroom.services.launchd._get_plist_path", return_value=plist_path):
        assert _get_launchd_service_environment() == service_environment
    with patch("mindroom.services.launchd._get_plist_path", return_value=tmp_path / "missing.plist"):
        assert _get_launchd_service_environment() == {}


@pytest.mark.parametrize("runtime_installed", [True, False])
@patch("mindroom.cli.service._get_service_manager")
def test_service_install_no_confirm(
    mock_get_manager: MagicMock,
    runtime_installed: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Service install -y installs the pinned version as a uv tool, then the service, without interactive prompts."""
    # Installing saves exported keys next to the active config, which must not be the developer's own.
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(tmp_path / "config.yaml"))
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.check_uv_installed.return_value = (True, Path("/usr/bin/uv"))
    mock_manager.install_runtime.return_value = runtime_installed
    mock_manager.install_service.return_value = InstallResult(success=True, message="Installed and started")
    mock_manager.get_log_command.return_value = "journalctl --user -u mindroom -f"
    mock_get_manager.return_value = mock_manager

    result = runner.invoke(app, ["service", "install", "-y"])

    assert result.exit_code == 0
    assert "Installed and started" in result.output
    assert "After upgrading, rerun mindroom service install" in result.output
    # A failed uv tool install only warns: the service still runs, from uv's cache.
    assert ("Could not install this MindRoom version as a uv tool" in result.output) is not runtime_installed
    assert mock_manager.method_calls[-3:] == [
        call.install_runtime(Path("/usr/bin/uv")),
        call.install_service(),
        call.get_log_command(),
    ]


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


@patch("mindroom.cli.service.subprocess.run")
@patch("mindroom.cli.service._get_service_manager")
def test_service_logs_command_follows_platform_logs(mock_get_manager: MagicMock, mock_run: MagicMock) -> None:
    """Service logs follows the platform-specific log stream command."""
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.get_log_args.return_value = [
        "tail",
        "-f",
        "/var/log/mindroom/stdout.log",
        "/var/log/mindroom/stderr.log",
    ]
    mock_get_manager.return_value = mock_manager
    mock_run.return_value = MagicMock(returncode=0)

    result = runner.invoke(app, ["service", "logs"])

    assert result.exit_code == 0
    mock_manager.get_log_args.assert_called_once_with()
    mock_run.assert_called_once_with(
        ["tail", "-f", "/var/log/mindroom/stdout.log", "/var/log/mindroom/stderr.log"],
        check=False,
    )


@patch("mindroom.cli.service.subprocess.run")
@patch("mindroom.cli.service._get_service_manager")
def test_service_logs_command_propagates_nonzero_exit_code(
    mock_get_manager: MagicMock,
    mock_run: MagicMock,
) -> None:
    """Service logs propagates non-zero exits from the log command."""
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.get_log_args.return_value = ["journalctl", "--user", "-u", "mindroom", "-f"]
    mock_get_manager.return_value = mock_manager
    mock_run.return_value = MagicMock(returncode=2)

    result = runner.invoke(app, ["service", "logs"])

    assert result.exit_code == 2
    mock_manager.get_log_args.assert_called_once_with()
    mock_run.assert_called_once_with(["journalctl", "--user", "-u", "mindroom", "-f"], check=False)


@patch("mindroom.cli.service.subprocess.run")
@patch("mindroom.cli.service._get_service_manager")
def test_service_logs_command_handles_keyboard_interrupt(
    mock_get_manager: MagicMock,
    mock_run: MagicMock,
) -> None:
    """Service logs exits cleanly when the user interrupts log following."""
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.get_log_args.return_value = ["tail", "-f", "/var/log/mindroom/stdout.log"]
    mock_get_manager.return_value = mock_manager
    mock_run.side_effect = KeyboardInterrupt

    result = runner.invoke(app, ["service", "logs"])

    assert result.exit_code == 0
    assert "Aborted" not in result.output


@patch("mindroom.cli.service.subprocess.run")
@patch("mindroom.cli.service._get_service_manager")
def test_service_logs_command_treats_sigint_return_code_as_clean_exit(
    mock_get_manager: MagicMock,
    mock_run: MagicMock,
) -> None:
    """Service logs exits cleanly when the child log process is interrupted."""
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.get_log_args.return_value = ["tail", "-f", "/var/log/mindroom/stdout.log"]
    mock_get_manager.return_value = mock_manager
    mock_run.return_value = MagicMock(returncode=-2)

    result = runner.invoke(app, ["service", "logs"])

    assert result.exit_code == 0
    assert "Aborted" not in result.output


@patch("mindroom.cli.service.subprocess.run")
@patch("mindroom.cli.service._get_service_manager")
def test_service_logs_command_reports_spawn_failure(
    mock_get_manager: MagicMock,
    mock_run: MagicMock,
) -> None:
    """Service logs reports missing log executables without a traceback."""
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.get_log_args.return_value = ["missing-tail", "-f", "/var/log/mindroom/stdout.log"]
    mock_get_manager.return_value = mock_manager
    mock_run.side_effect = FileNotFoundError("missing-tail")

    result = runner.invoke(app, ["service", "logs"])

    assert result.exit_code == 1
    assert "Failed to run log command" in result.output


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


@patch("mindroom.cli.service._get_service_manager")
def test_service_stop_failure_exits_with_message(mock_get_manager: MagicMock) -> None:
    """Service stop failures should print a concise error and exit non-zero."""
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.stop_service.return_value = ServiceActionResult(success=False, message="stop failed")
    mock_get_manager.return_value = mock_manager

    result = runner.invoke(app, ["service", "stop"])

    assert result.exit_code == 1
    assert "stop failed" in result.output
    mock_manager.stop_service.assert_called_once_with()


@patch("mindroom.cli.service._get_service_manager")
def test_service_restart_failure_exits_with_message(mock_get_manager: MagicMock) -> None:
    """Service restart failures should print a concise error and exit non-zero."""
    mock_manager = MagicMock(spec=ServiceManager)
    mock_manager.restart_service.return_value = ServiceActionResult(success=False, message="restart failed")
    mock_get_manager.return_value = mock_manager

    result = runner.invoke(app, ["service", "restart"])

    assert result.exit_code == 1
    assert "restart failed" in result.output
    mock_manager.restart_service.assert_called_once_with()


def _login_service_manager(
    *,
    available: bool = True,
    installed: bool = False,
    uv_installed: bool = True,
    install_result: InstallResult | None = None,
) -> MagicMock:
    manager = MagicMock(spec=ServiceManager)
    manager.description = "systemd user service"
    manager.is_available.return_value = available
    manager.get_service_status.return_value = ServiceStatus(installed=installed, running=installed)
    manager.check_uv_installed.return_value = (uv_installed, Path("/usr/bin/uv") if uv_installed else None)
    manager.install_service.return_value = install_result or InstallResult(
        success=True,
        message="Installed and started",
    )
    manager.get_log_command.return_value = "journalctl --user -u mindroom -f"
    return manager


def _shell_runtime(tmp_path: Path, **process_env: str) -> RuntimePaths:
    # `mindroom run` offers the service only once it has loaded a config.
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return resolve_primary_runtime_paths(config_path=config_path, process_env=process_env)


@pytest.mark.parametrize(
    ("answers", "uv_installed", "install_result"),
    [
        ([False], True, None),
        ([True, False], False, None),
        ([True], True, InstallResult(success=False, message="Failed to start service: no bus")),
    ],
    ids=["declined", "uv-declined", "install-failed"],
)
def test_login_service_question_falls_back_to_the_terminal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    answers: list[bool],
    uv_installed: bool,
    install_result: InstallResult | None,
) -> None:
    """Declining the service or uv, or a failed install, leaves MindRoom to start here and no service behind."""
    manager = _login_service_manager(uv_installed=uv_installed, install_result=install_result)

    with (
        patch("mindroom.cli.service._get_service_manager", return_value=manager),
        patch("mindroom.cli.service._confirm_action", side_effect=answers),
    ):
        assert start_login_service(_shell_runtime(tmp_path, OPENAI_API_KEY="sk-shell"), None) is False

    output = capsys.readouterr()
    if install_result is None:
        manager.uninstall_service.assert_not_called()
        # Shell keys are saved only once a service is about to be installed.
        assert not (tmp_path / ".env").exists()
    else:
        assert "Failed to start service: no bus" in output.err
        assert "Starting MindRoom in this terminal instead." in output.out
        # A half-installed service would otherwise start a second runtime at the next login.
        manager.uninstall_service.assert_called_once_with()
    if not uv_installed:
        manager.install_service.assert_not_called()


@pytest.mark.parametrize("reason", ["unsupported", "unavailable", "installed"])
def test_login_service_question_is_skipped_where_it_cannot_help(tmp_path: Path, reason: str) -> None:
    """An unsupported platform, a machine without systemd, or an existing service is never asked about."""
    manager = _login_service_manager(available=reason != "unavailable", installed=reason == "installed")
    get_manager = MagicMock(return_value=manager)
    if reason == "unsupported":
        get_manager.side_effect = RuntimeError("Unsupported platform")
    confirm = MagicMock()

    with (
        patch("mindroom.cli.service._get_service_manager", get_manager),
        patch("mindroom.cli.service._confirm_action", confirm),
    ):
        assert start_login_service(_shell_runtime(tmp_path), None) is False

    confirm.assert_not_called()
    manager.install_service.assert_not_called()


def test_requested_login_service_saves_usable_shell_provider_keys(tmp_path: Path) -> None:
    """`--service` hands exported provider keys and key files to the service, but never a blank or placeholder value."""
    manager = _login_service_manager()
    runtime_paths = _shell_runtime(
        tmp_path,
        OPENAI_API_KEY="sk-shell",
        GOOGLE_API_KEY_FILE="/run/secrets/google",
        ANTHROPIC_API_KEY="your-anthropic-key-here",
        GROQ_API_KEY="",
    )

    assert start_login_service(runtime_paths, manager) is True

    env_content = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-shell\n" in env_content
    assert "GOOGLE_API_KEY_FILE=/run/secrets/google\n" in env_content
    assert "ANTHROPIC_API_KEY" not in env_content
    assert "GROQ_API_KEY" not in env_content
    manager.install_runtime.assert_called_once_with(Path("/usr/bin/uv"))
    manager.install_service.assert_called_once_with()


@pytest.mark.parametrize("entry_point", ["run --service", "service install"])
@pytest.mark.parametrize(
    "env_line",
    ["MATRIX_HOMESERVER=https://mindroom.chat", "MINDROOM_API_KEY=${MINDROOM_API_KEY}"],
    ids=["other-keys", "shell-reference"],
)
def test_service_install_keeps_the_shell_dashboard_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_point: str,
    env_line: str,
) -> None:
    """A dashboard key only exported in the shell is saved to `.env`, so the service never listens without it.

    A `.env` line that reads the key from the shell does not hold it for the service, which runs without this shell.
    """
    # Quotes, ` #` and a trailing space would change or vanish in an unquoted `.env` line, and `$` must stay literal.
    key = 'shell$key \'dash"board" #key '
    # python-dotenv expands `${NAME}` from this process's environment, which stands in for the installing shell.
    monkeypatch.setenv("MINDROOM_API_KEY", key)
    (tmp_path / ".env").write_text(f"{env_line}\n", encoding="utf-8")
    runtime_paths = _shell_runtime(tmp_path, MINDROOM_API_KEY=key)
    manager = _login_service_manager()

    if entry_point == "run --service":
        assert start_login_service(runtime_paths, manager) is True
    else:
        monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(runtime_paths.config_path))
        with patch("mindroom.cli.service._get_service_manager", return_value=manager):
            result = runner.invoke(app, ["service", "install", "-y"])
        assert result.exit_code == 0, result.output

    manager.install_service.assert_called_once_with()
    # The service sees only its unit's paths and `.env`, never this shell.
    monkeypatch.delenv("MINDROOM_API_KEY")
    service_runtime = resolve_primary_runtime_paths(config_path=runtime_paths.config_path, process_env={})
    assert service_runtime.env_value("MINDROOM_API_KEY") == key
    assert dashboard_requires_credential(service_runtime)


@pytest.mark.parametrize("entry_point", ["run --service", "service install"])
def test_service_install_leaves_env_alone_when_it_already_holds_the_shell_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entry_point: str,
) -> None:
    """Keys `.env` already holds are not saved again, so a symlinked `.env` installs and keeps its own lines."""
    dotfiles_env = tmp_path / "dotfiles" / "mindroom.env"
    dotfiles_env.parent.mkdir()
    env_content = 'export OPENAI_API_KEY="sk-same"  # from dotfiles\nMINDROOM_API_KEY=dash-key\n'
    dotfiles_env.write_text(env_content, encoding="utf-8")
    (tmp_path / ".env").symlink_to(dotfiles_env)
    shell_keys = {"OPENAI_API_KEY": "sk-same", "MINDROOM_API_KEY": "dash-key"}
    runtime_paths = _shell_runtime(tmp_path, **shell_keys)
    manager = _login_service_manager()

    if entry_point == "run --service":
        assert start_login_service(runtime_paths, manager) is True
        output = capsys.readouterr().out
    else:
        for env_key in PROVIDER_ENV_KEYS.values():
            monkeypatch.delenv(env_key, raising=False)
            monkeypatch.delenv(f"{env_key}_FILE", raising=False)
        monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(runtime_paths.config_path))
        for name, value in shell_keys.items():
            monkeypatch.setenv(name, value)
        with patch("mindroom.cli.service._get_service_manager", return_value=manager):
            result = runner.invoke(app, ["service", "install", "-y"])
        assert result.exit_code == 0, result.output
        output = result.output

    assert "from your shell" not in output
    assert dotfiles_env.read_text(encoding="utf-8") == env_content
    manager.install_service.assert_called_once_with()


@pytest.mark.parametrize("entry_point", ["run --service", "service install"])
def test_service_install_refuses_a_shell_key_env_cannot_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entry_point: str,
) -> None:
    """A key that `.env` would expand on reading exits with an error, before anything is saved or installed."""
    key = "shell-${HOME}-key"
    runtime_paths = _shell_runtime(tmp_path, MINDROOM_API_KEY=key)
    env_content = "MATRIX_HOMESERVER=https://mindroom.chat\n"
    (tmp_path / ".env").write_text(env_content, encoding="utf-8")
    manager = _login_service_manager()

    if entry_point == "run --service":
        with pytest.raises(typer.Exit) as exit_info:
            start_login_service(runtime_paths, manager)
        assert exit_info.value.exit_code == 1
        output = capsys.readouterr().err
    else:
        monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(runtime_paths.config_path))
        monkeypatch.setenv("MINDROOM_API_KEY", key)
        with patch("mindroom.cli.service._get_service_manager", return_value=manager):
            result = runner.invoke(app, ["service", "install", "-y"])
        assert result.exit_code == 1
        output = result.output

    assert "Refusing to write MINDROOM_API_KEY to the env file" in output
    assert (tmp_path / ".env").read_text(encoding="utf-8") == env_content
    manager.install_runtime.assert_not_called()
    manager.install_service.assert_not_called()
    manager.uninstall_service.assert_not_called()


@pytest.mark.parametrize("installed", [False, True], ids=["new", "replacing"])
def test_requested_login_service_failure_exits(tmp_path: Path, installed: bool) -> None:
    """A failed `--service` install exits instead of running here, and only removes a service it created."""
    manager = _login_service_manager(
        installed=installed,
        install_result=InstallResult(success=False, message="Failed to start service: no bus"),
    )

    with pytest.raises(typer.Exit) as exit_info:
        start_login_service(_shell_runtime(tmp_path), manager)

    assert exit_info.value.exit_code == 1
    assert manager.uninstall_service.call_count == (0 if installed else 1)


def test_require_login_service_refuses_a_machine_that_cannot_run_it(capsys: pytest.CaptureFixture[str]) -> None:
    """`--service` on a machine without systemd exits with a clear error."""
    manager = _login_service_manager(available=False)

    with (
        patch("mindroom.cli.service._get_service_manager", return_value=manager),
        pytest.raises(typer.Exit) as exit_info,
    ):
        require_login_service()

    assert exit_info.value.exit_code == 1
    assert "This machine cannot run a systemd user service." in capsys.readouterr().err
