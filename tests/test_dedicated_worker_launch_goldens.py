"""What each dedicated worker backend sends to start a worker stays byte for byte the same.

The goldens hold the Kubernetes Deployment bodies and annotation patches, and the Docker ``containers.run``
arguments, rendered for a fixed set of worker scopes and options, plus the startup manifest each worker boots from.
A worker must never receive a mount, secret, or environment value it did not receive before, so any change to a
golden is a reviewed change to what reaches a worker.
Values derived from the test's temporary directory are replaced by placeholders.
Regenerate with ``MINDROOM_UPDATE_WORKER_LAUNCH_GOLDENS=1``, then review the diff.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import load_config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths, sandbox_startup_manifest_path
from mindroom.script_runs.models import script_worker_key_for_run
from mindroom.tool_system.worker_routing import worker_dir_name
from mindroom.workers.backends.dedicated_launch import (
    DedicatedWorkerLayout,
    dedicated_worker_runner_env,
    dedicated_worker_startup_runtime_paths,
)
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION
from tests.test_docker_worker_backend import _backend as _docker_test_backend
from tests.test_docker_worker_backend import _docker_backend, _FakeDockerClient
from tests.test_docker_worker_backend import _spec as _docker_spec
from tests.test_kubernetes_worker_backend import (
    _answer_worker_health,
    _materialize_private_workspace,
    _test_agent_vault_config,
)
from tests.test_kubernetes_worker_backend import _backend as _kubernetes_test_backend
from tests.test_kubernetes_worker_backend import _spec as _kubernetes_spec

if TYPE_CHECKING:
    from mindroom.workers.backends.docker import DockerWorkerBackend
    from mindroom.workers.backends.kubernetes import KubernetesWorkerBackend

_GOLDEN_DIR = Path(__file__).parent / "fixtures" / "dedicated_worker_launch"
_UPDATE_ENV = "MINDROOM_UPDATE_WORKER_LAUNCH_GOLDENS"

# A primary environment with a name of every kind the startup filters treat differently.
_PRIMARY_ENV = {
    "BROWSER_EXECUTABLE_PATH": "/usr/bin/chromium",
    "CUSTOMER_ID": "customer-1",
    "DO_NOT_TRACK": "0",
    "GOOGLE_CLOUD_PROJECT": "golden-project",
    "MATRIX_HOMESERVER": "https://matrix.example.test",
    "MINDROOM_KUBERNETES_WORKER_IMAGE": "ghcr.io/example/primary-only:1",
    "MINDROOM_LOG_LEVEL": "INFO",
    "MINDROOM_SANDBOX_PROXY_TOKEN": "primary-proxy-token",
    "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS": "30",
    "OPENAI_API_KEY": "sk-golden-secret",
    "OPENAI_BASE_URL": "https://llm.example.test/v1",
    "SERVICE_TOKEN_FILE": "/run/secrets/service-token",
    "UNRELATED_SETTING": "kept-out",
}
# Operator worker env with names each backend keeps or drops differently.
_EXTRA_ENV = {
    "DO_NOT_TRACK": "0",
    "EXTRA_ENV": "present",
    "MINDROOM_SANDBOX_PROXY_URL": "http://operator-proxy.example.test",
    "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS": "45",
    "MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH": "/operator/manifest.json",
    "OPENAI_BASE_URL": "https://extra-llm.example.test/v1",
}
_CONFIG_TEMPLATE = """
agents:
  code:
    display_name: Code
    role: Code test
    model: default
    worker_scope: shared
    knowledge_bases: [docs]
  helper:
    display_name: Helper
    role: Helper test
    model: default
    worker_scope: user
  solo:
    display_name: Solo
    role: Solo test
    model: default
  mind:
    display_name: Mind
    role: Mind test
    model: default
    private:
      per: user_agent
