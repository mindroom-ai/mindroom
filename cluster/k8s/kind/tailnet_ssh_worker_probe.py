"""A primary's steps for one tailnet SSH worker, run in a pod of the kind cluster by ``smoke_tailnet_ssh_worker``.

That pod is the one the runtime chart gives a primary, with this file as its command: its service account has the
chart's worker manager Role, it mounts the storage claim workers mount, and its environment configures the Kubernetes
backend. Only such a pod reaches a worker's runner and writes the files the worker's sshd reads.
The worker is started through the primary's worker manager first without the tailnet SSH mark, then with it, then
without it again, and the cluster's API server is asked what each start left. While the worker has the mark, the pod
that stands in for the gateway (``tailnet_ssh_gateway_stand_in.py``) is told its sshd's address and reads there.
"""

from __future__ import annotations

import os
import socket
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kubernetes import client
from kubernetes import config as kubernetes_config

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.api.tailnet_ssh_gateway import PORT_ENV
from mindroom.config.main import load_config
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.tailnet_ssh.projection import write_projection
from mindroom.tailnet_ssh.store import new_link
from mindroom.terminal.resolution import agent_terminals
from mindroom.tool_system.sandbox_proxy import worker_call
from mindroom.tool_system.worker_routing import build_requester_execution_identity
from mindroom.workers.backends.kubernetes import KubernetesWorkerBackendConfig
from mindroom.workers.models import WorkerSpec
from mindroom.workers.runtime import bind_worker_launch_config_source, lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.workers.models import WorkerSshEndpoint

_AGENT = "mine"
_REQUESTER = "@alice:example.org"
# The one kind of agent a launch marks: a worker per person and agent, for an agent that admits that person alone.
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
_API_PORT = ("api", 8766, "TCP", 8766)
_SSH_PORT = ("ssh", 2222, "TCP", 2222)
_UNMARKED = {
    "service ports": [_API_PORT],
    "deployment label": None,
    "pod template label": None,
    "pod label": None,
    "pod ports": [("api", 8766)],
}
_MARKED = {
    "service ports": [_API_PORT, _SSH_PORT],
    "deployment label": "true",
    "pod template label": "true",
    "pod label": "true",
    "pod ports": [("api", 8766), ("ssh", 2222)],
}


def _expect(what: str, actual: object, expected: object) -> None:
    if actual != expected:
        msg = f"{what}: expected {expected!r}, got {actual!r}"
        raise AssertionError(msg)
    print(f"[probe] {what}: {actual!r}", flush=True)


class _Cluster:
    """What the API server holds of one worker."""

    def __init__(self, namespace: str) -> None:
        kubernetes_config.load_incluster_config()
        self.namespace = namespace
        self._apps = client.AppsV1Api()
        self._core = client.CoreV1Api()

    def service_uid(self, worker_id: str) -> str:
        """Return the worker Service's UID, which a patch keeps and a delete and create replace."""
        return self._core.read_namespaced_service(worker_id, self.namespace).metadata.uid

    def pod(self, worker_id: str) -> client.V1Pod:
        """Return the worker's one pod that is not being deleted."""
        selector = self._apps.read_namespaced_deployment(worker_id, self.namespace).spec.selector.match_labels
        pods = self._core.list_namespaced_pod(
            self.namespace,
            label_selector=",".join(f"{name}={value}" for name, value in selector.items()),
        )
        [pod] = [pod for pod in pods.items if pod.metadata.deletion_timestamp is None]
        return pod

    def worker(self, worker_id: str) -> dict[str, object]:
        """Return the worker's Service ports, where it carries the tailnet SSH mark, and its pod's ports."""
        service = self._core.read_namespaced_service(worker_id, self.namespace)
        deployment = self._apps.read_namespaced_deployment(worker_id, self.namespace)
        pod = self.pod(worker_id)
        return {
            "service ports": [(port.name, port.port, port.protocol, port.target_port) for port in service.spec.ports],
            "deployment label": deployment.metadata.labels.get(_MARK),
            "pod template label": deployment.spec.template.metadata.labels.get(_MARK),
            "pod label": pod.metadata.labels.get(_MARK),
            "pod ports": [(port.name, port.container_port) for port in pod.spec.containers[0].ports],
        }


def _answers(host: str, port: int) -> bool:
    """Return whether a connection from this pod to ``host`` and ``port`` is accepted."""
    try:
        socket.create_connection((host, port), timeout=5).close()
    except OSError:
        return False
    return True


def _read_at_the_gateway(endpoint: WorkerSshEndpoint) -> bytes:
    """Tell the pod that stands in for the gateway where sshd is, and return what it read there first.

    That pod dials this one as the gateway dials the primary: at the Service and the port the chart gives the
    primary's listener for the gateway, which this pod serves for this one answer.
    """
    with socket.create_server(("", int(os.environ[PORT_ENV]))) as listener:
        listener.settimeout(120)
        asking, _ = listener.accept()
        with asking, asking.makefile("rwb") as stream:
            asking.settimeout(120)
            stream.write(f"{endpoint.host} {endpoint.port}\n".encode())
            stream.flush()
            return stream.read()


