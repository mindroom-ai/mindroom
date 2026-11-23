"""End-to-end sandbox-runner tests for named-worker sandbox mode (normal and streamed)."""

from __future__ import annotations

import base64
import json
import os
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from mindroom.api import sandbox_exec
from mindroom.api import sandbox_runner as sandbox_runner_module
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    named_worker_key,
    serialize_tool_execution_identity,
    worker_root_path,
)
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_TOKEN = "sandbox-mode-runner-token"  # noqa: S105
_HEADERS = {"x-mindroom-sandbox-token": _TOKEN}
_EXECUTE = "/api/sandbox-runner/execute"
_NORMAL_KEY = "v1:default:user_agent:@alice:example.org:code"
_IDENTITY = serialize_tool_execution_identity(
    ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    ),
)


@pytest.fixture
def sandbox_runner_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    """A runner configured with a `code` agent and a secret in process_env to prove sandbox strips it."""
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-6-astra\n"
        "defaults:\n"
        "  tool_output_auto_save_threshold_bytes: 1024\n"
        "agents:\n"
        "  code:\n"
        "    display_name: Code\n"
        "    tools: [shell]\n"
        "    worker_tools: [shell]\n"
        "    worker_scope: user_agent\n"
        "    named_workers: 4\n"
        "router:\n"
        "  model: default\n",
        encoding="utf-8",
    )
    test_env = {
        "PATH": os.environ.get("PATH", ""),
        "MINDROOM_CONFIG_PATH": str(config_path),
        "MINDROOM_STORAGE_PATH": str(storage_root),
        "MINDROOM_SANDBOX_PROXY_TOKEN": _TOKEN,
        "MINDROOM_SANDBOX_RUNNER_MODE": "true",
        "MINDROOM_SANDBOX_RUNNER_USE_SUBPROCESS": "false",
        "RUNNER_SECRET": "must-never-leak",
        "MINDROOM_AGENT_CLI_TOKEN": "cli-must-never-leak",
    }
    monkeypatch.delenv("MINDROOM_WORKER_COMPUTER_ENABLED", raising=False)
    monkeypatch.delenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", raising=False)
    monkeypatch.delenv("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT", raising=False)
    monkeypatch.delenv("MINDROOM_SANDBOX_SHARED_STORAGE_ROOT", raising=False)
    for key, value in test_env.items():
        monkeypatch.setenv(key, value)

    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=storage_root,
        process_env=test_env,
    )
    config = sandbox_runner_module._runtime_config_or_empty(runtime_paths)
    sandbox_runner_module.initialize_sandbox_runner_app(sandbox_runner_app, runtime_paths, config=config)
    with TestClient(sandbox_runner_app) as client:
        yield client, storage_root


def _post_execute(client: TestClient, **body: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    payload = {
        "protocol": WORKER_PROTOCOL_VERSION,
        "tool_name": "shell",
        "function_name": "run_shell_command",
        "worker_scope": "user_agent",
        "routing_agent_name": "code",
        "execution_identity": _IDENTITY,
        "private_agent_names": [],
        **body,
    }
    with client.stream("POST", _EXECUTE, headers=_HEADERS, json=payload) as response:
        assert response.status_code == 200
        return [json.loads(line) for line in response.iter_lines() if line]


def _streamed_stdout(frames: list[dict[str, Any]]) -> str:
    return b"".join(base64.b64decode(frame["b"]) for frame in frames if frame.get("t") == "out").decode("utf-8")


@pytest.mark.parametrize("stream_io", [False, True], ids=["execute", "stream_io"])
def test_empty_scratch_sandbox_uses_worker_workspace_without_resolve_agent_runtime(
    sandbox_runner_env: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
    stream_io: bool,
) -> None:
    """Empty-scratch sandbox never calls resolve_agent_runtime(create=True) and runs in worker paths.workspace."""
    client, storage_root = sandbox_runner_env
    worker_key = named_worker_key(_NORMAL_KEY, "1" * 32)

    def _forbid_resolve_agent_runtime(*_args: object, **_kwargs: object) -> object:
        msg = "resolve_agent_runtime must not be called for empty-scratch sandbox"
        raise AssertionError(msg)

    monkeypatch.setattr(sandbox_runner_module, "resolve_agent_runtime", _forbid_resolve_agent_runtime)
    # Never fall back to sandbox_exec.request_execution_env when request.sandbox is True.
    monkeypatch.setattr(
        sandbox_exec,
        "request_execution_env",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("request_execution_env must not run")),
    )

    cmd = 'printf \'%s|%s|%s|%s\' "$(pwd)" "$MINDROOM_AGENT_WORKSPACE" "$HOME" "${RUNNER_SECRET:-unset}:${MINDROOM_AGENT_CLI_TOKEN:-unset}"'
    frames = _post_execute(
        client,
        worker_key=worker_key,
        base_dir=None,
        args=[cmd],
        kwargs={"worker": "scratch", "sandbox": True},
        execution_env={},
        extra_env_passthrough="RUNNER_SECRET",
        sandbox=True,
        stream_io=stream_io,
    )
    assert frames[-1]["ok"] is True
    output = _streamed_stdout(frames) if stream_io else frames[-1]["result"]
    worker_root = worker_root_path(storage_root, worker_key).resolve()
    expected_workspace = (worker_root / "workspace").resolve()
    assert output == f"{expected_workspace}|{expected_workspace}|{worker_root}|unset:unset"
    assert not (storage_root / "agents" / "code" / "workspace").exists()


