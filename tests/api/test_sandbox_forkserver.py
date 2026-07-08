"""Tests for the sandbox forkserver warm-template dispatch."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from typing import TYPE_CHECKING

import pytest

import mindroom.api.sandbox_exec as sandbox_exec_module
import mindroom.api.sandbox_forkserver as sandbox_forkserver_module
import mindroom.api.sandbox_protocol as sandbox_protocol_module
import mindroom.api.sandbox_runner as sandbox_runner_module
from mindroom.api.sandbox_forkserver import (
    ForkserverError,
    ForkserverStartupError,
    ForkserverTimeoutError,
    _SandboxForkserver,
)
from mindroom.constants import resolve_primary_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

pytestmark = pytest.mark.skipif(
    not sandbox_forkserver_module.forkserver_supported(),
    reason="forkserver dispatch requires os.fork and unix sockets",
)

_STUB_TEMPLATE = '''\
"""Lightweight forkserver template used to test the manager transport."""

import os
import sys
import time
from pathlib import Path

from mindroom.api.sandbox_forkserver import serve_template


def _run_payload(payload: str) -> tuple[int, str, str]:
    if payload.startswith("sleep:"):
        pid_file = Path(payload.removeprefix("sleep:"))
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(30)
    marker = os.environ.get("FORKSERVER_TEST_MARKER", "")
    return 0, f"{os.getppid()}|{os.getcwd()}|{marker}", "stderr:" + payload


sys.exit(serve_template(sys.argv[1], _run_payload))
'''


@pytest.fixture
def stub_manager(tmp_path: Path) -> Iterator[tuple[_SandboxForkserver, list[str]]]:
    """Manager whose template runs a fast stub instead of the full runtime import."""
    script = tmp_path / "stub_template.py"
    script.write_text(_STUB_TEMPLATE, encoding="utf-8")
    spawned: list[str] = []

    def _command(python_executable: str, socket_path: str) -> list[str]:
        spawned.append(socket_path)
        return [python_executable, str(script), socket_path]

    manager = _SandboxForkserver(template_command=_command)
    yield manager, spawned
    manager.shutdown()


def _stub_execute(
    manager: _SandboxForkserver,
    *,
    template_env: dict[str, str] | None = None,
    request_env: dict[str, str] | None = None,
    request_cwd: str | None = None,
    envelope: str = "payload",
    timeout_seconds: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return manager.execute(
        python_executable=None,
        template_env=template_env,
        request_env=request_env,
        request_cwd=request_cwd,
        envelope=envelope,
        timeout_seconds=timeout_seconds,
    )


def _template_pid(completed: subprocess.CompletedProcess[str]) -> int:
    return int(completed.stdout.split("|")[0])


def test_execute_round_trips_and_reuses_one_template(
    stub_manager: tuple[_SandboxForkserver, list[str]],
    tmp_path: Path,
) -> None:
    """Repeated calls must fork the same warm template instead of respawning it."""
    manager, spawned = stub_manager
    workdir = tmp_path / "request-cwd"
    workdir.mkdir()

    first = _stub_execute(
        manager,
        request_env={"FORKSERVER_TEST_MARKER": "marker-1"},
        request_cwd=str(workdir),
        envelope="payload-1",
    )
    second = _stub_execute(manager, request_env={"FORKSERVER_TEST_MARKER": "marker-2"}, envelope="payload-2")

    assert first.returncode == 0
    assert first.stdout.split("|")[1] == str(workdir.resolve())
    assert first.stdout.split("|")[2] == "marker-1"
    assert first.stderr == "stderr:payload-1"
    assert second.stdout.split("|")[2] == "marker-2"
    assert _template_pid(first) == _template_pid(second)
    assert len(spawned) == 1


def test_template_recycled_when_env_fingerprint_changes(
    stub_manager: tuple[_SandboxForkserver, list[str]],
) -> None:
    """A changed template env must terminate the old template and spawn a fresh one."""
    manager, spawned = stub_manager
    base_env = dict(os.environ)

    first = _stub_execute(manager, template_env=base_env)
    changed = _stub_execute(manager, template_env={**base_env, "FORKSERVER_FINGERPRINT": "changed"})

    assert _template_pid(first) != _template_pid(changed)
    assert len(spawned) == 2
    with pytest.raises(ProcessLookupError):
        os.kill(_template_pid(first), 0)


def test_crashed_template_respawns_transparently(
    stub_manager: tuple[_SandboxForkserver, list[str]],
) -> None:
    """A killed template must be detected and replaced on the next call."""
    manager, spawned = stub_manager

    first_pid = _template_pid(_stub_execute(manager))
    os.kill(first_pid, signal.SIGKILL)

    deadline = time.monotonic() + 10.0
    while True:
        try:
            completed = _stub_execute(manager)
            break
        except ForkserverError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)

    assert _template_pid(completed) != first_pid
    assert len(spawned) == 2


def test_timeout_kills_child_and_keeps_template_alive(
    stub_manager: tuple[_SandboxForkserver, list[str]],
    tmp_path: Path,
) -> None:
    """A timed-out request must SIGKILL the fork child without recycling the template."""
    manager, spawned = stub_manager
    pid_file = tmp_path / "child.pid"

    with pytest.raises(ForkserverTimeoutError):
        _stub_execute(manager, envelope=f"sleep:{pid_file}", timeout_seconds=2.0)

    child_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("forked child survived the request timeout")

    assert _stub_execute(manager).returncode == 0
    assert len(spawned) == 1


def test_template_startup_failure_pins_fingerprint() -> None:
    """A template that dies during startup must not be respawned on every call."""
    attempts: list[str] = []

    def _command(python_executable: str, socket_path: str) -> list[str]:
        attempts.append(socket_path)
        return [python_executable, "-c", "import sys; sys.exit(3)"]

    manager = _SandboxForkserver(template_command=_command)
    try:
        with pytest.raises(ForkserverStartupError):
            _stub_execute(manager)
        with pytest.raises(ForkserverStartupError):
            _stub_execute(manager)
    finally:
        manager.shutdown()

    assert len(attempts) == 1


def _forkserver_runtime(tmp_path: Path) -> tuple[RuntimePaths, Config]:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.5\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "forkserver"},
    )
    return runtime_paths, sandbox_runner_module._runtime_config_or_empty(runtime_paths)


def test_forkserver_mode_routes_through_subprocess_dispatch(tmp_path: Path) -> None:
    """Forkserver mode must keep the subprocess routing decision in execute_tool_call."""
    runtime_paths, _config = _forkserver_runtime(tmp_path)

    assert sandbox_exec_module.runner_uses_subprocess(runtime_paths) is True
    assert sandbox_exec_module.runner_uses_forkserver(runtime_paths) is True


def test_forkserver_mode_reuses_one_template_across_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Warm-path acceptance: repeated non-shell tool calls must not re-pay the runtime import."""
    runtime_paths, config = _forkserver_runtime(tmp_path)
    spawned: list[str] = []

    def _command(python_executable: str, socket_path: str) -> list[str]:
        spawned.append(socket_path)
        return sandbox_forkserver_module._default_template_command(python_executable, socket_path)

    manager = _SandboxForkserver(template_command=_command)
    monkeypatch.setattr(sandbox_forkserver_module, "get_sandbox_forkserver", lambda: manager)
    try:
        for _ in range(2):
            response = sandbox_runner_module._execute_request_subprocess_sync(
                sandbox_runner_module.SandboxRunnerExecuteRequest(
                    tool_name="calculator",
                    function_name="add",
                    args=[1, 2],
                    kwargs={},
                ),
                runtime_paths,
                config,
            )
            assert response.ok is True
            assert '"result": 3' in str(response.result)

        workdir = tmp_path / "files"
        workdir.mkdir()
        response = sandbox_runner_module._execute_request_subprocess_sync(
            sandbox_runner_module.SandboxRunnerExecuteRequest(
                tool_name="file",
                function_name="save_file",
                kwargs={"contents": "hello forkserver", "file_name": "out.txt"},
                tool_init_overrides={"base_dir": str(workdir)},
            ),
            runtime_paths,
            config,
        )
        assert response.ok is True
        assert (workdir / "out.txt").read_text(encoding="utf-8") == "hello forkserver"

        response = sandbox_runner_module._execute_request_subprocess_sync(
            sandbox_runner_module.SandboxRunnerExecuteRequest(
                tool_name="python",
                function_name="run_python_code",
                args=['import os\nresult = os.environ.get("TEST_EXECUTION_ENV")', "result"],
                kwargs={},
                execution_env={"TEST_EXECUTION_ENV": "explicit-value"},
            ),
            runtime_paths,
            config,
        )
        assert response.ok is True
        assert response.result == "explicit-value"
    finally:
        manager.shutdown()

    assert len(spawned) == 1


