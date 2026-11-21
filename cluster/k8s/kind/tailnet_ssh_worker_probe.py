"""Run one worker through unmarked → marked → unmarked using the primary's chart identity."""

from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kubernetes import client
from kubernetes import config as kubernetes_config

from mindroom.api.tailnet_ssh_gateway import PORT_ENV
from mindroom.config.main import load_config
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.tailnet_ssh.links import _shell_call
from mindroom.tailnet_ssh.projection import write_projection
from mindroom.tailnet_ssh.store import new_link
from mindroom.tool_system.worker_routing import build_requester_execution_identity
from mindroom.workers.backends.kubernetes import KubernetesWorkerBackendConfig
from mindroom.workers.models import WORKER_SSH_PORT, WorkerSpec
from mindroom.workers.runtime import bind_worker_launch_config_source, lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from mindroom.tailnet_ssh.store import Link
    from mindroom.tool_system.sandbox_proxy import WorkerCall
    from mindroom.workers.models import WorkerSshEndpoint

_AGENT = "mine"
_REQUESTER = "@alice:example.org"
_CONFIG = f"""\
agents:
  {_AGENT}:
    display_name: Mine
    role: Run shell commands.
    worker_scope: user_agent
    tailnet_ssh: true
    tools: [shell]
    access:
      users: ["{_REQUESTER}"]
      members_of_rooms: []
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
"""
_MARK = "mindroom.ai/tailnet-ssh"


def _check_resources(namespace: str, worker_id: str, *, marked: bool, service_uid: str | None) -> str:
    core = client.CoreV1Api()
    service = core.read_namespaced_service(worker_id, namespace)
    deployment = client.AppsV1Api().read_namespaced_deployment(worker_id, namespace)
    selector = ",".join(f"{name}={value}" for name, value in deployment.spec.selector.match_labels.items())
    [pod] = [
        pod
        for pod in core.list_namespaced_pod(namespace, label_selector=selector).items
        if not pod.metadata.deletion_timestamp
    ]
    port = WORKER_SSH_PORT
    expected = [("api", 8766, "TCP", 8766)] + ([("ssh", port, "TCP", port)] if marked else [])
    ports = [(p.name, p.port, p.protocol, p.target_port) for p in service.spec.ports]
    assert ports == expected, ports
    pod_ports = [(p.name, p.container_port) for p in pod.spec.containers[0].ports]
    assert pod_ports == [(name, port) for name, port, _, _ in expected], pod_ports
    for resource in (deployment, deployment.spec.template, pod):
        assert resource.metadata.labels.get(_MARK) == ("true" if marked else None), resource.metadata.labels
    if marked:
        security = pod.spec.security_context
        assert (security.run_as_user, security.fs_group, security.seccomp_profile.type) == (
            1000,
            1000,
            "RuntimeDefault",
        )
    uid = service.metadata.uid
    assert service_uid is None or uid == service_uid, f"Service replaced: {service_uid} → {uid}"
    print(f"[probe] marked={marked} Service UID={uid} ports={ports}, labels and pod settings checked", flush=True)
    return uid


def _start_and_read_banner(endpoint: WorkerSshEndpoint, call: WorkerCall, link: Link) -> None:
    client_key = Ed25519PrivateKey.generate().public_key()
    key = client_key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    write_projection(endpoint.projection_dir, link, [f"restrict,pty {key}"])
    response = httpx.post(
        f"{call.api_root}/ssh/start",
        headers=call.headers,
        json={"request": call.request.model_dump(mode="json")},
        timeout=60,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"port": WORKER_SSH_PORT}, response.text
    print(f"[probe] /ssh/start returned {response.text}", flush=True)
    # The chart's internal listener Service connects the gateway-labelled stand-in to this pod.
    with socket.create_server(("", int(os.environ[PORT_ENV]))) as listener:
        listener.settimeout(120)
        gateway, _ = listener.accept()
        with gateway, gateway.makefile("rb") as stream:
            gateway.settimeout(120)
            gateway.sendall(f"{endpoint.host} {endpoint.port}\n".encode())
            banner = stream.read(8)
    assert banner == b"SSH-2.0-", banner
    print(f"[probe] gateway read {banner!r} through {endpoint.host}:{endpoint.port}", flush=True)


def main() -> None:
    """Verify Service patches, pod marks, endpoints and the sshd banner through the Service."""
    config_path = Path(tempfile.mkdtemp()) / "config.yaml"
    config_path.write_text(_CONFIG, encoding="utf-8")
    paths = resolve_primary_runtime_paths(config_path=config_path)
    config = load_config(paths)
    identity = build_requester_execution_identity(agent_name=_AGENT, runtime_paths=paths, requester_id=_REQUESTER)
    target = resolve_agent_execution(_AGENT, config, paths, execution_identity=identity).worker_target
    assert target.worker_key, "Expected a dedicated worker"
    spec = WorkerSpec(target.worker_key, private_agent_names=target.private_agent_names)
    kubernetes_config.load_incluster_config()
    namespace = KubernetesWorkerBackendConfig.from_runtime(paths).namespace
    lease = lease_configured_primary_worker_manager(paths)
    assert lease is not None, "Expected a Kubernetes worker backend"
    uid = None
    with bind_worker_launch_config_source(lambda: config), lease as manager:
        for marked in (False, True, False):
            config.agents[_AGENT].tailnet_ssh = marked
            handle = manager.ensure_worker(spec)
            uid = _check_resources(namespace, handle.worker_id, marked=marked, service_uid=uid)
            endpoint = handle.ssh_endpoint
            if marked:
                assert endpoint is not None
                expected = (f"{handle.worker_id}.{namespace}.svc.cluster.local", WORKER_SSH_PORT)
                assert (endpoint.host, endpoint.port) == expected, endpoint
                link = new_link(_REQUESTER, _AGENT)
                _start_and_read_banner(endpoint, _shell_call(config, paths, link, target), link)
            else:
                assert endpoint is None, endpoint


if __name__ == "__main__":
    main()
