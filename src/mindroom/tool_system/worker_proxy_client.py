"""HTTP client helpers for sandbox runner and dedicated worker proxy calls."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

import httpx

from mindroom.credentials import load_scoped_credentials
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.workers.models import WorkerHandle, worker_api_endpoint

if TYPE_CHECKING:
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget
    from mindroom.workers.backend import WorkerBackend

_SANDBOX_PROXY_EXECUTE_PATH = "/api/sandbox-runner/execute"
_SANDBOX_PROXY_LEASE_PATH = "/api/sandbox-runner/leases"
SANDBOX_PROXY_SAVE_ATTACHMENT_PATH = "/api/sandbox-runner/save-attachment"
_SANDBOX_PROXY_TOKEN_HEADER = "x-mindroom-sandbox-token"  # noqa: S105


class _WorkerProxyResponse(Protocol):
    status_code: int
    text: str

    def raise_for_status(self) -> object: ...

    def json(self) -> object: ...


class _WorkerProxyClient(Protocol):
    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        headers: Mapping[str, str],
    ) -> _WorkerProxyResponse: ...


_WorkerProxyClientFactory = Callable[..., AbstractContextManager[_WorkerProxyClient]]


@dataclass(frozen=True)
class WorkerProxyClientConfig:
    """HTTP-facing proxy settings for one sandbox proxy request."""

    proxy_url: str | None
    proxy_token: str | None
    proxy_timeout_seconds: float
    credential_lease_ttl_seconds: int
    credential_policy: Mapping[str, tuple[str, ...]]


def to_json_compatible(value: object) -> object:
    """Convert arbitrary values into JSON-friendly structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_json_compatible(item) for item in value]
    return str(value)


def _json_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _proxy_http_error_message(response: _WorkerProxyResponse) -> str:
    try:
        payload = _json_object(response.json())
    except ValueError:
        payload = None
    if payload is not None:
        detail = payload.get("detail")
        if detail:
            return str(detail)
        error = payload.get("error")
        if error:
            return str(error)
    body = response.text.strip()
    if body:
        return body
    return f"Sandbox proxy request failed with status {response.status_code}."


def _request_headers_for_handle(
    worker_handle: WorkerHandle | None,
    *,
    config: WorkerProxyClientConfig,
) -> dict[str, str]:
    token = worker_handle.auth_token if worker_handle is not None else config.proxy_token
    if token is None:
        msg = f"{SANDBOX_RUNTIME_ENV_BY_KEY['proxy_token']} must be set when sandbox proxying is enabled."
        raise RuntimeError(msg)
    return {_SANDBOX_PROXY_TOKEN_HEADER: token}


def _record_proxy_exception_for_worker(
    exc: Exception,
    *,
    worker_handle: WorkerHandle | None,
    worker_manager: WorkerBackend,
) -> None:
    """Classify one proxy exception as either worker-health or request-level failure."""
    if worker_handle is None:
        return
    if _is_request_level_proxy_http_error(exc):
        worker_manager.touch_worker(worker_handle.worker_key)
        return
    worker_manager.record_failure(worker_handle.worker_key, str(exc))


