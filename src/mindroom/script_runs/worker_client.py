"""Authenticated primary-to-worker transport for background script processes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import quote

import httpx

from mindroom.workers.models import WorkerHandle, worker_api_endpoint

_TOKEN_HEADER = "x-mindroom-sandbox-token"  # noqa: S105
_HANDLE_RE = re.compile(r"shell:[0-9a-f]{32}")
_DEFAULT_TIMEOUT_SECONDS = 15.0

__all__ = [
    "ScriptWorkerClient",
    "ScriptWorkerError",
    "WorkerScriptCancel",
    "WorkerScriptLaunch",
    "WorkerScriptStatus",
]


class ScriptWorkerError(RuntimeError):
    """A rejected script request or unavailable worker transport."""

    def __init__(self, message: str, *, failure_kind: Literal["tool", "worker"]) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind


@dataclass(frozen=True, slots=True)
class WorkerScriptLaunch:
    """Validated worker launch receipt."""

    supervisor_handle: str


@dataclass(frozen=True, slots=True)
class WorkerScriptStatus:
    """Normalized state from one supervisor handle."""

    state: Literal["running", "exited", "unknown"]
    output: str = ""
    exit_code: int | None = None

    @classmethod
    def unknown_handle(cls) -> WorkerScriptStatus:
        """Return the lifecycle fact used when a supervisor lost its handle."""
        return cls(state="unknown")


@dataclass(frozen=True, slots=True)
class WorkerScriptCancel:
    """Normalized cancellation signal outcome."""

    cancel_requested: bool
    already_finished: bool
    unknown_handle: bool


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
        source_path: str,
        source_digest: str,
        token_path: str,
        gateway_url: str,
        supervisor_handle: str,
        private_agent_names: tuple[str, ...] | None = None,
        tail_lines: int = 200,
    ) -> WorkerScriptLaunch:
        """Launch one source snapshot and validate its supervisor receipt."""
        data = await self._request(
            worker,
            method="POST",
            url=worker_api_endpoint(worker, "script-run"),
            json={
                "run_id": run_id,
                "worker_key": worker.worker_key,
                "source_path": source_path,
                "source_digest": source_digest,
                "token_path": token_path,
                "supervisor_handle": supervisor_handle,
                "environment": {"MINDROOM_SCRIPT_GATEWAY_URL": gateway_url},
                "private_agent_names": list(private_agent_names) if private_agent_names is not None else None,
                "tail_lines": tail_lines,
            },
        )
        self._raise_structured_failure(data)
        returned_handle = data.get("supervisor_handle")
        if (
            not isinstance(returned_handle, str)
            or _HANDLE_RE.fullmatch(returned_handle) is None
            or returned_handle != supervisor_handle
        ):
            message = "Worker returned an invalid script launch receipt."
            raise ScriptWorkerError(message, failure_kind="worker")
        return WorkerScriptLaunch(supervisor_handle=returned_handle)

    async def status(
        self,
        worker: WorkerHandle,
        *,
        run_id: str,
        supervisor_handle: str,
    ) -> WorkerScriptStatus:
        """Poll one worker-owned supervisor handle."""
        data = await self._request(
            worker,
            method="GET",
            url=f"{worker_api_endpoint(worker, 'script-status')}/{quote(run_id, safe='')}",
            params={
                "worker_key": worker.worker_key,
                "supervisor_handle": supervisor_handle,
            },
        )
        self._raise_structured_failure(data)
        state = data.get("state")
        output = data.get("output", "")
        exit_code = data.get("exit_code")
        if not isinstance(state, str) or state not in {"running", "exited", "unknown"} or not isinstance(output, str):
            message = "Worker returned an invalid script status receipt."
            raise ScriptWorkerError(message, failure_kind="worker")
        if exit_code is not None and type(exit_code) is not int:
            message = "Worker returned an invalid script exit code."
            raise ScriptWorkerError(message, failure_kind="worker")
        normalized_state = cast("Literal['running', 'exited', 'unknown']", state)
        return WorkerScriptStatus(state=normalized_state, output=output, exit_code=exit_code)

    async def cancel(
        self,
        worker: WorkerHandle,
        *,
        run_id: str,
        supervisor_handle: str,
        force: bool = False,
    ) -> WorkerScriptCancel:
        """Request graceful or forced termination of one worker-owned handle."""
        data = await self._request(
            worker,
            method="POST",
            url=f"{worker_api_endpoint(worker, 'script-cancel')}/{quote(run_id, safe='')}/cancel",
            json={
                "worker_key": worker.worker_key,
                "supervisor_handle": supervisor_handle,
                "force": force,
            },
        )
        self._raise_structured_failure(data)
        cancel_requested = data.get("cancel_requested")
        already_finished = data.get("already_finished", False)
        unknown_handle = data.get("unknown_handle", False)
        if (
            not isinstance(cancel_requested, bool)
            or not isinstance(already_finished, bool)
            or not isinstance(unknown_handle, bool)
        ):
            message = "Worker returned an invalid script cancellation receipt."
            raise ScriptWorkerError(message, failure_kind="worker")
        return WorkerScriptCancel(
            cancel_requested=cancel_requested,
            already_finished=already_finished,
            unknown_handle=unknown_handle,
        )

    async def _request(
        self,
        worker: WorkerHandle,
        *,
        method: Literal["GET", "POST"],
        url: str,
        json: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, object]:
        token = worker.auth_token
        if token is None:
            message = "Worker authentication token is unavailable."
            raise ScriptWorkerError(message, failure_kind="worker")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.request(
                    method,
                    url,
                    headers={_TOKEN_HEADER: token},
                    json=json,
                    params=params,
                )
        except httpx.HTTPError as exc:
            message = f"Worker script request failed: {exc}"
            raise ScriptWorkerError(message, failure_kind="worker") from exc
        if response.status_code >= 400:
            detail = _response_error(response)
            request_failure = response.status_code in {400, 413, 422}
            raise ScriptWorkerError(detail, failure_kind="tool" if request_failure else "worker")
        try:
            decoded = response.json()
        except ValueError as exc:
            message = "Worker returned a non-JSON script response."
            raise ScriptWorkerError(message, failure_kind="worker") from exc
        if not isinstance(decoded, dict):
            message = "Worker returned a non-object script response."
            raise ScriptWorkerError(message, failure_kind="worker")
        return {str(key): value for key, value in decoded.items()}

    @staticmethod
    def _raise_structured_failure(data: dict[str, object]) -> None:
        if data.get("ok") is True:
            return
        failure_kind = data.get("failure_kind")
        kind: Literal["tool", "worker"] = "tool" if failure_kind == "tool" else "worker"
        error = data.get("error")
        raise ScriptWorkerError(str(error or "Worker script operation failed."), failure_kind=kind)


def _response_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        error = payload.get("error")
        if isinstance(error, str) and error:
            return error
    return response.text.strip() or f"Worker script request failed with status {response.status_code}."
