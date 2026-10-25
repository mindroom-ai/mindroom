"""Worker roots and OAuth credential stores keyed by an agent's worker keys."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.constants import write_startup_manifest
from mindroom.credentials.manager import (
    get_runtime_credentials_manager,
)
from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
from mindroom.oauth.credential_store import oauth_credential_reader, oauth_credential_transaction
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    resolve_worker_key,
    resolve_worker_target,
    worker_dir_name,
)
from mindroom.workers.backends._dedicated_worker_common import build_dedicated_worker_runtime_paths

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.oauth.providers import OAuthProvider


ALICE = "@alice:x"


def agent_worker_identity(agent: str, requester_id: str = ALICE) -> ToolExecutionIdentity:
    """Return the execution identity of ``agent`` for a requester."""
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent,
        requester_id=requester_id,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )


def agent_worker_key(scope: WorkerScope, agent: str, requester_id: str = ALICE) -> str:
    """Return the worker key of ``agent`` in ``scope``."""
    key = resolve_worker_key(scope, agent_worker_identity(agent, requester_id))
    assert key is not None
    return key


def write_json(path: Path, payload: object) -> None:
    """Write ``payload`` as JSON to ``path``, with its parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def worker_root(runtime_paths: RuntimePaths, worker_key: str) -> Path:
    """Return the state root of a worker key."""
    return runtime_paths.storage_root / "workers" / worker_dir_name(worker_key)


def write_worker(runtime_paths: RuntimePaths, worker_key: str) -> Path:
    """Write a worker root with metadata, a workspace file and a venv whose script names the root."""
    root = worker_root(runtime_paths, worker_key)
    write_json(
        root / "metadata" / "worker.json",
        {"status": "idle", "worker_id": worker_dir_name(worker_key), "worker_key": worker_key},
    )
    (root / "workspace").mkdir()
    (root / "workspace" / "notes.md").write_text(f"notes of {worker_key}", encoding="utf-8")
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "pip").write_text(f"#!{root}/venv/bin/python\n", encoding="utf-8")
    return root


def write_docker_startup_manifest(runtime_paths: RuntimePaths, worker_key: str) -> Path:
    """Write the startup manifest the Docker backend writes into a worker root before creating its container."""
    worker_runtime_paths = build_dedicated_worker_runtime_paths(
        runtime_paths=runtime_paths,
        backend_name="Docker",
        worker_key=worker_key,
        config_path=Path("/app/config.yaml"),
        dedicated_root=Path("/app/worker"),
        worker_port=8766,
        shared_storage_root="/app/worker",
        extra_env={},
    )
    return write_startup_manifest(worker_root(runtime_paths, worker_key), worker_runtime_paths, public_runtime=True)


class DemoOAuthProvider:
    """A requester-scoped OAuth provider for the credential stores of the tests."""

    id = "demo_provider"
    credential_service = "demo_oauth"
    requester_scoped_credentials = True


def oauth_context(runtime_paths: RuntimePaths, scope: WorkerScope, agent: str) -> OAuthCredentialContext:
    """Return the OAuth credential context of ``agent`` in ``scope``."""
    return OAuthCredentialContext(
        provider=cast("OAuthProvider", DemoOAuthProvider()),
        runtime_paths=runtime_paths,
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        worker_target=resolve_worker_target(scope, agent, agent_worker_identity(agent)),
    )


async def publish_credentials(runtime_paths: RuntimePaths, scope: WorkerScope, agent: str) -> None:
    """Publish a token for ``agent`` in ``scope`` through MindRoom's credential transaction."""
    async with oauth_credential_transaction(oauth_context(runtime_paths, scope, agent)) as transaction:
        await transaction.publish({"token": f"{scope}-{agent}"}, advance_connection_generation=True)
        await transaction.commit()


async def read_credentials(runtime_paths: RuntimePaths, scope: WorkerScope, agent: str) -> object:
    """Read the credentials of ``agent`` in ``scope`` through MindRoom's credential reader."""
    async with oauth_credential_reader(oauth_context(runtime_paths, scope, agent)) as reader:
        snapshot = await reader.snapshot()
    return snapshot.credentials
