"""Rendered runtime chart checks for extra state subpaths and dedicated session and knowledge volumes."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from mindroom.runtime_env_policy import SESSION_STORAGE_PATH_ENV

RUNTIME_CHART_DIR = Path(__file__).resolve().parents[1] / "cluster" / "k8s" / "runtime"
_BASE_VALUES: dict[str, Any] = {"eventCache": {"postgres": {"auth": {"password": "test-password"}}}}
_STATE = {"enabled": True, "existingClaim": "mindroom-state"}


def _helm_template(tmp_path: Path, values: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for rendered chart checks")
    values_path = tmp_path / "values.yaml"
    values_path.write_text(yaml.safe_dump({**_BASE_VALUES, **values}), encoding="utf-8")
    return subprocess.run(
        [helm, "template", "mindroom-runtime", str(RUNTIME_CHART_DIR), "--values", str(values_path)],
        check=False,
        capture_output=True,
        text=True,
    )


def _render(tmp_path: Path, values: dict[str, Any]) -> list[dict[str, Any]]:
    completed = _helm_template(tmp_path, values)
    assert completed.returncode == 0, completed.stderr
    return [doc for doc in yaml.safe_load_all(completed.stdout) if isinstance(doc, dict)]


def _pod_spec(docs: list[dict[str, Any]]) -> dict[str, Any]:
    deployment = next(
        doc for doc in docs if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "mindroom-runtime"
    )
    return deployment["spec"]["template"]["spec"]


def _runtime_container(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return next(container for container in _pod_spec(docs)["containers"] if container["name"] == "mindroom")


def _pvcs(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {doc["metadata"]["name"]: doc["spec"] for doc in docs if doc["kind"] == "PersistentVolumeClaim"}


def test_storage_options_render_nothing_by_default(tmp_path: Path) -> None:
    """Unset session and knowledge storage add no PVC, volume, mount, or env."""
    docs = _render(tmp_path, {})
    container = _runtime_container(docs)

    new_volumes = {"session-storage", "knowledge-storage"}

    assert set(_pvcs(docs)).isdisjoint({"mindroom-runtime-sessions", "mindroom-runtime-knowledge"})
    assert {volume["name"] for volume in _pod_spec(docs)["volumes"]}.isdisjoint(new_volumes)
    assert {mount["name"] for mount in container["volumeMounts"]}.isdisjoint(new_volumes)
    assert SESSION_STORAGE_PATH_ENV not in {entry["name"] for entry in container["env"]}


def test_state_storage_extra_subpaths_are_mounted_and_prepared_by_the_init_container(tmp_path: Path) -> None:
    """Extra state subpaths are created and chowned before the kubelet would create them as root."""
    docs = _render(
        tmp_path,
        {
            "stateStorage": {
                "enabled": True,
                "existingClaim": "mindroom-state",
                "extraSubPaths": [
                    {"name": "tracking", "mountPath": "/app/agent_data/tracking"},
                    {"name": "cache", "mountPath": "/app/cache", "subPath": "nested/cache"},
                ],
            },
        },
    )
    mounts = {mount["mountPath"]: mount for mount in _runtime_container(docs)["volumeMounts"]}
    init_container = next(
        container for container in _pod_spec(docs)["initContainers"] if container["name"] == "prepare-state-storage"
    )
    dirs = '"/state" "/state/encryption_keys" "/state/sync_continuity" "/state/tracking" "/state/nested/cache"'

    assert mounts["/app/agent_data/tracking"] == {
        "name": "state-storage",
        "mountPath": "/app/agent_data/tracking",
        "subPath": "tracking",
    }
    assert mounts["/app/cache"] == {"name": "state-storage", "mountPath": "/app/cache", "subPath": "nested/cache"}
    assert init_container["command"][2].splitlines()[1:] == [
        f"mkdir -p {dirs}",
        f"chown -R 1000:1000 {dirs}",
        f"chmod 2775 {dirs}",
    ]


def test_state_storage_extra_subpaths_stay_strings_when_they_look_like_numbers_or_booleans(tmp_path: Path) -> None:
    """Subpaths YAML would otherwise retype, from name or subPath, still render as strings."""
    docs = _render(
        tmp_path,
        {
            "stateStorage": {
                **_STATE,
                "extraSubPaths": [
                    {"name": "2026", "mountPath": "/app/a"},
                    {"name": "b", "mountPath": "/app/b", "subPath": "on"},
                ],
            },
        },
    )
    mounts = {mount["mountPath"]: mount for mount in _runtime_container(docs)["volumeMounts"]}

    assert mounts["/app/a"]["subPath"] == "2026"
    assert mounts["/app/b"]["subPath"] == "on"


def test_session_storage_creates_a_pvc_and_points_the_runtime_at_it(tmp_path: Path) -> None:
    """Chart-managed session storage gets its own PVC, mount, and session storage path."""
    docs = _render(
        tmp_path,
        {"sessionStorage": {"enabled": True, "size": "20Gi", "storageClassName": "fast-rwo"}},
    )
    container = _runtime_container(docs)
    env = {entry["name"]: entry for entry in container["env"]}
    volumes = {volume["name"]: volume for volume in _pod_spec(docs)["volumes"]}

    assert _pvcs(docs)["mindroom-runtime-sessions"] == {
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "fast-rwo",
        "resources": {"requests": {"storage": "20Gi"}},
    }
    assert volumes["session-storage"] == {
        "name": "session-storage",
        "persistentVolumeClaim": {"claimName": "mindroom-runtime-sessions"},
    }
    assert {"name": "session-storage", "mountPath": "/app/session_state"} in container["volumeMounts"]
    assert env[SESSION_STORAGE_PATH_ENV] == {"name": SESSION_STORAGE_PATH_ENV, "value": "/app/session_state"}


def test_knowledge_storage_mounts_existing_claim_where_the_runtime_keeps_indexes(tmp_path: Path) -> None:
    """Knowledge storage overlays <storage.mountPath>/knowledge_db and creates no PVC for an existing claim."""
    docs = _render(
        tmp_path,
        {"storage": {"mountPath": "/data/"}, "knowledgeStorage": {"enabled": True, "existingClaim": "kb-index"}},
    )
    volumes = {volume["name"]: volume for volume in _pod_spec(docs)["volumes"]}

    assert "mindroom-runtime-knowledge" not in _pvcs(docs)
    assert volumes["knowledge-storage"] == {
        "name": "knowledge-storage",
        "persistentVolumeClaim": {"claimName": "kb-index"},
    }
    assert {"name": "knowledge-storage", "mountPath": "/data/knowledge_db"} in _runtime_container(docs)["volumeMounts"]


def test_knowledge_storage_can_create_a_pvc(tmp_path: Path) -> None:
    """Knowledge storage creates its own PVC when no existing claim is set."""
    docs = _render(tmp_path, {"knowledgeStorage": {"enabled": True, "accessModes": ["ReadWriteMany"]}})

    assert _pvcs(docs)["mindroom-runtime-knowledge"] == {
        "accessModes": ["ReadWriteMany"],
        "resources": {"requests": {"storage": "10Gi"}},
    }


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
    completed = _helm_template(tmp_path, values)

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
    completed = _helm_template(
        tmp_path,
        {
            "config": {"source": "file", "path": config_path, "bootstrapBundlePath": "/bundle"},
            "workers": {"backend": "kubernetes"},
            **values,
        },
    )

    assert completed.returncode != 0
    assert "config.path directory overlaps a mounted volume" in completed.stderr
