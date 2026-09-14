"""Real-environment checks for persistent worker virtualenv creation."""

from __future__ import annotations

import ensurepip
import json
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from mindroom.api.sandbox_exec import resolve_subprocess_worker_context
from mindroom.workers.backends import local as local_workers
from tests.conftest import requires_linux


def _assert_seeded_worker_environment(paths: local_workers.LocalWorkerStatePaths) -> None:
    completed = subprocess.run(
        [
            str(paths.venv_dir / "bin" / "python"),
            "-c",
            "import json,pip,sys; print(json.dumps([pip.__version__, pip.__file__, sys.prefix]))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    version, pip_file, prefix = json.loads(completed.stdout)
    assert version == ensurepip.version()
    assert Path(pip_file).is_relative_to(paths.venv_dir)
    assert Path(prefix) == paths.venv_dir
    for launcher in ("pip", "pip3", f"pip3.{sys.version_info.minor}"):
        assert (paths.venv_dir / "bin" / launcher).is_file()
    assert "include-system-site-packages = true" in (paths.venv_dir / "pyvenv.cfg").read_text()

    activated = subprocess.run(
        ["sh", "-c", '. "$1/bin/activate"; command -v python', "sh", str(paths.venv_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert activated.stdout.strip() == str(paths.venv_dir / "bin" / "python")
    python_executable, environment, cwd = resolve_subprocess_worker_context(paths)
    imported = subprocess.run(
        [python_executable, "-c", "from mindroom.script_sdk import MindRoomTools; print(MindRoomTools.__name__)"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        cwd=cwd,
    )
    assert imported.stdout.strip() == "MindRoomTools"


@requires_linux(reason="worker virtualenvs use POSIX activation and launchers", timeout=60)
def test_regular_worker_seeds_bundled_pip_offline_without_stdlib_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An empty cache needs no index access or slow ensurepip subprocess."""
    paths = local_workers.local_worker_state_paths_for_root(tmp_path / "worker")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "empty-cache"))
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_VENV_SEED", "0")
    monkeypatch.setenv("UV_INDEX_URL", "http://127.0.0.1:1/unavailable")

    def unexpected_bootstrap(*_args: object) -> None:
        pytest.fail("regular workers must seed bundled pip through uv when available")

    monkeypatch.setattr(local_workers.venv.EnvBuilder, "_setup_pip", unexpected_bootstrap)
    local_workers.ensure_local_worker_state_locked(paths)
    _assert_seeded_worker_environment(paths)


@pytest.mark.parametrize("link_mode", [None, "symlink"])
@requires_linux(reason="worker virtualenvs use POSIX activation and launchers", timeout=60)
def test_seeded_pip_mutation_is_isolated_from_other_workers_and_shared_cache(
    link_mode: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Editing one worker's pip cannot affect existing or future workers."""
    cache_dir = tmp_path / "shared-cache"
    monkeypatch.setenv("UV_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("UV_OFFLINE", "1")
    if link_mode is None:
        monkeypatch.delenv("UV_LINK_MODE", raising=False)
    else:
        monkeypatch.setenv("UV_LINK_MODE", link_mode)
    first = local_workers.local_worker_state_paths_for_root(tmp_path / "first")
    second = local_workers.local_worker_state_paths_for_root(tmp_path / "second")
    local_workers.ensure_local_worker_state_locked(first)
    local_workers.ensure_local_worker_state_locked(second)
    first_pip = next(first.venv_dir.glob("lib/python*/site-packages/pip/__init__.py"))
    second_pip = next(second.venv_dir.glob("lib/python*/site-packages/pip/__init__.py"))
    original = first_pip.read_bytes()
    cached_pip_files = list(cache_dir.rglob("pip/__init__.py"))
    assert cached_pip_files
    assert all(path.read_bytes() == original for path in cached_pip_files)

    with first_pip.open("ab") as pip_file:
        pip_file.write(b"\n# Edited only in the first worker.\n")

    assert first_pip.read_bytes() != original
    assert second_pip.read_bytes() == original
    assert all(path.read_bytes() == original for path in cached_pip_files)
    future = local_workers.local_worker_state_paths_for_root(tmp_path / "future")
    local_workers.ensure_local_worker_state_locked(future)
    future_pip = next(future.venv_dir.glob("lib/python*/site-packages/pip/__init__.py"))
    assert future_pip.read_bytes() == original


@pytest.mark.parametrize("missing", ["uv", "bundled_pip"])
@requires_linux(reason="worker virtualenvs use POSIX activation and launchers", timeout=60)
def test_regular_worker_falls_back_to_stdlib_when_uv_seeding_is_unavailable(
    missing: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Local installs lacking uv or bundled wheels retain a working pip environment."""
    paths = local_workers.local_worker_state_paths_for_root(tmp_path / "worker")
    if missing == "uv":
        monkeypatch.setattr(shutil, "which", lambda _command: None)
    else:
        original_get_path = sysconfig.get_path

        def get_path(name: str, *args: object, **kwargs: object) -> str:
            if name == "stdlib":
                return str(tmp_path / "without-bundled-pip")
            return original_get_path(name, *args, **kwargs)

        monkeypatch.setattr(sysconfig, "get_path", get_path)

    local_workers.ensure_local_worker_state_locked(paths)
    _assert_seeded_worker_environment(paths)


@requires_linux(reason="worker virtualenvs use POSIX activation and launchers", timeout=60)
def test_failed_uv_seed_is_retried_without_losing_existing_worker_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A partially created interpreter must not make failed pip seeding look ready."""
    paths = local_workers.local_worker_state_paths_for_root(tmp_path / "worker")
    retained = paths.venv_dir / "retained-user-file"
    retained.parent.mkdir(parents=True)
    retained.write_text("keep")
    fake_uv = tmp_path / "failing-uv"
    fake_uv.write_text('#!/bin/sh\nfor last; do :; done\nmkdir -p "$last/bin"\ntouch "$last/bin/python"\nexit 1\n')
    fake_uv.chmod(0o755)
    original_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda _command: str(fake_uv))

    with pytest.raises(subprocess.CalledProcessError):
        local_workers.ensure_local_worker_state_locked(paths)
    assert not (paths.venv_dir / "bin" / "python").exists()
    assert retained.read_text() == "keep"

    monkeypatch.setattr(shutil, "which", original_which)
    local_workers.ensure_local_worker_state_locked(paths)
    _assert_seeded_worker_environment(paths)
    assert retained.read_text() == "keep"
