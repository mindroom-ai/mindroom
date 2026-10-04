"""Rendered runtime chart checks for extra state subpaths and dedicated session and knowledge volumes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mindroom.runtime_env_policy import SESSION_STORAGE_PATH_ENV
from tests.test_helm_instance_worker_isolation import (
    _container,
    _env_by_name,
    _init_container,
    _render_chart,
    _resource,
    _run_helm_template,
    _values_files,
    _volumes_by_name,
)

RUNTIME_CHART = Path("cluster/k8s/runtime")
_STATE = {"enabled": True, "existingClaim": "mindroom-state"}


def _render(tmp_path: Path, values: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Return the rendered docs, the runtime Deployment, and its mindroom container."""
    docs = _render_chart(RUNTIME_CHART, release_name="mindroom-runtime", values_files=_values_files(tmp_path, values))
    deployment = _resource(docs, "Deployment", "mindroom-runtime")
    return docs, deployment, _container(deployment, "mindroom")


def _pvcs(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {doc["metadata"]["name"]: doc["spec"] for doc in docs if doc["kind"] == "PersistentVolumeClaim"}


def test_storage_options_render_nothing_by_default(tmp_path: Path) -> None:
    """Unset session and knowledge storage add no PVC, volume, mount, or env."""
    docs, deployment, container = _render(tmp_path, {})
    new_volumes = {"session-storage", "knowledge-storage"}

    assert set(_pvcs(docs)).isdisjoint({"mindroom-runtime-sessions", "mindroom-runtime-knowledge"})
    assert set(_volumes_by_name(deployment)).isdisjoint(new_volumes)
    assert {mount["name"] for mount in container["volumeMounts"]}.isdisjoint(new_volumes)
    assert SESSION_STORAGE_PATH_ENV not in _env_by_name(container)


def test_state_storage_extra_subpaths_are_mounted_and_prepared_by_the_init_container(tmp_path: Path) -> None:
    """Extra state subpaths are created and chowned before the kubelet would create them as root.

    Subpaths YAML would otherwise retype, from name or subPath, still render as strings.
    """
    _, deployment, container = _render(
        tmp_path,
        {
            "stateStorage": {
                **_STATE,
                "extraSubPaths": [
                    {"name": "tracking", "mountPath": "/app/agent_data/tracking"},
                    {"name": "cache", "mountPath": "/app/cache", "subPath": "nested/cache"},
                    {"name": "2026", "mountPath": "/app/a"},
                    {"name": "b", "mountPath": "/app/b", "subPath": "on"},
                ],
            },
        },
    )
    mounts = {mount["mountPath"]: mount for mount in container["volumeMounts"]}
    dirs = " ".join(
        f'"/state{suffix}"'
        for suffix in ("", "/encryption_keys", "/sync_continuity", "/tracking", "/nested/cache", "/2026", "/on")
    )

    assert mounts["/app/agent_data/tracking"] == {
        "name": "state-storage",
        "mountPath": "/app/agent_data/tracking",
        "subPath": "tracking",
    }
    assert mounts["/app/cache"] == {"name": "state-storage", "mountPath": "/app/cache", "subPath": "nested/cache"}
    assert mounts["/app/a"]["subPath"] == "2026"
    assert mounts["/app/b"]["subPath"] == "on"
    assert _init_container(deployment, "prepare-state-storage")["command"][2].splitlines()[1:] == [
        f"mkdir -p {dirs}",
        f"chown -R 1000:1000 {dirs}",
        f"chmod 2775 {dirs}",
    ]


def test_session_and_knowledge_storage_create_pvcs_and_point_the_runtime_at_them(tmp_path: Path) -> None:
    """Chart-managed session and knowledge storage get their own PVCs, and sessions their mount and storage path."""
    docs, deployment, container = _render(
        tmp_path,
        {
            "sessionStorage": {"enabled": True, "size": "20Gi", "storageClassName": "fast-rwo"},
            "knowledgeStorage": {"enabled": True, "accessModes": ["ReadWriteMany"]},
        },
    )

    assert _pvcs(docs)["mindroom-runtime-sessions"] == {
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "fast-rwo",
        "resources": {"requests": {"storage": "20Gi"}},
    }
    assert _pvcs(docs)["mindroom-runtime-knowledge"] == {
        "accessModes": ["ReadWriteMany"],
        "resources": {"requests": {"storage": "10Gi"}},
    }
    assert _volumes_by_name(deployment)["session-storage"] == {
        "name": "session-storage",
        "persistentVolumeClaim": {"claimName": "mindroom-runtime-sessions"},
    }
    assert {"name": "session-storage", "mountPath": "/app/session_state"} in container["volumeMounts"]
    assert _env_by_name(container)[SESSION_STORAGE_PATH_ENV] == {
        "name": SESSION_STORAGE_PATH_ENV,
        "value": "/app/session_state",
    }


def test_knowledge_storage_mounts_existing_claim_where_the_runtime_keeps_indexes(tmp_path: Path) -> None:
    """Knowledge storage overlays <storage.mountPath>/knowledge_db and creates no PVC for an existing claim."""
    docs, deployment, container = _render(
        tmp_path,
        {"storage": {"mountPath": "/data/"}, "knowledgeStorage": {"enabled": True, "existingClaim": "kb-index"}},
    )

    assert "mindroom-runtime-knowledge" not in _pvcs(docs)
    assert _volumes_by_name(deployment)["knowledge-storage"] == {
        "name": "knowledge-storage",
        "persistentVolumeClaim": {"claimName": "kb-index"},
    }
    assert {"name": "knowledge-storage", "mountPath": "/data/knowledge_db"} in container["volumeMounts"]


@pytest.mark.parametrize(
    ("values", "expected_error"),
    [
        (
            {"stateStorage": {**_STATE, "extraSubPaths": [{"mountPath": "/app/x"}]}},
            "stateStorage.extraSubPaths[0].name is required",
        ),
        (
            {
                "stateStorage": {
                    **_STATE,
                    "extraSubPaths": [{"name": "x", "mountPath": "/app/x"}, {"name": "x", "mountPath": "/app/y"}],
                },
            },
            'stateStorage.extraSubPaths[1].name "x" duplicates another stateStorage.extraSubPaths entry',
        ),
        (
            {"stateStorage": {**_STATE, "extraSubPaths": [{"name": "x", "mountPath": "app/x"}]}},
            "stateStorage.extraSubPaths[0].mountPath must be an absolute path",
        ),
        (
            {
                "stateStorage": {
                    **_STATE,
                    "extraSubPaths": [{"name": "x", "mountPath": "/app/agent_data/encryption_keys"}],
                },
            },
            "stateStorage.extraSubPaths[0].mountPath must differ from stateStorage.encryptionKeys.mountPath",
        ),
        (
            {"stateStorage": {**_STATE, "extraSubPaths": [{"name": "x", "mountPath": "/app/x", "subPath": "a/../b"}]}},
            'stateStorage.extraSubPaths[0].subPath "a/../b" must be a relative path',
        ),
        (
            {"stateStorage": {**_STATE, "extraSubPaths": [{"name": "x", "mountPath": "/app/x", "subPath": '"; id'}]}},
            "stateStorage.extraSubPaths[0].subPath",
        ),
        (
            {"stateStorage": {**_STATE, "extraSubPaths": [{"name": "sync_continuity", "mountPath": "/app/x"}]}},
            "stateStorage.extraSubPaths[0].subPath must differ from stateStorage.syncContinuity.subPath",
        ),
        (
            {"sessionStorage": {"enabled": True, "mountPath": "session_state"}},
            "sessionStorage.mountPath must be an absolute path",
        ),
        (
            {"sessionStorage": {"enabled": True, "mountPath": "/app/agent_data/"}},
            "sessionStorage.mountPath must differ from storage.mountPath",
        ),
        (
            {
                "sessionStorage": {"enabled": True},
                "extraVolumes": [{"name": "sessions", "emptyDir": {}}],
                "extraVolumeMounts": [{"name": "sessions", "mountPath": "/app/session_state/"}],
            },
            "sessionStorage.mountPath must differ from extraVolumeMounts[0].mountPath",
        ),
        (
            {
                "sessionStorage": {"enabled": True},
                "env": {"extra": [{"name": SESSION_STORAGE_PATH_ENV, "value": "/elsewhere"}]},
            },
            f"env.extra must not set {SESSION_STORAGE_PATH_ENV} when sessionStorage.enabled=true",
        ),
        (
            {
                "stateStorage": {
                    **_STATE,
                    "extraSubPaths": [{"name": "kb", "mountPath": "/app/agent_data/knowledge_db"}],
                },
                "knowledgeStorage": {"enabled": True},
            },
            "/app/agent_data/knowledge_db, where knowledgeStorage is mounted, must differ from "
            "stateStorage.extraSubPaths[0].mountPath",
        ),
    ],
)
def test_storage_options_reject_invalid_values(tmp_path: Path, values: dict[str, Any], expected_error: str) -> None:
    """Invalid entries and mount or subpath collisions fail at render time."""
    completed = _run_helm_template(RUNTIME_CHART, values_files=_values_files(tmp_path, values))

    assert completed.returncode != 0
    assert expected_error in completed.stderr


@pytest.mark.parametrize(
    ("config_path", "values"),
    [
        (
            "/app/agent_data/runtime-config/config.yaml",
            {
                "stateStorage": {
                    **_STATE,
                    "extraSubPaths": [{"name": "x", "mountPath": "/app/agent_data/runtime-config"}],
                },
            },
        ),
        (
            "/app/agent_data/runtime-config/config.yaml",
            {"sessionStorage": {"enabled": True, "mountPath": "/app/agent_data/runtime-config"}},
        ),
        ("/app/agent_data/knowledge_db/config.yaml", {"knowledgeStorage": {"enabled": True}}),
    ],
)
def test_bootstrap_config_directory_cannot_be_a_new_storage_mount(
    tmp_path: Path,
    config_path: str,
    values: dict[str, Any],
) -> None:
    """Native bootstrap must not write its config directory onto one of the new mounts."""
    bootstrap = {
        "config": {"source": "file", "path": config_path, "bootstrapBundlePath": "/bundle"},
        "workers": {"backend": "kubernetes"},
    }
    completed = _run_helm_template(RUNTIME_CHART, values_files=_values_files(tmp_path, {**bootstrap, **values}))

    assert completed.returncode != 0
    assert "config.path directory overlaps a mounted volume" in completed.stderr
