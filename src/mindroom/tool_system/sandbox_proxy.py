"""Generic proxy wrapper for routing tool calls to a sandbox runner service."""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import os
import secrets
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from mindroom.constants import sandbox_execution_runtime_env_values, sandbox_shell_execution_runtime_env_values
from mindroom.credentials import load_scoped_credentials
from mindroom.tool_system.runtime_context import (
    WorkerProgressEvent,
    WorkerProgressPump,
    get_tool_runtime_context,
    get_worker_progress_pump,
)
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    WorkerScope,
    resolve_unscoped_worker_key,
    tool_stays_local,
)
from mindroom.workers.models import ProgressSink, WorkerHandle, WorkerReadyProgress, WorkerSpec, worker_api_endpoint
from mindroom.workers.runtime import (
    get_primary_worker_manager,
    primary_worker_backend_available,
    primary_worker_backend_name,
    serialized_kubernetes_worker_validation_snapshot,
)

if TYPE_CHECKING:
    from agno.tools.function import Function
    from agno.tools.toolkit import Toolkit

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.workers.manager import WorkerManager

_SANDBOX_PROXY_EXECUTE_PATH = "/api/sandbox-runner/execute"
_SANDBOX_PROXY_LEASE_PATH = "/api/sandbox-runner/leases"
_SANDBOX_PROXY_TOKEN_HEADER = "x-mindroom-sandbox-token"  # noqa: S105
_DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS = 120.0
_DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS = 60
_MAX_CREDENTIAL_LEASE_TTL_SECONDS = 3600
_EXECUTION_ENV_TOOL_NAMES = frozenset({"python", "shell"})
_SANDBOX_PROXY_SAVE_ATTACHMENT_PATH = "/api/sandbox-runner/save-attachment"
_INLINE_ATTACHMENT_BYTES_ENV = "MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES"
DEFAULT_INLINE_ATTACHMENT_BYTES = 16 * 1024 * 1024
_ATTACHMENT_SAVE_WORKSPACE_CONSUMER_TOOLS = frozenset({"file", "coding", "python", "shell"})


@dataclass(frozen=True)
class WorkerAttachmentSaveReceipt:
    """Receipt returned after writing attachment bytes into a worker workspace."""

    worker_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class _SandboxProxyConfig:
    """Resolved sandbox proxy settings for one explicit runtime context."""

    runner_mode: bool
    proxy_url: str | None
    proxy_token: str | None
    proxy_timeout_seconds: float
    execution_mode: str | None
    credential_lease_ttl_seconds: int
    proxy_tools: set[str] | None
    credential_policy: dict[str, tuple[str, ...]]


def _read_proxy_url(runtime_paths: RuntimePaths) -> str | None:
    value = (runtime_paths.env_value("MINDROOM_SANDBOX_PROXY_URL", default="") or "").strip()
    if not value:
        return None
    return value.rstrip("/")


def _read_proxy_token(runtime_paths: RuntimePaths) -> str | None:
    value = (runtime_paths.env_value("MINDROOM_SANDBOX_PROXY_TOKEN", default="") or "").strip()
    if not value:
        return None
    return value


def _read_proxy_timeout(runtime_paths: RuntimePaths) -> float:
    raw = runtime_paths.env_value(
        "MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS",
        default=str(_DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS),
    )
    try:
        return float(raw or _DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS)
    except ValueError:
        return _DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS


def inline_attachment_byte_limit(runtime_paths: RuntimePaths) -> int:
    """Return the hard cap for inline primary-to-worker attachment saves."""
    raw_value = (
        runtime_paths.env_value(_INLINE_ATTACHMENT_BYTES_ENV)
        or os.environ.get(_INLINE_ATTACHMENT_BYTES_ENV)
        or str(DEFAULT_INLINE_ATTACHMENT_BYTES)
    )
    try:
        limit = int(raw_value)
    except ValueError:
        return DEFAULT_INLINE_ATTACHMENT_BYTES
    if limit <= 0:
        return DEFAULT_INLINE_ATTACHMENT_BYTES
    return limit


def _read_execution_mode(runtime_paths: RuntimePaths) -> str | None:
    raw = runtime_paths.env_value("MINDROOM_SANDBOX_EXECUTION_MODE")
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if not normalized:
        return None
    return normalized


