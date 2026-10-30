"""Dedicated workers launch from the applied config, while each call sends the snapshot and egress of its build config."""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

import pytest

import mindroom.tool_system.sandbox_proxy as sandbox_proxy_module
import mindroom.workers.runtime as workers_runtime_module
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.message_target import MessageTarget
from mindroom.tool_system.bootstrap import ensure_tool_registry_loaded
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import ConfiguredWorkerBackend, WorkerHandle, WorkerLaunchConfig, WorkerSpec
from mindroom.workers.runtime import bind_worker_launch_config_source
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_TOKEN = "worker-proxy-token"  # noqa: S105


class _ResultLine:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def read(self) -> bytes:
        return b""

    def iter_bytes(self) -> Iterator[bytes]:
        yield json.dumps({"ok": True, "result": "sandbox-result"}).encode() + b"\n"


def _recording_client_class(*, captured_calls: list[tuple[str, dict[str, Any]]] | None = None) -> type:
    class _Client:
        def __init__(self, *, timeout: float) -> None:
            del timeout

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        @contextlib.contextmanager
        def stream(self, method: str, url: str, *, json: dict[str, Any], **_kwargs: object) -> Iterator[_ResultLine]:
            del method
            if captured_calls is not None:
                captured_calls.append((url, json))
            yield _ResultLine()

    return _Client


class _FakeBackend:
    """Record the manager builds, shutdowns and worker requests one test makes."""

    builds: list[str]
    shutdowns: list[str]
    specs: list[WorkerSpec]
    launches: list[WorkerLaunchConfig]
    cleanup_locator: str | None = None
    idle_timeout_seconds = 60.0

    def __init__(self, backend_name: str, current_launch: Callable[[], WorkerLaunchConfig]) -> None:
        self.backend_name = backend_name
        self._current_launch = current_launch

    def ensure_worker(
        self,
        spec: WorkerSpec,
        *,
        now: float | None = None,
        progress_sink: object = None,
    ) -> WorkerHandle:
        del now, progress_sink
        # As the real dedicated backends do inside the worker's lock.
        type(self).launches.append(self._current_launch())
        type(self).specs.append(spec)
        return WorkerHandle(
            worker_id="worker-1",
            worker_key=spec.worker_key,
            endpoint="http://worker/api/sandbox-runner/execute",
            auth_token=_TOKEN,
            status="ready",
            backend_name=self.backend_name,
            last_used_at=0.0,
            created_at=0.0,
        )

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> None:
        del worker_key, now

    def record_failure(self, worker_key: str, failure_reason: str, **_kwargs: object) -> None:
        msg = f"unexpected worker failure for {worker_key}: {failure_reason}"
        raise AssertionError(msg)

    def shutdown(self) -> None:
        type(self).shutdowns.append(self.backend_name)


def _install_fake_backend(monkeypatch: pytest.MonkeyPatch, backend: str) -> type[_FakeBackend]:
    class _Recorded(_FakeBackend):
        builds: list[str] = []  # noqa: RUF012
        shutdowns: list[str] = []  # noqa: RUF012
        specs: list[WorkerSpec] = []  # noqa: RUF012
        launches: list[WorkerLaunchConfig] = []  # noqa: RUF012

    def build(current_launch: Callable[[], WorkerLaunchConfig]) -> _Recorded:
        _Recorded.builds.append(backend)
        return _Recorded(backend, current_launch)

    def configure(
        _runtime_paths: RuntimePaths,
        *,
        auth_token: str | None,
        storage_root: Path,
    ) -> ConfiguredWorkerBackend:
        del storage_root
        return ConfiguredWorkerBackend(signature=(backend, str(auth_token)), cleanup_signature=(backend,), build=build)

    monkeypatch.setattr(workers_runtime_module, f"configure_{backend}_worker_backend", configure)
    return _Recorded


def _runtime(tmp_path: Path, backend: str) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_WORKER_BACKEND": backend,
            "MINDROOM_SANDBOX_PROXY_TOKEN": _TOKEN,
            "MINDROOM_SANDBOX_EXECUTION_MODE": "selective",
            "MINDROOM_SANDBOX_PROXY_TOOLS": "calculator",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:test",
            "MINDROOM_KUBERNETES_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:test",
            "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME": "mindroom-storage",
        },
    )


def _config(runtime_paths: RuntimePaths) -> Config:
    return Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
            "router": {"model": "default"},
            "defaults": {"worker_grantable_credentials": ["github_private"]},
            "agents": {"mind": {"display_name": "Mind", "worker_scope": "shared", "tools": ["calculator"]}},
        },
        runtime_paths,
    )


