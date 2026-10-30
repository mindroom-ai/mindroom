"""Shared Kubernetes worker pod container and volume names."""

from __future__ import annotations

SANDBOX_RUNNER_CONTAINER_NAME = "sandbox-runner"

WORKER_STORAGE_VOLUME_NAME = "worker-storage"
WORKER_TMP_VOLUME_NAME = "worker-tmp"

RESERVED_EXTRA_CONTAINER_NAMES = frozenset({SANDBOX_RUNNER_CONTAINER_NAME})
RESERVED_EXTRA_VOLUME_NAMES = frozenset({WORKER_STORAGE_VOLUME_NAME, WORKER_TMP_VOLUME_NAME})
