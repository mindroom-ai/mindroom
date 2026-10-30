"""Tests for the dashboard API's lifespan inside `mindroom run`."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI

from mindroom import constants
from mindroom.api import config_lifecycle, main
from mindroom.workers.backends import docker as docker_backend_module
from mindroom.workers.backends.docker import DockerWorkerBackend
from mindroom.workers.models import WorkerSpec
from mindroom.workers.runtime import (
    bind_worker_launch_config_source,
    lease_primary_worker_manager,
    shutdown_primary_worker_manager,
)
from tests.runtime_fakes import bind_test_embedded_runtime
from tests.test_docker_worker_backend import _FakeDockerClient, _FakeDockerError, _FakeImage, _FakeNotFoundError

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.workers.models import WorkerHandle

_DOCKER_WORKER_IMAGE = "ghcr.io/mindroom-ai/mindroom:latest"


def _api_app(tmp_path: Path) -> FastAPI:
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={},
    )
    api_app = FastAPI()
    main.initialize_api_app(api_app, runtime_paths)
    return api_app


@pytest.mark.asyncio
async def test_the_api_refuses_to_serve_outside_mindroom_run(tmp_path: Path) -> None:
    """Served without the runtime `mindroom run` lends it, the dashboard API fails at startup instead of serving."""
    api_app = _api_app(tmp_path)

    with pytest.raises(RuntimeError, match="only inside `mindroom run`"):
        async with main._lifespan(api_app):
            pytest.fail("the API served without a runtime")


@pytest.mark.asyncio
async def test_the_lifespan_leaves_config_loads_to_mindroom_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`mindroom run` published its config load before serving the API and publishes every later load itself.

    Idle-worker cleanup keeps the workers of the runtime's live background scripts.
    """
    api_app = _api_app(tmp_path)

    def keep_live_script_workers(_backend: object) -> None:
        pass

    bind_test_embedded_runtime(api_app, script_worker_keepalive=keep_live_script_workers)
    snapshot_before = main._app_context(api_app)
    ran: list[str] = []

    async def record_cleanup(*_args: object, touch_live_workers: object, **_kwargs: object) -> None:
        assert touch_live_workers is keep_live_script_workers
        ran.append("worker_cleanup")

    monkeypatch.setattr(main, "_worker_cleanup_loop", record_cleanup)

    async with main._lifespan(api_app):
        await asyncio.sleep(0)

    assert ran == ["worker_cleanup"]
    assert main._app_context(api_app) is snapshot_before


def _docker_worker_api_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[FastAPI, _FakeDockerClient]:
    """Return an API app configured for Docker workers, served by an in-memory Docker daemon."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "data",
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": _DOCKER_WORKER_IMAGE,
        },
    )
    api_app = FastAPI()
    main.initialize_api_app(api_app, runtime_paths)
    docker_daemon = _FakeDockerClient()
    docker_daemon.images.by_name[_DOCKER_WORKER_IMAGE] = _FakeImage("sha256:image-v1")
    docker_errors = SimpleNamespace(DockerException=_FakeDockerError, NotFound=_FakeNotFoundError)
    monkeypatch.setattr(
        docker_backend_module,
        "_load_docker_client_and_errors",
        lambda **_kwargs: (docker_daemon, docker_errors),
    )
    # The in-memory daemon runs no sandbox runner to answer the readiness probe.
    monkeypatch.setattr(
        DockerWorkerBackend,
        "_wait_for_ready",
        lambda _backend, _container, **_kwargs: None,
    )
    return api_app, docker_daemon


def _containers(docker_daemon: _FakeDockerClient) -> list[tuple[str, str]]:
    return [(container.name, container.status) for container in docker_daemon.containers.list(all=True)]


def _start_docker_worker(api_app: FastAPI) -> WorkerHandle:
    """Start one worker the way a worker-routed tool call does, launching from the process's applied config."""
    runtime_paths = main._app_runtime_paths(api_app)
    with lease_primary_worker_manager(
        runtime_paths,
        proxy_url=None,
        proxy_token="test-token",  # noqa: S106
    ) as worker_manager:
        return worker_manager.ensure_worker(WorkerSpec("v1:default:unscoped:code"))


@pytest.mark.asyncio
async def test_embedded_api_shutdown_leaves_workers_to_the_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`mindroom run` stops the worker manager itself once its bots have stopped, so its API shutdown must not."""
    api_app, docker_daemon = _docker_worker_api_app(monkeypatch, tmp_path)
    bind_test_embedded_runtime(api_app)
    # `mindroom run` loads the config into its API before serving it.
    config_lifecycle.load_config_into_app(main._app_runtime_paths(api_app), api_app)

    try:
        # `mindroom run` owns the applied config there, so the embedded API binds none of its own.
        with bind_worker_launch_config_source(lambda: main._app_context(api_app).runtime_config):
            async with main._lifespan(api_app):
                worker = _start_docker_worker(api_app)

        assert _containers(docker_daemon) == [(worker.worker_id, "running")]
    finally:
        shutdown_primary_worker_manager(timeout_seconds=0.0)