def test_forkserver_timeout_maps_to_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Forkserver timeouts must surface the same worker failure as spawn-per-call timeouts."""
    runtime_paths, config = _forkserver_runtime(tmp_path)

    class _TimeoutForkserver:
        def execute(self, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise ForkserverTimeoutError

    monkeypatch.setattr(sandbox_forkserver_module, "get_sandbox_forkserver", _TimeoutForkserver)

    response = sandbox_runner_module._execute_request_subprocess_sync(
        sandbox_runner_module.SandboxRunnerExecuteRequest(
            tool_name="calculator",
            function_name="add",
            args=[1, 2],
            kwargs={},
        ),
        runtime_paths,
        config,
    )

    assert response.ok is False
    assert response.error == "Sandbox subprocess timed out."
    assert response.failure_kind == "worker"


def test_forkserver_startup_failure_falls_back_to_spawn_per_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A broken template must not break tool calls; dispatch falls back to spawn-per-call."""
    runtime_paths, config = _forkserver_runtime(tmp_path)

    class _BrokenForkserver:
        def execute(self, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            msg = "template import crashed"
            raise ForkserverStartupError(msg)

    monkeypatch.setattr(sandbox_forkserver_module, "get_sandbox_forkserver", _BrokenForkserver)

    def _fake_run(*_args: object, **run_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert json.loads(str(run_kwargs["input"]))["request"]["tool_name"] == "calculator"
        response = sandbox_runner_module.SandboxRunnerExecuteResponse(ok=True, result="spawned")
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr=sandbox_protocol_module._RESPONSE_MARKER + response.model_dump_json(),
        )

    monkeypatch.setattr(sandbox_runner_module.subprocess, "run", _fake_run)

    response = sandbox_runner_module._execute_request_subprocess_sync(
        sandbox_runner_module.SandboxRunnerExecuteRequest(
            tool_name="calculator",
            function_name="add",
            args=[1, 2],
            kwargs={},
        ),
        runtime_paths,
        config,
    )

    assert response.ok is True
    assert response.result == "spawned"
