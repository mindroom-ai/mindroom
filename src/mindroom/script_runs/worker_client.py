"""Authenticated primary-to-worker transport for background script processes."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from mindroom.http_error_detail import error_detail_from_response
from mindroom.script_runs.wire import (
    SCRIPT_PROTOCOL_VERSION,
    SandboxScriptCancelResponse,
    SandboxScriptControlRequest,
    SandboxScriptRunRequest,
    SandboxScriptRunResponse,
    SandboxScriptStatusRequest,
    SandboxScriptStatusResponse,
    ScriptReceipt,
    ScriptWorkerError,
    WorkerScriptCancel,
    WorkerScriptStatus,
)
from mindroom.workers.models import WorkerHandle, worker_api_endpoint

_TOKEN_HEADER = "x-mindroom-sandbox-token"  # noqa: S105
_DEFAULT_TIMEOUT_SECONDS = 15.0

__all__ = ["ScriptWorkerClient"]


@dataclass(slots=True)
class ScriptWorkerClient:
    """Send script process-control requests to an already-selected worker."""

    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    transport: httpx.AsyncBaseTransport | None = None

    async def launch(
        self,
        worker: WorkerHandle,
        *,
        run_id: str,
        source_digest: str,
        gateway_url: str,
        max_runtime_seconds: int,
        state_scope_worker_key: str | None = None,
        private_agent_names: tuple[str, ...] | None = None,
        config_snapshot: dict[str, object] | None = None,
    ) -> None:
        """Launch the run's fixed source snapshot under its derived handle.

        ``config_snapshot`` holds the live config fields runners resolve; workers mount no config and resolve agents only from it.
        """
        request = _parsed(
            SandboxScriptRunRequest,
            {
                "protocol_version": SCRIPT_PROTOCOL_VERSION,
                "run_id": run_id,
                "worker_key": worker.worker_key,
                "state_scope_worker_key": state_scope_worker_key,
                "source_digest": source_digest,
                "gateway_url": gateway_url,
                "max_runtime_seconds": max_runtime_seconds,
                "private_agent_names": private_agent_names,
                "config_snapshot": config_snapshot,
            },
        )
        await self._post(worker, worker_api_endpoint(worker, "script-run"), request, SandboxScriptRunResponse)

    async def status(
        self,
        worker: WorkerHandle,
        *,
        run_id: str,
    ) -> WorkerScriptStatus:
        """Poll one worker-owned supervisor handle."""
        receipt = await self._post(
            worker,
            f"{worker_api_endpoint(worker, 'script-status')}/{quote(run_id, safe='')}/status",
            _parsed(SandboxScriptStatusRequest, {"worker_key": worker.worker_key}),
            SandboxScriptStatusResponse,
        )
        return WorkerScriptStatus(state=receipt.state, output=receipt.output, exit_code=receipt.exit_code)

    async def cancel(
        self,
        worker: WorkerHandle,
        *,
        run_id: str,
        force: bool = False,
    ) -> WorkerScriptCancel:
        """Request graceful or forced termination of one worker-owned handle."""
        receipt = await self._post(
            worker,
            f"{worker_api_endpoint(worker, 'script-cancel')}/{quote(run_id, safe='')}/cancel",
            _parsed(SandboxScriptControlRequest, {"worker_key": worker.worker_key, "force": force}),
            SandboxScriptCancelResponse,
        )
        return WorkerScriptCancel(
            cancel_requested=receipt.cancel_requested,
            already_finished=receipt.already_finished,
            unknown_handle=receipt.unknown_handle,
        )

    async def _post[ReceiptT: ScriptReceipt](
        self,
        worker: WorkerHandle,
        url: str,
        request: BaseModel,
        receipt_model: type[ReceiptT],
    ) -> ReceiptT:
        token = worker.auth_token
        if token is None:
            message = "Worker authentication token is unavailable."
            raise ScriptWorkerError(message)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                # The body carries the worker key, which names the requester, because access logs record URLs.
                response = await client.post(url, headers={_TOKEN_HEADER: token}, json=request.model_dump(mode="json"))
        except httpx.HTTPError as exc:
            message = f"Worker script request failed: {exc}"
            raise ScriptWorkerError(message) from exc
        if response.status_code >= 400:
            detail = error_detail_from_response(
                response,
                fallback=f"Worker script request failed with status {response.status_code}.",
            )
            raise ScriptWorkerError(detail)
        try:
            decoded = response.json()
        except ValueError as exc:
            message = "Worker returned a non-JSON script response."
            raise ScriptWorkerError(message) from exc
        receipt = _parsed(receipt_model, decoded)
        if not receipt.ok:
            raise ScriptWorkerError(receipt.error or "Worker script operation failed.")
        return receipt


def _parsed[ModelT: BaseModel](model: type[ModelT], data: object) -> ModelT:
    """Validate one script message, naming each rejected field without repeating its value."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        fields = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or model.__name__}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        )
        message = f"Invalid script worker message: {fields}"
        # The validation error itself holds the rejected values, which can include the config snapshot.
        raise ScriptWorkerError(message) from None
