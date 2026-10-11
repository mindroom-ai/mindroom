"""Safe command and storage regressions for the local Matrix reset recipe."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def reset_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the real recipe with harmless Docker/rm commands and an isolated config."""
    root = tmp_path / "project"
    root.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / "justfile", root / "justfile")
    (root / "local" / "matrix").mkdir(parents=True)
    (root / "config.yaml").write_text("agents: {}\n")
    fake_bin = root / "bin"
    fake_bin.mkdir()
    for name in ("docker", "rm"):
        command = fake_bin / name
        command.write_text(
            "#!/bin/sh\n"
            f'printf \'%s|{name}|%s\\n\' "$PWD" "$*" >> "$RESET_COMMAND_LOG"\n'
            + ('exit "${RESET_COMPOSE_STATUS:-0}"\n' if name == "docker" else ""),
        )
        command.chmod(0o755)
    # Keep the selected test interpreter and dependencies; do not sync a new environment.
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        '[ "$1" = run ] && [ "$2" = python ] || exit 64\n'
        "shift 2\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n',
    )
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(sys.path))
    monkeypatch.setenv("RESET_COMMAND_LOG", str(root / "commands.log"))
    monkeypatch.delenv("RESET_COMPOSE_STATUS", raising=False)
    monkeypatch.delenv("MINDROOM_CONFIG_PATH", raising=False)
    monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)
    return root


def _reset(root: Path) -> subprocess.CompletedProcess[str]:
    just = shutil.which("just")
    if just is None:
        pytest.fail("Install just to run the reset recipe regressions (included in shell.nix)")
    return subprocess.run([just, "local-matrix-reset"], cwd=root, capture_output=True, text=True, check=False)


def test_local_matrix_reset_only_removes_project_volumes(reset_project: Path) -> None:
    """A local reset must never issue daemon-wide volume cleanup."""
    result = _reset(reset_project)

    assert result.returncode == 0, result.stderr
    commands = (reset_project / "commands.log").read_text().splitlines()
    assert [command for command in commands if "|docker|" in command] == [
        f"{reset_project / 'local' / 'matrix'}|docker|compose down -v",
    ]
    assert [command for command in commands if "|rm|" in command] == [f"{reset_project}|rm|-rf tmp/"]


@pytest.mark.parametrize(
    ("config_path", "process_storage", "dotenv_storage", "selected_storage"),
    [
        (None, None, None, "mindroom_data"),
        ("selected config/config.yaml", None, None, "selected config/mindroom_data"),
        ("selected config/config.yaml", "process storage", None, "process storage"),
        ("selected config/config.yaml", None, "dotenv storage", "selected config/dotenv storage"),
        ("selected config/config.yaml", "process storage", "dotenv storage", "process storage"),
    ],
)
def test_local_matrix_reset_removes_only_selected_matrix_state(
    reset_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_path: str | None,
    process_storage: str | None,
    dotenv_storage: str | None,
    selected_storage: str,
) -> None:
    """Reset must follow runtime path precedence without erasing unrelated data."""
    config = reset_project / (config_path or "config.yaml")
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("agents: {}\n")
    if config_path is not None:
        monkeypatch.setenv("MINDROOM_CONFIG_PATH", config_path)
    if process_storage is not None:
        monkeypatch.setenv("MINDROOM_STORAGE_PATH", process_storage)
    if dotenv_storage is not None:
        (config.parent / ".env").write_text(f"MINDROOM_STORAGE_PATH={dotenv_storage}\n")
    candidates = [
        reset_project,
        reset_project / "mindroom_data",
        reset_project / "selected config" / "mindroom_data",
        reset_project / "selected config" / "dotenv storage",
        reset_project / "process storage",
    ]
    for storage in candidates:
        storage.mkdir(parents=True, exist_ok=True)
        (storage / "matrix_state.yaml").write_text("stale Matrix accounts and rooms\n")
        (storage / "keep.txt").write_text("unrelated persistent data\n")

    result = _reset(reset_project)

    assert result.returncode == 0, result.stderr
    for storage in candidates:
        assert (storage / "matrix_state.yaml").exists() == (storage != reset_project / selected_storage)
        assert (storage / "keep.txt").read_text() == "unrelated persistent data\n"


def test_local_matrix_reset_allows_missing_state(reset_project: Path) -> None:
    """A first reset succeeds without creating a storage directory."""
    result = _reset(reset_project)

    assert result.returncode == 0, result.stderr
    assert not (reset_project / "mindroom_data").exists()


def test_local_matrix_reset_preserves_state_when_compose_fails(
    reset_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local state must survive an unsuccessful Compose teardown."""
    state = reset_project / "mindroom_data" / "matrix_state.yaml"
    state.parent.mkdir()
    state.write_text("existing Matrix accounts and rooms\n")
    monkeypatch.setenv("RESET_COMPOSE_STATUS", "7")

    result = _reset(reset_project)

    assert result.returncode != 0
    assert state.read_text() == "existing Matrix accounts and rooms\n"
    assert (reset_project / "commands.log").read_text().splitlines() == [
        f"{reset_project / 'local' / 'matrix'}|docker|compose down -v",
    ]