def _is_request_level_proxy_http_error(exc: Exception) -> bool:
    """Return whether one execute-route HTTP failure came from a healthy worker."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    status_code = exc.response.status_code
    if status_code not in {400, 404, 422}:
        return False
    try:
        payload = _json_object(exc.response.json())
    except (ValueError, json.JSONDecodeError):
        return False
    detail = payload.get("detail") if payload is not None else None
    if isinstance(detail, str) and detail:
        return status_code in {400, 422} or detail != "Not Found"
    if status_code == 422 and isinstance(detail, list):
        return bool(detail)
    return False


def record_proxy_response_failure_for_worker(
    *,
    worker_handle: WorkerHandle | None,
    worker_manager: WorkerBackend,
    error: str,
    failure_kind: object,
) -> None:
    """Classify one structured runner failure response for worker health."""
    if worker_handle is None:
        return
    if failure_kind == "tool":
        worker_manager.touch_worker(worker_handle.worker_key)
        return
    worker_manager.record_failure(worker_handle.worker_key, error)


def post_worker_proxy_json(
    *,
    config: WorkerProxyClientConfig,
    payload: dict[str, object],
    worker_handle: WorkerHandle | None,
    worker_manager: WorkerBackend,
    proxy_path: str,
    worker_operation: Literal["execute", "save-attachment"],
    surface_proxy_http_detail: bool = False,
    client_factory: _WorkerProxyClientFactory = httpx.Client,
) -> object:
    """POST one non-lease worker proxy request and return its decoded JSON payload."""
    if worker_handle is None and config.proxy_url is None:
        msg = f"{SANDBOX_RUNTIME_ENV_BY_KEY['proxy_url']} must be set when sandbox proxying is enabled."
        raise RuntimeError(msg)

    try:
        headers = _request_headers_for_handle(worker_handle, config=config)
        request_url = (
            worker_api_endpoint(worker_handle, worker_operation)
            if worker_handle is not None
            else f"{config.proxy_url}{proxy_path}"
        )
        with client_factory(timeout=config.proxy_timeout_seconds) as client:
            response = client.post(request_url, json=payload, headers=headers)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if worker_handle is None and surface_proxy_http_detail:
                    raise RuntimeError(_proxy_http_error_message(exc.response)) from exc
                raise
            return response.json()
    except Exception as exc:
        _record_proxy_exception_for_worker(
            exc,
            worker_handle=worker_handle,
            worker_manager=worker_manager,
        )
        raise


def execute_worker_proxy_request(
    *,
    config: WorkerProxyClientConfig,
    payload: dict[str, object],
    credentials_manager: CredentialsManager | None,
    tool_name: str,
    function_name: str,
    worker_target: ResolvedWorkerTarget | None,
    worker_handle: WorkerHandle | None,
    worker_manager: WorkerBackend,
    client_factory: _WorkerProxyClientFactory = httpx.Client,
) -> object:
    """Execute one tool call through the sandbox proxy or selected dedicated worker."""
    if worker_handle is None and config.proxy_url is None:
        msg = f"{SANDBOX_RUNTIME_ENV_BY_KEY['proxy_url']} must be set when sandbox proxying is enabled."
        raise RuntimeError(msg)

    try:
        headers = _request_headers_for_handle(worker_handle, config=config)
        execute_url = (
            worker_api_endpoint(worker_handle, "execute")
            if worker_handle is not None
            else f"{config.proxy_url}{_SANDBOX_PROXY_EXECUTE_PATH}"
        )
        lease_url = (
            worker_api_endpoint(worker_handle, "leases")
            if worker_handle is not None
            else f"{config.proxy_url}{_SANDBOX_PROXY_LEASE_PATH}"
        )
        with client_factory(timeout=config.proxy_timeout_seconds) as client:
            lease_id = _create_credential_lease(
                client,
                config=config,
                lease_url=lease_url,
                headers=headers,
                credentials_manager=credentials_manager,
                tool_name=tool_name,
                function_name=function_name,
                worker_target=worker_target,
            )
            if lease_id is not None:
                payload["lease_id"] = lease_id

            response = client.post(execute_url, json=payload, headers=headers)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if worker_handle is None:
                    raise RuntimeError(_proxy_http_error_message(exc.response)) from exc
                raise
            data = _json_object(response.json())
    except Exception as exc:
        _record_proxy_exception_for_worker(
            exc,
            worker_handle=worker_handle,
            worker_manager=worker_manager,
        )
        raise

    if data is None:
        msg = "Sandbox proxy returned a non-object response."
        record_proxy_response_failure_for_worker(
            worker_handle=worker_handle,
            worker_manager=worker_manager,
            error=msg,
            failure_kind=None,
        )
        raise TypeError(msg)
    if data.get("ok") is True:
        if worker_handle is not None:
            worker_manager.touch_worker(worker_handle.worker_key)
        return data.get("result")

    error = data.get("error") or "Sandbox execution failed."
    record_proxy_response_failure_for_worker(
        worker_handle=worker_handle,
        worker_manager=worker_manager,
        error=str(error),
        failure_kind=data.get("failure_kind"),
    )
    raise RuntimeError(str(error))


def _create_credential_lease(
    client: _WorkerProxyClient,
    *,
    config: WorkerProxyClientConfig,
    lease_url: str,
    headers: Mapping[str, str],
    credentials_manager: CredentialsManager | None,
    tool_name: str,
    function_name: str,
    worker_target: ResolvedWorkerTarget | None,
) -> str | None:
    credential_overrides = _collect_credential_overrides(
        tool_name,
        function_name,
        config=config,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    if not credential_overrides:
        return None

    lease_payload = {
        "tool_name": tool_name,
        "function_name": function_name,
        "credential_overrides": to_json_compatible(credential_overrides),
        "ttl_seconds": config.credential_lease_ttl_seconds,
        "max_uses": 1,
    }
    response = client.post(lease_url, json=lease_payload, headers=headers)
    response.raise_for_status()
    lease_data = _json_object(response.json())
    lease_id = lease_data.get("lease_id") if lease_data is not None else None
    if not isinstance(lease_id, str):
        msg = "Sandbox proxy lease response is missing lease_id."
        raise TypeError(msg)
    return lease_id


def _collect_credential_overrides(
    tool_name: str,
    function_name: str,
    *,
    config: WorkerProxyClientConfig,
    credentials_manager: CredentialsManager | None,
    worker_target: ResolvedWorkerTarget | None,
) -> dict[str, object]:
    if credentials_manager is None:
        return {}
    services = _credential_services_for_call(tool_name, function_name, config=config)
    if not services:
        return {}
    allowed_shared_services: frozenset[str] | None = None
    if worker_target is not None and worker_target.worker_scope is not None:
        context = get_tool_runtime_context()
        allowed_shared_services = (
            context.config.get_worker_grantable_credentials() if context is not None else frozenset()
        )

    merged_overrides: dict[str, object] = {}
    for service in services:
        credentials = load_scoped_credentials(
            service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            allowed_shared_services=allowed_shared_services,
        )
        if isinstance(credentials, Mapping):
            merged_overrides.update(_filter_internal_credential_keys(credentials))
    return merged_overrides


def _credential_services_for_call(
    tool_name: str,
    function_name: str,
    *,
    config: WorkerProxyClientConfig,
) -> list[str]:
    selectors = ("*", tool_name, f"{tool_name}.{function_name}")
    services: list[str] = []
    for selector in selectors:
        for service in config.credential_policy.get(selector, ()):
            if service not in services:
                services.append(service)
    return services


def _filter_internal_credential_keys(credentials: Mapping[str, object]) -> dict[str, object]:
    return {str(key): value for key, value in credentials.items() if not str(key).startswith("_")}