def _read_credential_lease_ttl(runtime_paths: RuntimePaths) -> int:
    raw = runtime_paths.env_value(
        "MINDROOM_SANDBOX_CREDENTIAL_LEASE_TTL_SECONDS",
        default=str(_DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS),
    )
    try:
        ttl_seconds = int(raw or _DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS)
    except ValueError:
        ttl_seconds = _DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS
    return max(1, min(_MAX_CREDENTIAL_LEASE_TTL_SECONDS, ttl_seconds))


def _read_proxy_tools(runtime_paths: RuntimePaths, execution_mode: str | None) -> set[str] | None:
    default = "" if execution_mode in {"selective", "sandbox_selective"} else "*"
    raw_value = (runtime_paths.env_value("MINDROOM_SANDBOX_PROXY_TOOLS", default=default) or default).strip()
    if raw_value == "*":
        return None
    if not raw_value:
        return set()
    return {part.strip() for part in raw_value.split(",") if part.strip()}


def _read_credential_policy(runtime_paths: RuntimePaths) -> dict[str, tuple[str, ...]]:
    raw_policy = (runtime_paths.env_value("MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON", default="") or "").strip()
    if not raw_policy:
        return {}

    try:
        parsed = json.loads(raw_policy)
    except json.JSONDecodeError:
        return {}

    if not isinstance(parsed, dict):
        return {}

    policy: dict[str, tuple[str, ...]] = {}
    for selector, services in parsed.items():
        if not isinstance(selector, str):
            continue
        if not isinstance(services, list):
            continue
        cleaned_services = tuple(
            service.strip() for service in services if isinstance(service, str) and service.strip()
        )
        policy[selector.strip()] = cleaned_services
    return policy


def sandbox_proxy_config(runtime_paths: RuntimePaths) -> _SandboxProxyConfig:
    """Return sandbox proxy settings for one explicit runtime context."""
    execution_mode = _read_execution_mode(runtime_paths)
    return _SandboxProxyConfig(
        runner_mode=runtime_paths.env_flag("MINDROOM_SANDBOX_RUNNER_MODE"),
        proxy_url=_read_proxy_url(runtime_paths),
        proxy_token=_read_proxy_token(runtime_paths),
        proxy_timeout_seconds=_read_proxy_timeout(runtime_paths),
        execution_mode=execution_mode,
        credential_lease_ttl_seconds=_read_credential_lease_ttl(runtime_paths),
        proxy_tools=_read_proxy_tools(runtime_paths, execution_mode),
        credential_policy=_read_credential_policy(runtime_paths),
    )


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


def _credential_services_for_call(
    tool_name: str,
    function_name: str,
    *,
    proxy_config: _SandboxProxyConfig,
) -> list[str]:
    policy = proxy_config.credential_policy
    selectors = ("*", tool_name, f"{tool_name}.{function_name}")
    services: list[str] = []
    for selector in selectors:
        for service in policy.get(selector, ()):
            if service not in services:
                services.append(service)
    return services


def _filter_internal_credential_keys(credentials: Mapping[str, object]) -> dict[str, object]:
    return {str(key): value for key, value in credentials.items() if not str(key).startswith("_")}


def _collect_credential_overrides(
    tool_name: str,
    function_name: str,
    *,
    proxy_config: _SandboxProxyConfig,
    credentials_manager: CredentialsManager | None,
    worker_target: ResolvedWorkerTarget | None,
) -> dict[str, object]:
    if credentials_manager is None:
        return {}
    services = _credential_services_for_call(tool_name, function_name, proxy_config=proxy_config)
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


