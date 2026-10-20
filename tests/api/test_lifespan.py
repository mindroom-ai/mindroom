"""Tests for API process lifecycle work."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI

from mindroom import constants
from mindroom.api import config_lifecycle, main
from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
from mindroom.workers.backends import docker as docker_backend_module
from mindroom.workers.backends.docker import DockerWorkerBackend
from mindroom.workers.models import WorkerSpec
from mindroom.workers.runtime import get_primary_worker_manager, shutdown_primary_worker_manager
from tests.test_docker_worker_backend import _FakeDockerClient, _FakeDockerError, _FakeImage, _FakeNotFoundError

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.workers.models import WorkerHandle

_DOCKER_WORKER_IMAGE = "ghcr.io/mindroom-ai/mindroom:latest"


@pytest.mark.asyncio
async def test_api_lifespan_syncs_credentials_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Credential filesystem work should run outside the API event loop."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={},
    )
    api_app = FastAPI()
    main.initialize_api_app(api_app, runtime_paths)
    config_lifecycle.app_state(api_app).orchestrator_knowledge_refresh_scheduler = object()  # type: ignore[assignment]
    event_loop_thread = threading.get_ident()
    credential_sync_thread: int | None = None

    def _sync_credentials(runtime_paths: constants.RuntimePaths) -> None:
        nonlocal credential_sync_thread
        assert runtime_paths is main._app_runtime_paths(api_app)
        credential_sync_thread = threading.get_ident()

    async def _no_op_async(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(constants, "ensure_writable_config_path", lambda **_kwargs: None)
    monkeypatch.setattr(config_lifecycle, "load_config_into_app", lambda *_args: True)
    monkeypatch.setattr(main, "sync_env_to_credentials", _sync_credentials)
    monkeypatch.setattr(main, "_sync_standalone_knowledge_watchers", _no_op_async)
    monkeypatch.setattr(main, "_watch_config", _no_op_async)
    monkeypatch.setattr(main, "_worker_cleanup_loop", _no_op_async)

    async with main._lifespan(api_app):
        pass

    assert credential_sync_thread is not None
    assert credential_sync_thread != event_loop_thread


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
        lambda _backend, _container: "http://127.0.0.1:1/api/sandbox-runner/execute",
    )
    return api_app, docker_daemon


def _containers(docker_daemon: _FakeDockerClient) -> list[tuple[str, str]]:
    return [(container.name, container.status) for container in docker_daemon.containers.list(all=True)]


def _start_docker_worker(api_app: FastAPI) -> WorkerHandle:
    """Start one worker the way a worker-routed tool call does."""
    runtime_paths = main._app_runtime_paths(api_app)
    worker_manager = get_primary_worker_manager(
        runtime_paths,
        proxy_url=None,
        proxy_token="test-token",  # noqa: S106
        storage_root=runtime_paths.storage_root,
    )
    return worker_manager.ensure_worker(WorkerSpec("v1:default:unscoped:code"))


@pytest.mark.asyncio
async def test_standalone_api_shutdown_removes_its_docker_worker_containers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An API served without `mindroom run` must not leave its Docker worker containers running after it stops."""
    api_app, docker_daemon = _docker_worker_api_app(monkeypatch, tmp_path)

    async with main._lifespan(api_app):
        worker = _start_docker_worker(api_app)
        assert _containers(docker_daemon) == [(worker.worker_id, "running")]

    assert _containers(docker_daemon) == []


@pytest.mark.asyncio
async def test_embedded_api_shutdown_leaves_workers_to_the_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`mindroom run` stops the worker manager itself once its bots have stopped, so its API shutdown must not."""
    api_app, docker_daemon = _docker_worker_api_app(monkeypatch, tmp_path)
    config_lifecycle.app_state(api_app).orchestrator_knowledge_refresh_scheduler = KnowledgeRefreshScheduler()

    try:
        async with main._lifespan(api_app):
            worker = _start_docker_worker(api_app)

        assert _containers(docker_daemon) == [(worker.worker_id, "running")]
    finally:
        shutdown_primary_worker_manager(timeout_seconds=0.0)