knowledge_bases:
  docs:
    path: {knowledge_path}
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
"""
# The Kubernetes startup manifest records the primary's config path, so it is fixed outside the temporary directory.
_KUBERNETES_PRIMARY_CONFIG_PATH = Path("/srv/mindroom-golden/config.yaml")
_SHARED_KEY = "v1:tenant-123:shared:code"
_USER_KEY = "v1:tenant-123:user:~@alice:example.org"
_USER_AGENT_KEY = "v1:tenant-123:user_agent:~@alice:example.org:mind"
_UNSCOPED_KEY = "v1:tenant-123:unscoped:solo"
_SCRIPT_KEY = script_worker_key_for_run(_USER_AGENT_KEY, f"script-{'a' * 32}")


def _write_config(root: Path) -> Path:
    storage_root = root / "storage"
    knowledge_root = storage_root / "agents" / "code" / "knowledge"
    knowledge_root.mkdir(parents=True)
    (storage_root / "agents" / "code" / "workspace").mkdir(parents=True)
    config_path = root / "config.yaml"
    config_path.write_text(_CONFIG_TEMPLATE.format(knowledge_path=knowledge_root).lstrip(), encoding="utf-8")
    return config_path


def _normalized(value: object, replacements: dict[str, str]) -> object:
    text = json.dumps(value, indent=2, default=str)
    for old, new in sorted(replacements.items(), key=lambda item: -len(item[0])):
        text = text.replace(old, new)
    return json.loads(text)


def _assert_matches_golden(name: str, rendered: object) -> None:
    path = _GOLDEN_DIR / f"{name}.json"
    text = json.dumps(rendered, indent=2) + "\n"
    if os.environ.get(_UPDATE_ENV):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def _manifest_text(storage_root: Path, worker_key: str) -> object:
    manifest_path = sandbox_startup_manifest_path(storage_root / "workers" / worker_dir_name(worker_key))
    return json.loads(manifest_path.read_text(encoding="utf-8"))


_KUBERNETES_CASES = {
    "shared_with_knowledge": {"worker_key": _SHARED_KEY},
    "shared_with_agent_vault": {"worker_key": _SHARED_KEY, "agent_vault": True},
    "user": {"worker_key": _USER_KEY},
    "user_agent_private": {"worker_key": _USER_AGENT_KEY, "private": True},
    "unscoped": {"worker_key": _UNSCOPED_KEY},
    "script_scoped_with_agent_vault": {
        "worker_key": _SCRIPT_KEY,
        "state_scope_worker_key": _USER_AGENT_KEY,
        "private": True,
        "agent_vault": True,
    },
}


def _kubernetes_backend(tmp_path: Path, *, agent_vault: bool) -> KubernetesWorkerBackend:
    config_path = _write_config(tmp_path)
    storage_root = tmp_path / "storage"
    applied_config = load_config(resolve_primary_runtime_paths(config_path=config_path, storage_path=storage_root))
    runtime_paths = resolve_primary_runtime_paths(
        config_path=_KUBERNETES_PRIMARY_CONFIG_PATH,
        storage_path=storage_root,
        process_env=dict(_PRIMARY_ENV),
    )
    backend, _apps_api, _core_api = _kubernetes_test_backend(
        runtime_paths=runtime_paths,
        applied_config=applied_config,
        extra_env=dict(_EXTRA_ENV),
        agent_vault=_test_agent_vault_config(worker_ca_configmap_name="agent-vault-ca") if agent_vault else None,
    )
    return backend


@pytest.mark.parametrize("case", sorted(_KUBERNETES_CASES))
def test_kubernetes_worker_deployment_matches_its_golden(tmp_path: Path, case: str) -> None:
    """The Deployment body, its readiness patch, and the startup manifest of each worker kind are unchanged."""
    options = _KUBERNETES_CASES[case]
    worker_key = str(options["worker_key"])
    backend = _kubernetes_backend(tmp_path, agent_vault=bool(options.get("agent_vault")))
    apps_api = backend._resources.apps_api
    if options.get("private"):
        _materialize_private_workspace(backend.storage_root, _USER_AGENT_KEY, "@alice:example.org")
    spec = _kubernetes_spec(
        worker_key,
        private_agent_names=frozenset({"mind"}),
        state_scope_worker_key=options.get("state_scope_worker_key"),
    )

    backend.ensure_worker(spec, now=10.0)

    _assert_matches_golden(
        f"kubernetes_{case}",
        _normalized(
            {
                "created": apps_api.created_bodies,
                "patched": apps_api.patched_bodies,
                "manifest": _manifest_text(backend.storage_root, worker_key),
            },
            {str(tmp_path): "<TMP>"},
        ),
    )


def test_kubernetes_worker_lifecycle_annotations_match_their_golden(tmp_path: Path) -> None:
    """Cold start, a warm re-check, a cached touch, and a stale-protocol replacement write the same annotations."""
    backend = _kubernetes_backend(tmp_path, agent_vault=False)
    apps_api = backend._resources.apps_api
    spec = _kubernetes_spec(_SHARED_KEY)

    backend.ensure_worker(spec, now=10.0)
    backend.shutdown()
    backend.ensure_worker(spec, now=20.0)
    backend.ensure_worker(spec, now=25.0)
    backend.touch_worker(_SHARED_KEY, now=26.0)
    backend.shutdown()
    _answer_worker_health(backend, lambda: WORKER_PROTOCOL_VERSION if len(apps_api.created_bodies) > 1 else 0)
    backend.ensure_worker(spec, now=30.0)

    _assert_matches_golden(
        "kubernetes_lifecycle",
        _normalized(
            {
                "created_annotations": [body["metadata"]["annotations"] for body in apps_api.created_bodies],
                "patched_annotations": [body["metadata"]["annotations"] for _name, body in apps_api.patched_bodies],
                "deleted": apps_api.deleted_names,
            },
            {str(tmp_path): "<TMP>"},
        ),
    )


_DOCKER_CASES = {
    "shared": {"worker_key": _SHARED_KEY},
    "user": {"worker_key": _USER_KEY},
    "user_agent_private": {"worker_key": _USER_AGENT_KEY, "private": True},
    "unscoped": {"worker_key": _UNSCOPED_KEY},
    "script_scoped": {"worker_key": _SCRIPT_KEY, "state_scope_worker_key": _USER_AGENT_KEY, "private": True},
    "computer_runtime_without_host_config": {"worker_key": _SHARED_KEY, "computer": True},
}


def _docker_backend_for(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    computer: bool,
) -> tuple[DockerWorkerBackend, _FakeDockerClient]:
    config_path = _write_config(tmp_path)
    storage_root = tmp_path / "storage"
    runtime_paths: RuntimePaths = resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=storage_root,
        process_env=dict(_PRIMARY_ENV),
    )
    base_backend, fake_client, _sync_calls = _docker_test_backend(
        monkeypatch,
        tmp_path,
        config_text=config_path.read_text(encoding="utf-8"),
        runtime_paths=runtime_paths,
        storage_root=storage_root,
        host_config_path=config_path,
    )
    config = replace(base_backend.config, extra_env=dict(_EXTRA_ENV))
    if computer:
        config = replace(config, security_policy="computer", runtime="runsc", host_config_path=None)
    backend = _docker_backend(
        config=config,
        auth_token=base_backend.auth_token,
        storage_root=storage_root,
        runtime_paths=runtime_paths,
    )
    monkeypatch.setattr(backend, "_wait_for_ready", lambda _container, **_kwargs: None)
    return backend, fake_client


@pytest.mark.parametrize("case", sorted(_DOCKER_CASES))
def test_docker_worker_run_arguments_match_their_golden(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
) -> None:
    """The ``containers.run`` arguments and the startup manifest of each worker kind are unchanged."""
    options = _DOCKER_CASES[case]
    worker_key = str(options["worker_key"])
    backend, fake_client = _docker_backend_for(monkeypatch, tmp_path, computer=bool(options.get("computer")))
    storage_root = tmp_path / "storage"
    if options.get("private"):
        _materialize_private_workspace(storage_root, _USER_AGENT_KEY, "@alice:example.org")
    spec = _docker_spec(
        backend,
        worker_key,
        private_agent_names=frozenset({"mind"}),
        state_scope_worker_key=options.get("state_scope_worker_key"),
    )

    handle = backend.ensure_worker(spec, now=10.0)

    run_call = dict(fake_client.containers.run_calls[0])
    environment = dict(run_call["environment"])
    token_name = "MINDROOM_SANDBOX_PROXY_TOKEN"  # noqa: S105
    assert environment[token_name] == backend._control_token(worker_key)
    environment[token_name] = "<WORKER_TOKEN>"
    # Docker sets the environment as a mapping, so its order carries no meaning.
    run_call["environment"] = dict(sorted(environment.items()))
    manifest_path = sandbox_startup_manifest_path(Path(str(handle.debug_metadata["state_root"])))
    labels = dict(run_call["labels"])
    labels["mindroom.ai/launch-config-hash"] = "<LAUNCH_CONFIG_HASH>"
    # The manifest the worker boots from is pinned below, so the label pins only that it names that manifest.
    assert labels["mindroom.ai/startup-manifest-hash"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    labels["mindroom.ai/startup-manifest-hash"] = "<STARTUP_MANIFEST_HASH>"
    run_call["labels"] = labels
    # The seccomp profile is long and owned by its own tests, so the golden pins its digest.
    if "security_opt" in run_call:
        run_call["security_opt"] = [
            f"seccomp=<sha256:{hashlib.sha256(option.encode()).hexdigest()}>"
            if option.startswith("seccomp=")
            else option
            for option in run_call["security_opt"]
        ]
    # A projection directory is named after a digest of the projected files, which hold temporary paths.
    run_call["volumes"] = [
        re.sub(r"config-projection-[0-9a-f]+", "config-projection-<DIGEST>", volume) for volume in run_call["volumes"]
    ]
    _assert_matches_golden(
        f"docker_{case}",
        _normalized(
            {
                "run": run_call,
                "manifest": json.loads(manifest_path.read_text(encoding="utf-8")),
            },
            {
                str(tmp_path): "<TMP>",
                backend._runtime_namespace: "<RUNTIME_NAMESPACE>",
                handle.worker_id: "<CONTAINER_NAME>",
            },
        ),
    )


def test_startup_manifest_records_normalized_container_paths() -> None:
    """The container environment keeps a backend's path spelling, and the startup manifest the normalized path."""
    layout = DedicatedWorkerLayout(dedicated_root="/app/worker/", config_path="/app//config.yaml", worker_port=8766)

    runner_env = dedicated_worker_runner_env(layout, worker_key=_UNSCOPED_KEY)
    runtime_paths = dedicated_worker_startup_runtime_paths(
        layout,
        worker_key=_UNSCOPED_KEY,
        process_env={},
        env_file_values={},
    )

    assert runner_env["MINDROOM_STORAGE_PATH"] == "/app/worker/"
    assert runner_env["MINDROOM_CONFIG_PATH"] == "/app//config.yaml"
    assert runtime_paths.process_env["MINDROOM_STORAGE_PATH"] == "/app/worker"
    assert runtime_paths.process_env["MINDROOM_SHARED_CREDENTIALS_PATH"] == "/app/worker/.shared_credentials"
    assert runtime_paths.process_env["MINDROOM_CONFIG_PATH"] == "/app/config.yaml"


def test_startup_runtime_holds_only_public_startup_values() -> None:
    """The runtime a worker boots from is what its manifest records, so no secret or control value is in it."""
    runtime_paths = dedicated_worker_startup_runtime_paths(
        DedicatedWorkerLayout(dedicated_root="/app/worker", config_path="/app/config.yaml", worker_port=8766),
        worker_key=_UNSCOPED_KEY,
        process_env=_PRIMARY_ENV,
        env_file_values={"MATRIX_HOMESERVER": "https://dotenv.example.test", "ANTHROPIC_API_KEY": "sk-dotenv"},
    )

    assert runtime_paths.process_env["MATRIX_HOMESERVER"] == "https://matrix.example.test"
    assert "OPENAI_API_KEY" not in runtime_paths.process_env
    assert "MINDROOM_SANDBOX_PROXY_TOKEN" not in runtime_paths.process_env
    assert "MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH" not in runtime_paths.process_env
    assert dict(runtime_paths.env_file_values) == {"MATRIX_HOMESERVER": "https://dotenv.example.test"}