def _create_credential_lease(
    client: httpx.Client,
    *,
    proxy_config: _SandboxProxyConfig,
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
        proxy_config=proxy_config,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    if not credential_overrides:
        return None

    lease_payload = {
        "tool_name": tool_name,
        "function_name": function_name,
        "credential_overrides": to_json_compatible(credential_overrides),
        "ttl_seconds": proxy_config.credential_lease_ttl_seconds,
        "max_uses": 1,
    }
    response = client.post(lease_url, json=lease_payload, headers=headers)
    response.raise_for_status()
    lease_data = response.json()
    if not isinstance(lease_data, Mapping) or not isinstance(lease_data.get("lease_id"), str):
        msg = "Sandbox proxy lease response is missing lease_id."
        raise TypeError(msg)
    return lease_data["lease_id"]


def _build_worker_routing_payload(
    *,
    runtime_paths: RuntimePaths,
    tool_name: str,
    function_name: str,
    worker_target: ResolvedWorkerTarget | None,
    progress_sink: ProgressSink | None,
) -> tuple[dict[str, object], WorkerHandle | None]:
    proxy_config = sandbox_proxy_config(runtime_paths)
    worker_scope = worker_target.worker_scope if worker_target is not None else None
    execution_identity = worker_target.execution_identity if worker_target is not None else None
    routing_agent_name = worker_target.routing_agent_name if worker_target is not None else None
    if worker_scope is None:
        if primary_worker_backend_name(runtime_paths) != "kubernetes":
            payload: dict[str, object] = {}
            if routing_agent_name is not None:
                payload["routing_agent_name"] = routing_agent_name
            if execution_identity is not None:
                payload["execution_identity"] = to_json_compatible(asdict(execution_identity))
            return payload, None

        effective_agent_name = routing_agent_name
        if effective_agent_name is None:
            msg = (
                f"Unscoped worker-routed tool '{tool_name}.{function_name}' requires an agent name "
                "when using the Kubernetes worker backend."
            )
            raise RuntimeError(msg)

        worker_key = resolve_unscoped_worker_key(
            agent_name=effective_agent_name,
            execution_identity=execution_identity,
            tenant_id=worker_target.tenant_id if worker_target is not None else None,
            account_id=worker_target.account_id if worker_target is not None else None,
        )
        worker_handle = _get_worker_manager(runtime_paths, proxy_config).ensure_worker(
            WorkerSpec(worker_key),
            progress_sink=progress_sink,
        )
        return (
            {
                "routing_agent_name": effective_agent_name,
                "worker_key": worker_key,
            },
            worker_handle,
        )

    if worker_target is None or execution_identity is None:
        msg = f"Worker-routed tool '{tool_name}.{function_name}' requires execution identity context."
        raise RuntimeError(msg)

    effective_agent_name = routing_agent_name
    if worker_scope == "user_agent":
        worker_key, resolved_private_agent_names = _resolve_user_agent_worker_payload(
            tool_name=tool_name,
            function_name=function_name,
            worker_target=worker_target,
        )
    else:
        resolved_private_agent_names = None
        worker_key = worker_target.worker_key
        if worker_key is None:
            msg = (
                f"Worker scope '{worker_scope}' for tool '{tool_name}.{function_name}' "
                "could not be resolved from the current execution identity."
            )
            raise RuntimeError(msg)
    worker_handle = _get_worker_manager(runtime_paths, proxy_config).ensure_worker(
        WorkerSpec(worker_key, private_agent_names=resolved_private_agent_names),
        progress_sink=progress_sink,
    )
    return (
        {
            "worker_scope": worker_scope,
            "routing_agent_name": effective_agent_name,
            "worker_key": worker_key,
            "execution_identity": to_json_compatible(asdict(execution_identity)),
            "private_agent_names": (
                sorted(resolved_private_agent_names) if resolved_private_agent_names is not None else None
            ),
        },
        worker_handle,
    )


def _resolve_user_agent_worker_payload(
    *,
    tool_name: str,
    function_name: str,
    worker_target: ResolvedWorkerTarget,
) -> tuple[str, frozenset[str]]:
    if worker_target.private_agent_names is None:
        msg = (
            f"Worker-routed tool '{tool_name}.{function_name}' with scope 'user_agent' "
            "requires explicit private visibility."
        )
        raise RuntimeError(msg)
    worker_key = worker_target.worker_key
    if worker_key is None:
        msg = (
            f"Worker scope 'user_agent' for tool '{tool_name}.{function_name}' "
            "could not be resolved from the current execution identity."
        )
        raise RuntimeError(msg)
    return worker_key, worker_target.private_agent_names


def _get_worker_manager(
    runtime_paths: RuntimePaths,
    proxy_config: _SandboxProxyConfig,
) -> WorkerManager:
    context = get_tool_runtime_context()
    storage_root = (
        context.storage_path if context is not None and context.storage_path is not None else runtime_paths.storage_root
    )
    kubernetes_tool_validation_snapshot: dict[str, dict[str, object]] | None = None
    if context is not None and primary_worker_backend_name(runtime_paths) == "kubernetes":
        kubernetes_tool_validation_snapshot = serialized_kubernetes_worker_validation_snapshot(
            runtime_paths,
            runtime_config=context.config,
        )
    return get_primary_worker_manager(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
        storage_root=storage_root,
        kubernetes_tool_validation_snapshot=kubernetes_tool_validation_snapshot,
        worker_grantable_credentials=(
            context.config.get_worker_grantable_credentials() if context is not None else None
        ),
    )


def _execution_env_payload(
    tool_name: str,
    *,
    runtime_paths: RuntimePaths,
    extra_env_passthrough: str | None = None,
) -> dict[str, str] | None:
    """Return explicit execution env only for tools that intentionally support it."""
    if tool_name not in _EXECUTION_ENV_TOOL_NAMES:
        return None
    if tool_name == "shell":
        return dict(
            sandbox_shell_execution_runtime_env_values(
                runtime_paths,
                extra_env_passthrough=extra_env_passthrough,
                process_env=runtime_paths.process_env,
            ),
        )
    return dict(sandbox_execution_runtime_env_values(runtime_paths))


def _request_headers_for_handle(
    worker_handle: WorkerHandle | None,
    *,
    proxy_config: _SandboxProxyConfig,
) -> dict[str, str]:
    token = worker_handle.auth_token if worker_handle is not None else proxy_config.proxy_token
    if token is None:
        msg = "MINDROOM_SANDBOX_PROXY_TOKEN must be set when sandbox proxying is enabled."
        raise RuntimeError(msg)
    return {_SANDBOX_PROXY_TOKEN_HEADER: token}


def _record_proxy_exception_for_worker(
    exc: Exception,
    *,
    worker_handle: WorkerHandle | None,
    runtime_paths: RuntimePaths,
    proxy_config: _SandboxProxyConfig,
) -> None:
    """Classify one proxy exception as either worker-health or request-level failure."""
    if worker_handle is None:
        return
    manager = _get_worker_manager(runtime_paths, proxy_config)
    if _is_request_level_proxy_http_error(exc):
        manager.touch_worker(worker_handle.worker_key)
        return
    manager.record_failure(worker_handle.worker_key, str(exc))


def _is_request_level_proxy_http_error(exc: Exception) -> bool:
    """Return whether one execute-route HTTP failure came from a healthy worker."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    status_code = exc.response.status_code
    if status_code not in {400, 404, 422}:
        return False
    try:
        payload = exc.response.json()
    except (ValueError, json.JSONDecodeError):
        return False
    detail = payload.get("detail") if isinstance(payload, Mapping) else None
    if isinstance(detail, str) and detail:
        return status_code in {400, 422} or detail != "Not Found"
    if status_code == 422 and isinstance(detail, list):
        return bool(detail)
    return False


def _record_proxy_response_failure_for_worker(
    *,
    worker_handle: WorkerHandle | None,
    runtime_paths: RuntimePaths,
    proxy_config: _SandboxProxyConfig,
    error: str,
    failure_kind: object,
) -> None:
    """Classify one structured runner failure response for worker health."""
    if worker_handle is None:
        return
    manager = _get_worker_manager(runtime_paths, proxy_config)
    if failure_kind == "tool":
        manager.touch_worker(worker_handle.worker_key)
        return
    manager.record_failure(worker_handle.worker_key, error)


def attachment_save_uses_worker(
    *,
    runtime_paths: RuntimePaths,
    worker_target: ResolvedWorkerTarget | None,
    worker_tools_override: list[str] | None = None,
) -> bool:
    """Return whether attachment saves should land where workspace consumers run."""
    worker_scope = worker_target.worker_scope if worker_target is not None else None
    return any(
        _sandbox_proxy_enabled_for_tool(
            tool_name,
            runtime_paths=runtime_paths,
            worker_tools_override=worker_tools_override,
            worker_scope=worker_scope,
        )
        for tool_name in _ATTACHMENT_SAVE_WORKSPACE_CONSUMER_TOOLS
    )


def _record_worker_save_failure(
    *,
    worker_handle: WorkerHandle | None,
    runtime_paths: RuntimePaths,
    proxy_config: _SandboxProxyConfig,
    error: str,
) -> None:
    """Record a worker save protocol/integrity failure against worker health."""
    if worker_handle is not None:
        _get_worker_manager(runtime_paths, proxy_config).record_failure(worker_handle.worker_key, error)


def _validated_worker_save_receipt(
    data: Mapping[str, object],
    *,
    requested_path: str,
    byte_count: int,
    sha256: str,
    worker_handle: WorkerHandle | None,
    runtime_paths: RuntimePaths,
    proxy_config: _SandboxProxyConfig,
) -> WorkerAttachmentSaveReceipt:
    """Validate one successful worker save response against the sent bytes."""
    worker_path = data.get("worker_path")
    response_size = data.get("size_bytes")
    response_sha256 = data.get("sha256")
    if (
        not isinstance(worker_path, str)
        or type(response_size) is not int
        or response_size < 0
        or not isinstance(response_sha256, str)
    ):
        msg = "Sandbox save-attachment response is missing its receipt fields."
        _record_worker_save_failure(
            worker_handle=worker_handle,
            runtime_paths=runtime_paths,
            proxy_config=proxy_config,
            error=msg,
        )
        raise RuntimeError(msg)
    if worker_path != requested_path:
        msg = "Sandbox save-attachment response path does not match the requested workspace path."
        _record_worker_save_failure(
            worker_handle=worker_handle,
            runtime_paths=runtime_paths,
            proxy_config=proxy_config,
            error=msg,
        )
        raise RuntimeError(msg)
    if response_size != byte_count:
        msg = "Sandbox save-attachment response size does not match the sent bytes."
        _record_worker_save_failure(
            worker_handle=worker_handle,
            runtime_paths=runtime_paths,
            proxy_config=proxy_config,
            error=msg,
        )
        raise RuntimeError(msg)
    if not secrets.compare_digest(response_sha256, sha256):
        msg = "Sandbox save-attachment response SHA256 does not match the sent bytes."
        _record_worker_save_failure(
            worker_handle=worker_handle,
            runtime_paths=runtime_paths,
            proxy_config=proxy_config,
            error=msg,
        )
        raise RuntimeError(msg)
    return WorkerAttachmentSaveReceipt(
        worker_path=worker_path,
        size_bytes=response_size,
        sha256=response_sha256,
    )


def save_attachment_to_worker(
    *,
    runtime_paths: RuntimePaths,
    worker_target: ResolvedWorkerTarget | None,
    worker_tools_override: list[str] | None = None,
    attachment_id: str,
    mindroom_output_path: str,
    payload_bytes: bytes,
    mime_type: str | None,
    filename: str | None,
) -> WorkerAttachmentSaveReceipt | None:
    """Write attachment bytes to the selected worker, returning None when no worker endpoint exists."""
    if not attachment_save_uses_worker(
        runtime_paths=runtime_paths,
        worker_target=worker_target,
        worker_tools_override=worker_tools_override,
    ):
        return None

    byte_limit = inline_attachment_byte_limit(runtime_paths)
    byte_count = len(payload_bytes)
    if byte_count > byte_limit:
        msg = (
            f"Attachment {attachment_id} exceeds inline save-to-disk size limit "
            f"({byte_count} bytes > {byte_limit} bytes)."
        )
        raise RuntimeError(msg)

    proxy_config = sandbox_proxy_config(runtime_paths)
    worker_payload, worker_handle = _build_worker_routing_payload(
        runtime_paths=runtime_paths,
        tool_name="attachments",
        function_name="get_attachment",
        worker_target=worker_target,
        progress_sink=None,
    )
    if worker_handle is None and proxy_config.proxy_url is None:
        return None

    sha256 = hashlib.sha256(payload_bytes).hexdigest()
    request_payload: dict[str, object] = {
        **worker_payload,
        "attachment_id": attachment_id,
        "mindroom_output_path": mindroom_output_path,
        "sha256": sha256,
        "size_bytes": byte_count,
        "mime_type": mime_type,
        "filename": filename,
        "bytes_b64": base64.b64encode(payload_bytes).decode("ascii"),
    }

    try:
        headers = _request_headers_for_handle(worker_handle, proxy_config=proxy_config)
        save_url = (
            worker_api_endpoint(worker_handle, "save-attachment")
            if worker_handle is not None
            else (f"{proxy_config.proxy_url}{_SANDBOX_PROXY_SAVE_ATTACHMENT_PATH}")
        )
        with httpx.Client(timeout=proxy_config.proxy_timeout_seconds) as client:
            response = client.post(save_url, json=request_payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        _record_proxy_exception_for_worker(
            exc,
            worker_handle=worker_handle,
            runtime_paths=runtime_paths,
            proxy_config=proxy_config,
        )
        raise

    if not isinstance(data, Mapping):
        msg = "Sandbox save-attachment returned a non-object response."
        _record_worker_save_failure(
            worker_handle=worker_handle,
            runtime_paths=runtime_paths,
            proxy_config=proxy_config,
            error=msg,
        )
        raise TypeError(msg)
    if data.get("ok") is True:
        receipt = _validated_worker_save_receipt(
            data,
            requested_path=mindroom_output_path,
            byte_count=byte_count,
            sha256=sha256,
            worker_handle=worker_handle,
            runtime_paths=runtime_paths,
            proxy_config=proxy_config,
        )
        if worker_handle is not None:
            _get_worker_manager(runtime_paths, proxy_config).touch_worker(worker_handle.worker_key)
        return receipt

    error = data.get("error") or "Sandbox attachment save failed."
    _record_proxy_response_failure_for_worker(
        worker_handle=worker_handle,
        runtime_paths=runtime_paths,
        proxy_config=proxy_config,
        error=str(error),
        failure_kind=data.get("failure_kind"),
    )
    raise RuntimeError(str(error))


def _make_progress_sink(
    pump: WorkerProgressPump,
    *,
    tool_name: str,
    function_name: str,
) -> ProgressSink:
    def sink(progress: WorkerReadyProgress) -> None:
        if pump.shutdown.is_set() or pump.loop.is_closed():
            return
        event = WorkerProgressEvent(
            tool_name=tool_name,
            function_name=function_name,
            progress=progress,
        )
        with suppress(RuntimeError):
            pump.loop.call_soon_threadsafe(pump.queue.put_nowait, event)

    return sink


def _portable_tool_init_overrides(
    tool_init_overrides: dict[str, object] | None,
    *,
    shared_storage_root_path: Path | None,
    worker_key: str | None,
) -> dict[str, object] | None:
    """Rewrite storage-root absolute base_dir values only for worker-keyed requests."""
    if not tool_init_overrides:
        return tool_init_overrides
    if worker_key is None:
        return tool_init_overrides

    portable_overrides = dict(tool_init_overrides)
    raw_base_dir = portable_overrides.get("base_dir")
    if not isinstance(raw_base_dir, str):
        return portable_overrides

    base_dir = Path(raw_base_dir).expanduser()
    if not base_dir.is_absolute():
        return portable_overrides

    if shared_storage_root_path is None:
        return portable_overrides

    shared_root = shared_storage_root_path.resolve()
    with suppress(ValueError):
        portable_overrides["base_dir"] = base_dir.resolve().relative_to(shared_root).as_posix()
    return portable_overrides


def _sandbox_proxy_enabled_for_tool(
    tool_name: str,
    *,
    runtime_paths: RuntimePaths,
    worker_tools_override: list[str] | None = None,
    worker_scope: WorkerScope | None = None,
) -> bool:
    """Return whether the given tool should execute through the sandbox proxy.

    When *worker_tools_override* is not ``None``, it takes precedence over the
    env-var based ``_EXECUTION_MODE`` / ``_PROXY_TOOLS`` logic. An empty list
    means "route nothing through the proxy for this agent".
    """
    proxy_config = sandbox_proxy_config(runtime_paths)
    if proxy_config.runner_mode or tool_stays_local(tool_name):
        return False

    if worker_tools_override is not None:
        requested = tool_name in worker_tools_override
    elif proxy_config.execution_mode in {"off", "local", "disabled"}:
        requested = False
    else:
        requested = proxy_config.execution_mode in {"all", "sandbox_all"}
        if not requested:
            requested = proxy_config.proxy_tools is None or tool_name in proxy_config.proxy_tools

    if not requested:
        return False

    backend_name = primary_worker_backend_name(runtime_paths)
    if backend_name == "static_runner" and proxy_config.proxy_url is None and worker_scope is None:
        return False

    if primary_worker_backend_available(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
    ):
        return True

    # Dedicated-worker backends must fail closed when routing is intended but the
    # provider config is incomplete; otherwise tools silently execute locally.
    return backend_name == "kubernetes"


def _call_proxy_sync(  # noqa: C901
    *,
    runtime_paths: RuntimePaths,
    tool_name: str,
    function_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    credentials_manager: CredentialsManager | None,
    shared_storage_root_path: Path | None = None,
    tool_config_overrides: dict[str, object] | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    execution_env: dict[str, str] | None = None,
    extra_env_passthrough: str | None = None,
    worker_target: ResolvedWorkerTarget | None = None,
) -> object:
    proxy_config = sandbox_proxy_config(runtime_paths)
    pump = get_worker_progress_pump()
    progress_sink = (
        _make_progress_sink(
            pump,
            tool_name=tool_name,
            function_name=function_name,
        )
        if pump is not None
        else None
    )
    payload: dict[str, object] = {
        "tool_name": tool_name,
        "function_name": function_name,
        "args": [to_json_compatible(arg) for arg in args],
        "kwargs": {key: to_json_compatible(value) for key, value in kwargs.items()},
    }
    worker_payload, worker_handle = _build_worker_routing_payload(
        runtime_paths=runtime_paths,
        tool_name=tool_name,
        function_name=function_name,
        worker_target=worker_target,
        progress_sink=progress_sink,
    )
    payload.update(worker_payload)
    if execution_env:
        payload["execution_env"] = execution_env
    if extra_env_passthrough is not None:
        payload["extra_env_passthrough"] = extra_env_passthrough
    if tool_config_overrides:
        payload["tool_config_overrides"] = to_json_compatible(tool_config_overrides)
    if worker_handle is None and proxy_config.proxy_url is None:
        msg = "MINDROOM_SANDBOX_PROXY_URL must be set when sandbox proxying is enabled."
        raise RuntimeError(msg)

    try:
        headers = _request_headers_for_handle(worker_handle, proxy_config=proxy_config)
        execute_url = (
            worker_api_endpoint(worker_handle, "execute")
            if worker_handle is not None
            else (f"{proxy_config.proxy_url}{_SANDBOX_PROXY_EXECUTE_PATH}")
        )
        worker_key = worker_payload.get("worker_key")
        portable_tool_init_overrides = _portable_tool_init_overrides(
            tool_init_overrides,
            shared_storage_root_path=shared_storage_root_path,
            worker_key=worker_key if isinstance(worker_key, str) else None,
        )
        if portable_tool_init_overrides:
            payload["tool_init_overrides"] = to_json_compatible(portable_tool_init_overrides)
        lease_url = (
            worker_api_endpoint(worker_handle, "leases")
            if worker_handle is not None
            else (f"{proxy_config.proxy_url}{_SANDBOX_PROXY_LEASE_PATH}")
        )

        with httpx.Client(timeout=proxy_config.proxy_timeout_seconds) as client:
            lease_id = _create_credential_lease(
                client,
                proxy_config=proxy_config,
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
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        _record_proxy_exception_for_worker(
            exc,
            worker_handle=worker_handle,
            runtime_paths=runtime_paths,
            proxy_config=proxy_config,
        )
        raise

    if not isinstance(data, Mapping):
        msg = "Sandbox proxy returned a non-object response."
        raise TypeError(msg)
    if data.get("ok") is True:
        if worker_handle is not None:
            _get_worker_manager(runtime_paths, proxy_config).touch_worker(worker_handle.worker_key)
        return data.get("result")
    error = data.get("error") or "Sandbox execution failed."
    _record_proxy_response_failure_for_worker(
        worker_handle=worker_handle,
        runtime_paths=runtime_paths,
        proxy_config=proxy_config,
        error=str(error),
        failure_kind=data.get("failure_kind"),
    )
    raise RuntimeError(str(error))


def _wrap_sync_function(
    function: Function,
    tool_name: str,
    function_name: str,
    *,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager | None,
    shared_storage_root_path: Path | None = None,
    tool_config_overrides: dict[str, object] | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    execution_env: dict[str, str] | None = None,
    extra_env_passthrough: str | None = None,
    worker_target: ResolvedWorkerTarget | None = None,
) -> Function:
    wrapped = function.model_copy(deep=False)
    assert function.entrypoint is not None

    @functools.wraps(function.entrypoint)
    def proxy_entrypoint(*args: object, **kwargs: object) -> object:
        return _call_proxy_sync(
            runtime_paths=runtime_paths,
            tool_name=tool_name,
            function_name=function_name,
            args=args,
            kwargs=dict(kwargs),
            credentials_manager=credentials_manager,
            shared_storage_root_path=shared_storage_root_path,
            tool_config_overrides=tool_config_overrides,
            tool_init_overrides=tool_init_overrides,
            execution_env=execution_env,
            extra_env_passthrough=extra_env_passthrough,
            worker_target=worker_target,
        )

    wrapped.entrypoint = proxy_entrypoint
    return wrapped


def _wrap_async_function(
    function: Function,
    tool_name: str,
    function_name: str,
    *,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager | None,
    shared_storage_root_path: Path | None = None,
    tool_config_overrides: dict[str, object] | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    execution_env: dict[str, str] | None = None,
    extra_env_passthrough: str | None = None,
    worker_target: ResolvedWorkerTarget | None = None,
) -> Function:
    wrapped = function.model_copy(deep=False)
    assert function.entrypoint is not None

    @functools.wraps(function.entrypoint)
    async def proxy_entrypoint(*args: object, **kwargs: object) -> object:
        return await asyncio.to_thread(
            _call_proxy_sync,
            runtime_paths=runtime_paths,
            tool_name=tool_name,
            function_name=function_name,
            args=args,
            kwargs=dict(kwargs),
            credentials_manager=credentials_manager,
            shared_storage_root_path=shared_storage_root_path,
            tool_config_overrides=tool_config_overrides,
            tool_init_overrides=tool_init_overrides,
            execution_env=execution_env,
            extra_env_passthrough=extra_env_passthrough,
            worker_target=worker_target,
        )

    wrapped.entrypoint = proxy_entrypoint
    return wrapped


def maybe_wrap_toolkit_for_sandbox_proxy(
    tool_name: str,
    toolkit: Toolkit,
    *,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager | None,
    shared_storage_root_path: Path | None = None,
    tool_config_overrides: dict[str, object] | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    extra_env_passthrough: str | None = None,
    worker_tools_override: list[str] | None = None,
    worker_target: ResolvedWorkerTarget | None = None,
) -> Toolkit:
    """Wrap toolkit functions so calls execute through the sandbox runner API.

    Note: mutates ``toolkit.functions`` and ``toolkit.async_functions`` in place.
    Callers must pass a freshly-created toolkit (``get_tool_by_name`` does this).
    """
    if not _sandbox_proxy_enabled_for_tool(
        tool_name,
        runtime_paths=runtime_paths,
        worker_tools_override=worker_tools_override,
        worker_scope=worker_target.worker_scope if worker_target is not None else None,
    ):
        return toolkit

    execution_env = _execution_env_payload(
        tool_name,
        runtime_paths=runtime_paths,
        extra_env_passthrough=extra_env_passthrough,
    )
    original_functions = toolkit.functions
    original_async_functions = toolkit.async_functions
    toolkit.functions = {
        function_name: _wrap_sync_function(
            function,
            tool_name,
            function_name,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            shared_storage_root_path=shared_storage_root_path,
            tool_config_overrides=tool_config_overrides,
            tool_init_overrides=tool_init_overrides,
            execution_env=execution_env,
            extra_env_passthrough=extra_env_passthrough,
            worker_target=worker_target,
        )
        for function_name, function in original_functions.items()
    }
    toolkit.async_functions = {
        function_name: _wrap_async_function(
            function,
            tool_name,
            function_name,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            shared_storage_root_path=shared_storage_root_path,
            tool_config_overrides=tool_config_overrides,
            tool_init_overrides=tool_init_overrides,
            execution_env=execution_env,
            extra_env_passthrough=extra_env_passthrough,
            worker_target=worker_target,
        )
        for function_name, function in original_async_functions.items()
    }
    return toolkit