@pytest.mark.parametrize("stream_io", [False, True], ids=["execute", "stream_io"])
def test_readonly_mount_dir_keeps_home_writable_skips_hook_and_autosave(
    sandbox_runner_env: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
    stream_io: bool,
) -> None:
    """RO mount keeps HOME/XDG writable, skips .mindroom/worker-env.sh, skips auto-save, and refuses output_path."""
    client, storage_root = sandbox_runner_env
    worker_key = named_worker_key(_NORMAL_KEY, "2" * 32)
    mounted_dir = storage_root / "agents" / "code" / "workspace" / "scratch" / "ro_job"
    hook_dir = mounted_dir / ".mindroom"
    hook_dir.mkdir(parents=True)
    (hook_dir / "worker-env.sh").write_text("export HOOK_LEAKED=yes\nexit 9\n", encoding="utf-8")
    (mounted_dir / "input.txt").write_text("hello", encoding="utf-8")

    observed_shared_services: list[object] = []
    real_get_tool = sandbox_runner_module.get_tool_by_name

    def _spy_get_tool(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        observed_shared_services.append(kwargs.get("allowed_shared_services"))
        return real_get_tool(*args, **kwargs)

    monkeypatch.setattr(sandbox_runner_module, "get_tool_by_name", _spy_get_tool)

    mounted_dir.chmod(0o555)
    try:
        cmd = (
            'touch "$HOME/home-ok" && '
            "printf '%s|%s|%s|%s|%s|%s' "
            '"$(pwd)" "$MINDROOM_AGENT_WORKSPACE" "$HOME" "$XDG_CONFIG_HOME" '
            '"${HOOK_LEAKED:-unset}:${RUNNER_SECRET:-unset}:${WIRE_SECRET:-unset}" '
            '"$(cat input.txt)"'
        )
        frames = _post_execute(
            client,
            worker_key=worker_key,
            base_dir="agents/code/workspace/scratch/ro_job",
            args=[cmd],
            kwargs={"worker": "ro", "mount_dir": "scratch/ro_job", "mount_read_only": True},
            execution_env={"WIRE_SECRET": "should-be-dropped"},
            environment={"set_names": ["WIRE_SECRET"], "unset_names": [], "path_prepend": []},
            extra_env_passthrough="RUNNER_SECRET",
            tool_config_overrides={"extra_env_passthrough": "RUNNER_SECRET"},
            sandbox=True,
            stream_io=stream_io,
        )
        assert frames[-1]["ok"] is True
        output = _streamed_stdout(frames) if stream_io else frames[-1]["result"]
        worker_root = worker_root_path(storage_root, worker_key).resolve()
        assert output == (
            f"{mounted_dir.resolve()}|{mounted_dir.resolve()}|{worker_root}|{worker_root}/.config"
            "|unset:unset:unset|hello"
        )
        assert (worker_root / "home-ok").is_file()
        if stream_io:
            assert observed_shared_services == [frozenset()]

        if not stream_io:
            # Large output (> 1024 byte threshold) does not crash auto-save on a read-only mount.
            large_frames = _post_execute(
                client,
                worker_key=worker_key,
                base_dir="agents/code/workspace/scratch/ro_job",
                args=["printf 'x%.0s' {1..2000}"],
                kwargs={"worker": "ro"},
                sandbox=True,
            )
            assert large_frames[-1]["ok"] is True
            assert large_frames[-1]["result"] == "x" * 2000

            # Explicit mindroom_output_path on a read-only mount is refused cleanly by the runner too.
            refused_frames = _post_execute(
                client,
                worker_key=worker_key,
                base_dir="agents/code/workspace/scratch/ro_job",
                args=["echo hi"],
                kwargs={"worker": "ro", "mindroom_output_path": "out.txt"},
                sandbox=True,
            )
            assert refused_frames[-1]["ok"] is False
            assert "read-only sandbox mount" in refused_frames[-1]["error"]
    finally:
        mounted_dir.chmod(0o755)


def test_resolve_entrypoint_sandbox_forces_empty_allowed_shared_services(
    sandbox_runner_env: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_resolve_entrypoint passes allowed_shared_services=frozenset() whenever sandbox=True."""
    _client, _storage_root = sandbox_runner_env
    observed: list[object] = []
    real_get_tool = sandbox_runner_module.get_tool_by_name

    def _spy_get_tool(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        observed.append(kwargs.get("allowed_shared_services"))
        return real_get_tool(*args, **kwargs)

    monkeypatch.setattr(sandbox_runner_module, "get_tool_by_name", _spy_get_tool)
    runtime_paths = sandbox_runner_module.app_runtime_paths(sandbox_runner_app)
    config = sandbox_runner_module.app_runtime_config(sandbox_runner_app)
    tools = sandbox_runner_module.refresh_plugins(config, runtime_paths).tools
    sandbox_runner_module._resolve_entrypoint(
        runtime_paths=runtime_paths,
        config=config,
        tools=tools,
        tool_name="shell",
        function_name="run_shell_command",
        worker_scope="user_agent",
        routing_agent_name="code",
        sandbox=True,
    )
    assert observed == [frozenset()]
