"""Run one worker through unmarked → marked → unmarked using the primary's chart identity."""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from tempfile import TemporaryDirectory

from kubernetes import client
from kubernetes.config import load_incluster_config

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.main import load_config
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.tailnet_ssh.links import connect, desired_links
from mindroom.tailnet_ssh.store import read_links
from mindroom.workers.backends.kubernetes import KubernetesWorkerBackendConfig
from mindroom.workers.models import LABEL_TAILNET_SSH, WORKER_SSH_PORT, WorkerSpec
from mindroom.workers.runtime import bind_worker_launch_config_source, lease_configured_primary_worker_manager

_CONFIG = """\
agents:
  mine:
    display_name: Mine
    worker_scope: shared
    tailnet_ssh: true
    tools: [shell]
    access: {users: ["@alice:example.org"]}
models:
  default: {provider: openai, id: gpt-6-astra}
router: {model: default}
tailnet_ssh:
  logins: {"@alice:example.org": alice}
  authorized_keys:
    "@alice:example.org": [ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPVHVwHKbi4VF0jfKNiYpfwAeEgeTqB6B8NVRZvzjnDS]
"""


def _banner(host: str, port: int) -> None:
    # The internal chart Service connects the gateway-labelled stand-in to this pod.
    with socket.create_server(("", int(os.environ["MINDROOM_TAILNET_SSH_PORT"]))) as listener:
        listener.settimeout(120)
        with listener.accept()[0] as gateway, gateway.makefile("rb") as stream:
            gateway.settimeout(30)
            gateway.sendall(f"{host} {port}\n".encode())
            banner = stream.read(8)
    assert banner == b"SSH-2.0-", banner
    print(f"[probe] gateway read {banner!r} through {host}:{port}", flush=True)


def main() -> None:
    """Verify an in-place Service update, worker pod identity and a banner through the Service."""
    load_incluster_config()
    core = client.CoreV1Api()
    with TemporaryDirectory() as directory:
        file = Path(directory) / "config.yaml"
        file.write_text(_CONFIG, encoding="utf-8")
        paths = resolve_primary_runtime_paths(config_path=file)
        config = load_config(paths)
        namespace = KubernetesWorkerBackendConfig.from_runtime(paths).namespace
        memberships = AgentReplyMembershipIndex()
        assert len(desired_links(config, paths, memberships)) == 1
        [link] = read_links(paths).links
        spec = WorkerSpec(link.worker_key)
        lease = lease_configured_primary_worker_manager(paths)
        assert lease is not None, "Expected a Kubernetes worker backend"
        uid = None
        with bind_worker_launch_config_source(lambda: config), lease as manager:
            for marked in (False, True, False):
                config.agents["mine"].tailnet_ssh = marked
                handle = manager.ensure_worker(spec)
                worker = handle.worker_id
                service = core.read_namespaced_service(worker, namespace)
                assert uid is None or service.metadata.uid == uid, "The Service was replaced"
                uid = service.metadata.uid
                ports = [(p.name, p.port, p.target_port) for p in service.spec.ports]
                assert ports == [("api", 8766, 8766)] + (
                    [("ssh", WORKER_SSH_PORT, WORKER_SSH_PORT)] if marked else []
                ), ports
                selector = ",".join(f"{k}={v}" for k, v in service.spec.selector.items())
                [pod] = [
                    p
                    for p in core.list_namespaced_pod(namespace, label_selector=selector).items
                    if not p.metadata.deletion_timestamp
                ]
                assert pod.metadata.labels.get(LABEL_TAILNET_SSH) == ("true" if marked else None)
                assert (handle.ssh_endpoint is not None) == marked
                if marked:
                    security = pod.spec.security_context
                    assert (security.run_as_user, security.fs_group, security.seccomp_profile.type) == (
                        1000,
                        1000,
                        "RuntimeDefault",
                    )
                    request = connect(lambda: (config, paths), memberships, link.id, "alice", "probe")
                    host, port = asyncio.run(request)
                    assert (host, port) == (f"{worker}.{namespace}.svc.cluster.local", WORKER_SSH_PORT)
                    _banner(host, port)


if __name__ == "__main__":
    main()