def _start_sshd(
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    worker_key: str,
    endpoint: WorkerSshEndpoint,
) -> None:
    """Write sshd's files into the worker's state and have its runner start sshd, as a connect the primary passed."""
    link = new_link(_REQUESTER, _AGENT, worker_key, _AGENT, now=time.time())
    client_key = Ed25519PrivateKey.generate().public_key()
    key_line = client_key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    # A connect whose keys admit nobody starts no sshd, so the files hold one key.
    write_projection(endpoint.projection_dir, link, [f"restrict,pty {key_line}"])
    terminals = agent_terminals(_AGENT, config, runtime_paths, identity, create=True)
    call = None if terminals is None else worker_call(terminals.stand_ins, SHELL_FUNCTION, start=False)
    if call is None:
        msg = "The agent's shell does not run in its ready worker."
        raise AssertionError(msg)
    response = httpx.post(
        f"{call.api_root}/ssh/start",
        headers=call.headers,
        json={"request": call.request.model_dump(mode="json")},
        timeout=60,
    )
    # The runner answers once sshd accepts on its port: it started on the host key the primary wrote 0600, as the
    # pod's uid, under its fsGroup and seccomp profile.
    answer = response.json() if response.status_code == 200 else response.text
    _expect("the runner's answer to /ssh/start", (response.status_code, answer), (200, {"port": 2222}))
    # The chart admits the gateway's pods alone to that port. Whether a cluster holds others off is its network
    # plugin's doing, so this pod's own attempt is only told: where it is accepted, the gateway's proves no admission.
    enforced = not _answers(endpoint.host, endpoint.port)
    print(f"[probe] the cluster keeps the primary from the Service's port 2222: {enforced}", flush=True)
    _expect("what the Service's port 2222 sends the gateway", _read_at_the_gateway(endpoint), b"SSH-2.0-")


def main() -> None:
    """Start one worker unmarked, marked, and unmarked again, and check what the API server holds after each."""
    config_path = Path(tempfile.mkdtemp()) / "config.yaml"
    config_path.write_text(_CONFIG, encoding="utf-8")
    # The storage root and the Kubernetes backend's settings come from the pod's environment, as a primary's do.
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path)
    marked = load_config(runtime_paths)
    unmarked = marked.model_copy(deep=True)
    unmarked.agents[_AGENT].tailnet_ssh = False
    identity = build_requester_execution_identity(
        agent_name=_AGENT,
        runtime_paths=runtime_paths,
        requester_id=_REQUESTER,
    )
    target = resolve_agent_execution(_AGENT, marked, runtime_paths, execution_identity=identity).worker_target
    if target.worker_key is None:
        msg = "The agent resolves to no worker for its requester."
        raise AssertionError(msg)
    spec = WorkerSpec(target.worker_key, private_agent_names=target.private_agent_names)
    cluster = _Cluster(KubernetesWorkerBackendConfig.from_runtime(runtime_paths).namespace)
    lease = lease_configured_primary_worker_manager(runtime_paths)
    if lease is None:
        msg = "The pod's environment configures no Kubernetes worker backend."
        raise AssertionError(msg)
    applied = [unmarked]
    with bind_worker_launch_config_source(lambda: applied[0]), lease as manager:
        handle = manager.ensure_worker(spec)
        worker_id = handle.worker_id
        _expect("unmarked worker", cluster.worker(worker_id), _UNMARKED)
        _expect("unmarked worker's SSH endpoint", handle.ssh_endpoint, None)
        service_uid = cluster.service_uid(worker_id)

        applied[0] = marked
        handle = manager.ensure_worker(spec)
        _expect("marked worker", cluster.worker(worker_id), _MARKED)
        _expect("marked worker's Service was patched, not replaced", cluster.service_uid(worker_id), service_uid)
        endpoint = handle.ssh_endpoint
        if endpoint is None:
            msg = "The marked worker's handle has no SSH endpoint."
            raise AssertionError(msg)
        _expect(
            "marked worker's SSH endpoint",
            (endpoint.host, endpoint.port),
            (f"{worker_id}.{cluster.namespace}.svc.cluster.local", 2222),
        )
        security = cluster.pod(worker_id).spec.security_context
        _expect(
            "marked pod's uid, fsGroup and seccomp profile",
            (security.run_as_user, security.fs_group, security.seccomp_profile.type),
            (1000, 1000, "RuntimeDefault"),
        )
        _start_sshd(marked, runtime_paths, identity, spec.worker_key, endpoint)

        applied[0] = unmarked
        handle = manager.ensure_worker(spec)
        _expect("worker that lost the mark", cluster.worker(worker_id), _UNMARKED)
        _expect("its Service was patched, not replaced", cluster.service_uid(worker_id), service_uid)
        _expect("its SSH endpoint", handle.ssh_endpoint, None)
    print("[probe] passed", flush=True)


if __name__ == "__main__":
    main()
