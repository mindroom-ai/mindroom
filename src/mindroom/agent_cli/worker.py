"""Turn-owned isolated worker leases using existing Docker and shell supervision."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import httpx
from pydantic import SecretStr

from mindroom.agent_cli.worker_network import validate_cli_primary_auth
from mindroom.agent_cli.worker_protocol import CliShellRequest, CliShellSettings, CliWorkerLaunch, safe_origin
from mindroom.background_tasks import run_blocking_until_complete, wait_for_future_until_complete
from mindroom.logging_config import get_logger
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from mindroom.tool_system.worker_routing import resolve_worker_key, visible_state_roots_for_worker_key
from mindroom.workers.backends.docker import DockerWorkerBackend
from mindroom.workers.compatibility import WORKER_PROTOCOL_VERSION
from mindroom.workers.models import WorkerSpec, process_worker_key, worker_api_endpoint
from mindroom.workers.runtime import lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.agent_cli.session import CliGrant, TurnToolBridge
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workers.models import WorkerHandle


__all__ = ["CliWorkerLease", "open_cli_worker", "open_configured_cli_worker"]

logger = get_logger(__name__)


def _cli_worker_spec(context: ToolRuntimeContext) -> WorkerSpec:
    """Allocate a fresh physical process nonce independently from durable turn IDs."""
    identity = build_execution_identity_from_runtime_context(context)
    base_key = resolve_worker_key("user_agent", identity)
    if base_key is None:
        msg = "CLI workers require a requester-scoped agent identity"
        raise ValueError(msg)
    target = context.resolve_worker_target()
    return WorkerSpec(
        process_worker_key(base_key, purpose="agent-turn", process_id=uuid4()),
        private_agent_names=target.private_agent_names or frozenset(),
        mirrored_credential_services=frozenset(),
        state_scope_worker_key=target.worker_key or base_key,
    )


@dataclass
class CliWorkerLease:
    """An acquired physical worker; grant installation is single-use and owner-bound."""

    handle: WorkerHandle
    client: httpx.AsyncClient
    context: ToolRuntimeContext = field(repr=False)
    spec: WorkerSpec = field(repr=False)
    control_urls: tuple[str, ...] = ()
    container_storage_root: Path | None = None
    _handles: set[str] = field(default_factory=set, init=False, repr=False)
    _bridge: TurnToolBridge | None = field(default=None, init=False, repr=False)
    _installed: bool = field(default=False, init=False)
    _closed: bool = field(default=False, init=False)
    _close_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _tasks: set[asyncio.Task] = field(default_factory=set, init=False, repr=False)

    async def _post(
        self,
        operation: Literal["agent-cli-install", "agent-cli-shell"],
        payload: dict[str, object],
    ) -> dict[str, object]:
        response = await self.client.post(
            worker_api_endpoint(self.handle, operation),
            json=payload,
            headers={"x-mindroom-sandbox-token": self.handle.auth_token or ""},
        )
        if response.status_code == 404:
            msg = (
                "Docker worker image does not support minimal mode; use a worker image built for this MindRoom release"
            )
            raise RuntimeError(msg)
        if response.status_code != 200:
            # Server validation details can echo input. Never include a launch
            # response body in provider-visible exceptions or audit traces.
            msg = f"CLI worker {operation} failed (HTTP {response.status_code})"
            raise RuntimeError(msg)
        result = response.json()
        if not isinstance(result, dict):
            msg = "CLI worker returned an invalid response"
            raise TypeError(msg)
        return result

    async def install_grant(self, bridge: TurnToolBridge, grant: CliGrant, *, shell: CliShellSettings) -> None:
        """Bind authority after acquisition; never install a new grant into an old worker."""
        if self._closed or self._bridge is not None:
            msg = "CLI worker lease cannot accept another grant"
            raise RuntimeError(msg)
        owner = bridge.owner
        if (
            owner.worker_id != self.handle.worker_id
            or owner.execution_identity != build_execution_identity_from_runtime_context(self.context)
        ):
            msg = "CLI grant does not own this worker lease"
            raise ValueError(msg)
        runtime = self.context.runtime_paths
        if self.container_storage_root is not None:
            key = self.spec.state_scope_worker_key or ""
            private = self.spec.private_agent_names or frozenset()
            local_roots = visible_state_roots_for_worker_key(runtime.storage_root, key, private_agent_names=private)
            worker_roots = visible_state_roots_for_worker_key(
                self.container_storage_root,
                key,
                private_agent_names=private,
            )
            workspace = Path(shell.workspace).resolve()
            projected = [
                remote / workspace.relative_to(local.resolve())
                for local, remote in zip(local_roots, worker_roots, strict=True)
                if workspace.is_relative_to(local.resolve())
            ]
            if len(projected) != 1:
                msg = "CLI workspace has no unique canonical state mount"
                raise ValueError(msg)
            shell = shell.model_copy(update={"workspace": str(projected[0])})
        launch = CliWorkerLaunch(
            protocol_version=WORKER_PROTOCOL_VERSION,
            worker_key=self.handle.worker_key,
            state_scope_worker_key=self.spec.state_scope_worker_key or "",
            private_agent_names=sorted(self.spec.private_agent_names or ()),
            turn_id=owner.turn_id,
            generation=owner.generation,
            token=SecretStr(grant.raw_token),
            gateway_url=runtime.env_value("MINDROOM_AGENT_CLI_GATEWAY_URL") or "",
            primary_url=runtime.env_value("MINDROOM_AGENT_CLI_PRIMARY_URL") or "",
            control_urls=list(self.control_urls),
            shell=shell,
        )
        self._bridge = bridge
        try:
            response = await self.client.get(
                f"{launch.primary_url}/api/config/raw",
                timeout=5,
                headers={"Authorization": f"Bearer {runtime.env_value('MINDROOM_API_KEY')}"},
            )
            if response.status_code != 200:
                msg = "CLI primary protected-route positive check failed"
                raise RuntimeError(msg)  # noqa: TRY301 - grant revocation covers the entire install
            await self._post("agent-cli-install", launch.model_dump(mode="json") | {"token": grant.raw_token})
        except BaseException:
            bridge.revoke()
            raise
        self._installed = True

    async def invoke_shell(self, function_name: str, arguments: dict[str, object]) -> object:
        """Execute an already authorized canonical call on this exact worker only."""
        arguments = dict(arguments)
        if function_name == "run_shell_command":
            if "handle" in arguments:
                msg = "Canonical shell run cannot select its supervisor handle"
                raise ValueError(msg)
            handle = f"shell:{uuid4().hex}"
        else:
            handle = arguments.pop("handle", None)
            if not isinstance(handle, str) or handle not in self._handles:
                msg = "Shell handle does not belong to this worker turn"
                raise ValueError(msg)
        if self._closed or not self._installed:
            msg = "CLI worker lease is not active"
            raise RuntimeError(msg)
        request = CliShellRequest.model_validate(
            {
                "worker_key": self.handle.worker_key,
                "handle": handle,
                "operation": {"function_name": function_name, **arguments},
            },
        )
        self._handles.add(handle)
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        io_task = asyncio.create_task(self._post("agent-cli-shell", request.model_dump(mode="json")))

        def revoke_and_cancel() -> None:
            if self._bridge is not None:
                self._bridge.revoke()
            io_task.cancel()

        try:
            result = await wait_for_future_until_complete(io_task, on_cancel=revoke_and_cancel)
            return result["result"]
        finally:
            self._tasks.discard(task)

    async def close(self) -> None:
        """Revoke and drain in-flight IO before the owning context retires the worker."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await wait_for_future_until_complete(self._close_task)

    async def _close(self) -> None:
        self._closed = True
        if self._bridge is not None:
            self._bridge.revoke()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def open_cli_worker(
    backend: DockerWorkerBackend,
    context: ToolRuntimeContext,
) -> AsyncIterator[CliWorkerLease]:
    """Acquire fresh physical execution before constructing its checked turn owner.

    Keep this lease open across approvals/delegations. The response owner closes
    its windows and catalog lifetimes before exiting this context. Static runners
    and non-Docker backends are deliberately unsupported by this initial profile.
    """
    validate_cli_primary_auth(context.runtime_paths)
    for name in ("MINDROOM_AGENT_CLI_GATEWAY_URL", "MINDROOM_AGENT_CLI_PRIMARY_URL"):
        safe_origin(context.runtime_paths.env_value(name) or "")
    if context.runtime_paths.env_value("MINDROOM_AGENT_CLI_GATEWAY_URL") == context.runtime_paths.env_value(
        "MINDROOM_AGENT_CLI_PRIMARY_URL",
    ):
        msg = "CLI gateway must be a separate gateway-only proxy origin"
        raise ValueError(msg)
    spec = _cli_worker_spec(context)
    startup = asyncio.create_task(asyncio.to_thread(backend.ensure_worker, spec))
    try:
        handle = await asyncio.shield(startup)
        control_urls = await run_blocking_until_complete(backend.inspect_cli_worker, handle)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(1800, connect=5),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            lease = CliWorkerLease(handle, client, context, spec, control_urls, Path(backend.config.storage_mount_path))

            async def touch() -> None:
                while True:
                    await asyncio.sleep(max(0.1, min(30, backend.idle_timeout_seconds / 3)))
                    try:
                        await run_blocking_until_complete(backend.touch_worker, handle.worker_key)
                    except Exception:
                        # A missed touch is retried; idle cleanup only fires after a full timeout.
                        logger.exception("CLI worker heartbeat failed", worker_id=handle.worker_id)

            heartbeat = asyncio.create_task(touch())
            try:
                yield lease
            finally:
                heartbeat.cancel()

                async def close() -> None:
                    await lease.close()
                    await asyncio.gather(heartbeat, return_exceptions=True)

                await wait_for_future_until_complete(asyncio.create_task(close()))
    finally:

        async def retire() -> None:
            try:
                await wait_for_future_until_complete(startup)
            finally:
                await asyncio.to_thread(backend.retire_worker, spec.worker_key)

        await wait_for_future_until_complete(asyncio.create_task(retire()))


@asynccontextmanager
async def open_configured_cli_worker(context: ToolRuntimeContext) -> AsyncIterator[CliWorkerLease]:
    """Retain the existing configured manager lease for this isolated worker."""
    acquisition = asyncio.create_task(
        asyncio.to_thread(
            partial(lease_configured_primary_worker_manager, context.runtime_paths, runtime_config=context.config),
        ),
    )
    try:
        manager_lease = await wait_for_future_until_complete(acquisition)
        if manager_lease is None or not isinstance(manager_lease.manager, DockerWorkerBackend):
            msg = "Minimal CLI execution requires a configured dedicated Docker worker"
            raise RuntimeError(msg)
        async with open_cli_worker(manager_lease.manager, context) as worker:
            yield worker
    finally:
        # The drain helper propagates cancellation instead of returning the
        # acquired lease. Its completed task still owns that result.
        if acquisition.done() and not acquisition.cancelled() and acquisition.exception() is None:
            manager_lease = acquisition.result()
            if manager_lease is not None:
                manager_lease.release()