def _calculator_add(runtime_paths: RuntimePaths, config: Config) -> Any:  # noqa: ANN401
    identity = ToolExecutionIdentity(
        channel="openai_compat",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-1",
    )
    ensure_tool_registry_loaded(runtime_paths, config)
    toolkit = get_tool_by_name(
        "calculator",
        runtime_paths,
        runtime_config=config,
        worker_target=resolve_worker_target("shared", "mind", execution_identity=identity),
    )
    entrypoint = toolkit.functions["add"].entrypoint
    assert entrypoint is not None
    return entrypoint


def _turn_context(runtime_paths: RuntimePaths, config: Config) -> object:
    return make_test_tool_runtime_context(
        agent_name="mind",
        target=MessageTarget.resolve(room_id="!room:example.org", thread_id=None, reply_to_event_id=None),
        requester_id="@alice:example.org",
        client=object(),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


@pytest.fixture(autouse=True)
def _fresh_worker_manager() -> Iterator[None]:
    workers_runtime_module._reset_primary_worker_manager()
    yield
    workers_runtime_module._reset_primary_worker_manager()


class _AppliedConfig:
    """Stand in for the process that owns the applied config."""

    def __init__(self, config: Config) -> None:
        self.config: Config | None = config


@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
def test_calls_outside_a_matrix_turn_share_the_worker_manager_of_calls_inside_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: str,
) -> None:
    """An OpenAI-compatible call binds no tool runtime context, and still reaches the one worker manager."""
    runtime_paths = _runtime(tmp_path, backend)
    config = _config(runtime_paths)
    recorded = _install_fake_backend(monkeypatch, backend)
    sent: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(sandbox_proxy_module.httpx, "Client", _recording_client_class(captured_calls=sent))
    add = _calculator_add(runtime_paths, config)

    with bind_worker_launch_config_source(lambda: config):
        with tool_runtime_context(_turn_context(runtime_paths, config)):
            assert add(1, 2) == "sandbox-result"
        assert add(1, 2) == "sandbox-result"
        with tool_runtime_context(_turn_context(runtime_paths, config)):
            assert add(1, 2) == "sandbox-result"

    assert recorded.builds == [backend]
    assert recorded.shutdowns == []
    assert len(recorded.specs) == 3
    launches = recorded.launches
    assert all(launch.config is config for launch in launches)
    assert {launch.grantable_credentials for launch in launches} == {
        frozenset({"github_private"}),
    }
    assert all(spec.mirror_grantable_credentials for spec in recorded.specs)
    snapshots = [payload.get("config_snapshot") for _url, payload in sent]
    expected_snapshot = None if backend == "docker" else snapshots[0]
    assert snapshots == [expected_snapshot] * 3
    if backend == "kubernetes":
        assert snapshots[0] is not None


@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
def test_a_turn_built_before_a_reload_launches_its_worker_from_the_applied_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: str,
) -> None:
    """Workers launch from the applied config, so a turn still on the old config cannot restart them back to it."""
    runtime_paths = _runtime(tmp_path, backend)
    old_config = _config(runtime_paths)
    new_config = Config.validate_with_runtime(
        {
            **old_config.authored_model_dump(),
            "defaults": {"worker_grantable_credentials": []},
        },
        runtime_paths,
    )
    recorded = _install_fake_backend(monkeypatch, backend)
    monkeypatch.setattr(sandbox_proxy_module.httpx, "Client", _recording_client_class())
    old_turn_add = _calculator_add(runtime_paths, old_config)
    applied = _AppliedConfig(old_config)

    with bind_worker_launch_config_source(lambda: applied.config):
        assert old_turn_add(1, 2) == "sandbox-result"
        applied.config = new_config
        assert old_turn_add(1, 2) == "sandbox-result"
        assert _calculator_add(runtime_paths, new_config)(1, 2) == "sandbox-result"

    launches = recorded.launches
    assert [launch.config for launch in launches] == [
        old_config,
        new_config,
        new_config,
    ]
    assert launches[1] == launches[2]
    assert launches[0] != launches[1]
    assert recorded.builds == [backend]


def test_a_dedicated_call_without_an_applied_config_fails_before_reaching_a_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No process source of the applied config means no dedicated worker launch, rather than one from another config."""
    runtime_paths = _runtime(tmp_path, "docker")
    config = _config(runtime_paths)
    recorded = _install_fake_backend(monkeypatch, "docker")
    monkeypatch.setattr(sandbox_proxy_module.httpx, "Client", _recording_client_class())

    with pytest.raises(WorkerBackendError, match="applied config"):
        _calculator_add(runtime_paths, config)(1, 2)
    assert recorded.specs == []
