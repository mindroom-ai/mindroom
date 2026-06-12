"""Tests for the shared worker backend env readers in `_config_helpers`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends._config_helpers import (
    read_bool_env,
    read_env,
    read_float_env,
    read_int_env,
    read_json_mapping_env,
)
from mindroom.workers.backends.docker_config import _DockerWorkerBackendConfig
from mindroom.workers.backends.kubernetes_config import KubernetesWorkerBackendConfig

if TYPE_CHECKING:
    from pathlib import Path


def test_read_env_returns_default_when_missing() -> None:
    """Missing env names should fall back to the provided default."""
    assert read_env({}, "MISSING") == ""
    assert read_env({}, "MISSING", "fallback") == "fallback"


def test_read_env_strips_whitespace() -> None:
    """Present env values should be stripped before use."""
    assert read_env({"NAME": "  value \n"}, "NAME") == "value"
    assert read_env({"NAME": "   "}, "NAME", "fallback") == ""


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, 30.0),
        ({"TIMEOUT": "12.5"}, 12.5),
        ({"TIMEOUT": " 45 "}, 45.0),
        ({"TIMEOUT": "not-a-float"}, 30.0),
        ({"TIMEOUT": ""}, 30.0),
        ({"TIMEOUT": "0.25"}, 1.0),
        ({"TIMEOUT": "-5"}, 1.0),
    ],
)
def test_read_float_env(env: dict[str, str], expected: float) -> None:
    """Float readers should fall back on unparsable values and clamp to at least 1.0."""
    assert read_float_env(env, "TIMEOUT", 30.0) == expected


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, 8766),
        ({"PORT": "9000"}, 9000),
        ({"PORT": " 9000 "}, 9000),
        ({"PORT": "not-an-int"}, 8766),
        ({"PORT": ""}, 8766),
        ({"PORT": "0"}, 1),
        ({"PORT": "-2"}, 1),
    ],
)
def test_read_int_env(env: dict[str, str], expected: int) -> None:
    """Int readers should fall back on unparsable values and clamp to at least 1."""
    assert read_int_env(env, "PORT", 8766) == expected


@pytest.mark.parametrize(
    ("env", "default", "expected"),
    [
        ({}, False, False),
        ({}, True, True),
        ({"FLAG": "1"}, False, True),
        ({"FLAG": "true"}, False, True),
        ({"FLAG": " YES "}, False, True),
        ({"FLAG": "On"}, False, True),
        ({"FLAG": "0"}, True, False),
        ({"FLAG": "false"}, True, False),
        ({"FLAG": ""}, True, False),
        ({"FLAG": "junk"}, True, False),
    ],
)
def test_read_bool_env(env: dict[str, str], *, default: bool, expected: bool) -> None:
    """Bool readers should accept common truthy spellings and treat the rest as false."""
    assert read_bool_env(env, "FLAG", default=default) is expected


def test_read_json_mapping_env_returns_empty_for_missing_or_blank() -> None:
    """Unset or blank JSON env values should resolve to an empty mapping."""
    assert read_json_mapping_env({}, "EXTRA") == {}
    assert read_json_mapping_env({"EXTRA": "   "}, "EXTRA") == {}


def test_read_json_mapping_env_cleans_valid_objects() -> None:
    """Valid JSON objects should keep strings, stringify scalars, and drop null and non-string keys."""
    raw = '{"KEEP": "value", "NUMBER": 7, "NULL": null, "1": "string-key-only"}'
    assert read_json_mapping_env({"EXTRA": raw}, "EXTRA") == {
        "KEEP": "value",
        "NUMBER": "7",
        "1": "string-key-only",
    }


@pytest.mark.parametrize("raw", ["{not-json", '["list"]', '"text"', "42"])
def test_read_json_mapping_env_rejects_malformed_values(raw: str) -> None:
    """Malformed or non-object JSON env values should fail loudly."""
    with pytest.raises(WorkerBackendError, match="EXTRA must contain a JSON object"):
        read_json_mapping_env({"EXTRA": raw}, "EXTRA")


def test_docker_config_rejects_malformed_labels_json(tmp_path: Path) -> None:
    """Malformed Docker JSON env values should raise instead of silently becoming empty mappings."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "docker",
            "MINDROOM_DOCKER_WORKER_IMAGE": "ghcr.io/mindroom-ai/mindroom:latest",
            "MINDROOM_DOCKER_WORKER_LABELS_JSON": "{not-json",
        },
    )

    with pytest.raises(WorkerBackendError, match="MINDROOM_DOCKER_WORKER_LABELS_JSON must contain a JSON object"):
        _DockerWorkerBackendConfig.from_runtime(runtime_paths)


@pytest.mark.parametrize(
    "env_name",
    [
        "MINDROOM_KUBERNETES_WORKER_ENV_JSON",
        "MINDROOM_KUBERNETES_WORKER_LABELS_JSON",
        "MINDROOM_KUBERNETES_WORKER_ANNOTATIONS_JSON",
    ],
)
def test_kubernetes_config_rejects_malformed_json_mapping_env(tmp_path: Path, env_name: str) -> None:
    """Malformed Kubernetes JSON env values should raise instead of being silently ignored."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_BACKEND": "kubernetes",
            "MINDROOM_KUBERNETES_WORKER_IMAGE": "test-image",
            "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME": "test-pvc",
            env_name: "{not-json",
        },
    )

    with pytest.raises(WorkerBackendError, match=f"{env_name} must contain a JSON object"):
        KubernetesWorkerBackendConfig.from_runtime(runtime_paths)
