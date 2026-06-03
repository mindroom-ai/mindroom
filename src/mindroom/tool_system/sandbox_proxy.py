"""Generic proxy wrapper for routing tool calls to a sandbox runner service."""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import hashlib
import json
import os
import secrets
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import httpx

from mindroom.constants import EXECUTION_ENV_TOOL_NAMES, build_execution_tool_env
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.tool_system.runtime_context import (
    WorkerProgressEvent,
    WorkerProgressPump,
    get_tool_runtime_context,
    get_worker_progress_pump,
)
from mindroom.tool_system.worker_proxy_client import (
    SANDBOX_PROXY_SAVE_ATTACHMENT_PATH,
    WorkerProxyClientConfig,
    execute_worker_proxy_request,
    post_worker_proxy_json,
    record_proxy_response_failure_for_worker,
    to_json_compatible,
)
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    WorkerScope,
    resolve_unscoped_worker_key,
    tool_stays_local,
)
from mindroom.workers.models import ProgressSink, WorkerHandle, WorkerReadyProgress, WorkerSpec
from mindroom.workers.runtime import (
    get_primary_worker_manager,
    lease_primary_worker_manager,
    primary_worker_backend_available,
    primary_worker_backend_is_dedicated,
    primary_worker_backend_name,
    serialized_kubernetes_worker_validation_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agno.tools.function import Function
    from agno.tools.toolkit import Toolkit

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.workers.manager import WorkerManager

_DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS = 120.0
_DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS = 60
_MAX_CREDENTIAL_LEASE_TTL_SECONDS = 3600
_INLINE_ATTACHMENT_BYTES_ENV = "MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES"
_DEFAULT_INLINE_ATTACHMENT_BYTES = 16 * 1024 * 1024
_ATTACHMENT_SAVE_WORKSPACE_CONSUMER_TOOLS = frozenset({"file", "coding", "python", "shell"})


class _AttachmentSavePayloadFields(TypedDict):
    bytes_b64: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class WorkerAttachmentSaveReceipt:
    """Receipt returned after writing attachment bytes into a worker workspace."""

    worker_path: str
    size_bytes: int
    sha256: str


def _attachment_save_payload_fields(payload_bytes: bytes) -> _AttachmentSavePayloadFields:
    """Return the byte-integrity fields for one save-attachment request."""
    return {
        "bytes_b64": base64.b64encode(payload_bytes).decode("ascii"),
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "size_bytes": len(payload_bytes),
    }


def decode_attachment_save_bytes(*, bytes_b64: str, sha256: str, size_bytes: int | None) -> bytes | str:
    """Decode and integrity-check one save-attachment payload."""
    if size_bytes is not None and (type(size_bytes) is not int or size_bytes < 0):
        return "Attachment size_bytes must be a non-negative integer."
    try:
        payload_bytes = base64.b64decode(bytes_b64.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error):
        return "Attachment bytes are not valid base64."
    if size_bytes is not None and len(payload_bytes) != size_bytes:
        return "Attachment byte length does not match the request receipt."
    actual_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    if not secrets.compare_digest(actual_sha256, sha256):
        return "Attachment SHA256 does not match the request payload."
    return payload_bytes


def _validate_attachment_save_receipt(
    data: Mapping[str, object],
    *,
    requested_path: str,
    byte_count: int,
    sha256: str,
) -> WorkerAttachmentSaveReceipt | str:
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
        return "Sandbox save-attachment response is missing its receipt fields."
    if worker_path != requested_path:
        return "Sandbox save-attachment response path does not match the requested workspace path."
    if response_size != byte_count:
        return "Sandbox save-attachment response size does not match the sent bytes."
    if not secrets.compare_digest(response_sha256, sha256):
        return "Sandbox save-attachment response SHA256 does not match the sent bytes."
    return WorkerAttachmentSaveReceipt(
        worker_path=worker_path,
        size_bytes=response_size,
        sha256=response_sha256,
    )


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


@dataclass(frozen=True)
class _PrimaryWorkerManagerContext:
    """Runtime-context-derived parameters for resolving the primary worker manager."""

    storage_root: Path
    kubernetes_tool_validation_snapshot: dict[str, dict[str, object]] | None
    worker_grantable_credentials: frozenset[str] | None


def _read_proxy_url(runtime_paths: RuntimePaths) -> str | None:
    value = (runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["proxy_url"], default="") or "").strip()
    if not value:
        return None
    return value.rstrip("/")


def _read_proxy_token(runtime_paths: RuntimePaths) -> str | None:
    value = (runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"], default="") or "").strip()
    if not value:
        return None
    return value


def _read_proxy_timeout(runtime_paths: RuntimePaths) -> float:
    raw = runtime_paths.env_value(
        SANDBOX_RUNTIME_ENV_BY_KEY["proxy_timeout_seconds"],
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
        or str(_DEFAULT_INLINE_ATTACHMENT_BYTES)
    )
    try:
        limit = int(raw_value)
    except ValueError:
        return _DEFAULT_INLINE_ATTACHMENT_BYTES
    if limit <= 0:
        return _DEFAULT_INLINE_ATTACHMENT_BYTES
    return limit


def _read_execution_mode(runtime_paths: RuntimePaths) -> str | None:
    raw = runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["execution_mode"])
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if not normalized:
        return None
    return normalized


def _read_credential_lease_ttl(runtime_paths: RuntimePaths) -> int:
    raw = runtime_paths.env_value(
        SANDBOX_RUNTIME_ENV_BY_KEY["credential_lease_ttl_seconds"],
        default=str(_DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS),
    )
    try:
        ttl_seconds = int(raw or _DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS)
    except ValueError:
        ttl_seconds = _DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS
    return max(1, min(_MAX_CREDENTIAL_LEASE_TTL_SECONDS, ttl_seconds))


def _read_proxy_tools(runtime_paths: RuntimePaths, execution_mode: str | None) -> set[str] | None:
    default = "" if execution_mode in {"selective", "sandbox_selective"} else "*"
    raw_value = (runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["proxy_tools"], default=default) or default).strip()
    if raw_value == "*":
        return None
    if not raw_value:
        return set()
    return {part.strip() for part in raw_value.split(",") if part.strip()}


def _read_credential_policy(runtime_paths: RuntimePaths) -> dict[str, tuple[str, ...]]:
    raw_policy = (
        runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["credential_policy_json"], default="") or ""
    ).strip()
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
        runner_mode=runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]),
        proxy_url=_read_proxy_url(runtime_paths),
        proxy_token=_read_proxy_token(runtime_paths),
        proxy_timeout_seconds=_read_proxy_timeout(runtime_paths),
        execution_mode=execution_mode,
        credential_lease_ttl_seconds=_read_credential_lease_ttl(runtime_paths),
        proxy_tools=_read_proxy_tools(runtime_paths, execution_mode),
        credential_policy=_read_credential_policy(runtime_paths),
    )


def _worker_proxy_client_config(proxy_config: _SandboxProxyConfig) -> WorkerProxyClientConfig:
    return WorkerProxyClientConfig(
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
        proxy_timeout_seconds=proxy_config.proxy_timeout_seconds,
        credential_lease_ttl_seconds=proxy_config.credential_lease_ttl_seconds,
        credential_policy=proxy_config.credential_policy,
    )


def _build_worker_routing_payload(  # noqa: C901, PLR0912
    *,
    runtime_paths: RuntimePaths,
    tool_name: str,
    function_name: str,
    worker_target: ResolvedWorkerTarget | None,
    progress_sink: ProgressSink | None = None,
    worker_manager: WorkerManager | None = None,
) -> tuple[dict[str, object], WorkerHandle | None]:
    proxy_config = sandbox_proxy_config(runtime_paths)
    resolved_worker_manager = worker_manager or _get_worker_manager(runtime_paths, proxy_config)
    worker_scope = worker_target.worker_scope if worker_target is not None else None
    execution_identity = worker_target.execution_identity if worker_target is not None else None
    routing_agent_name = worker_target.routing_agent_name if worker_target is not None else None
    if worker_scope is None:
        if not primary_worker_backend_is_dedicated(runtime_paths):
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
                "when using a dedicated worker backend."
            )
            raise RuntimeError(msg)

        worker_key = resolve_unscoped_worker_key(
            agent_name=effective_agent_name,
            execution_identity=execution_identity,
            tenant_id=worker_target.tenant_id if worker_target is not None else None,
            account_id=worker_target.account_id if worker_target is not None else None,
        )
        if progress_sink is None:
            worker_handle = resolved_worker_manager.ensure_worker(WorkerSpec(worker_key))
        else:
            worker_handle = resolved_worker_manager.ensure_worker(
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
    if progress_sink is None:
        worker_handle = resolved_worker_manager.ensure_worker(
            WorkerSpec(worker_key, private_agent_names=resolved_private_agent_names),
        )
    else:
        worker_handle = resolved_worker_manager.ensure_worker(
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


def _primary_worker_manager_context(runtime_paths: RuntimePaths) -> _PrimaryWorkerManagerContext:
    """Resolve runtime-context-dependent primary worker manager parameters."""
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
    return _PrimaryWorkerManagerContext(
        storage_root=storage_root,
        kubernetes_tool_validation_snapshot=kubernetes_tool_validation_snapshot,
        worker_grantable_credentials=(
            context.config.get_worker_grantable_credentials() if context is not None else None
        ),
    )


def _get_worker_manager(
    runtime_paths: RuntimePaths,
    proxy_config: _SandboxProxyConfig,
) -> WorkerManager:
    manager_context = _primary_worker_manager_context(runtime_paths)
    return get_primary_worker_manager(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
        storage_root=manager_context.storage_root,
        kubernetes_tool_validation_snapshot=manager_context.kubernetes_tool_validation_snapshot,
        worker_grantable_credentials=manager_context.worker_grantable_credentials,
    )


def _execution_env_payload(
    tool_name: str,
    *,
    runtime_paths: RuntimePaths,
    extra_env_passthrough: str | None = None,
) -> dict[str, str] | None:
    """Return explicit execution env only for tools that intentionally support it."""
    if tool_name not in EXECUTION_ENV_TOOL_NAMES:
        return None
    return build_execution_tool_env(
        tool_name,
        runtime_paths,
        extra_env_passthrough=extra_env_passthrough,
        shell_process_env=runtime_paths.process_env,
    )


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
    worker_manager: WorkerManager,
    error: str,
) -> None:
    """Record a worker save protocol/integrity failure against worker health."""
    if worker_handle is not None:
        worker_manager.record_failure(worker_handle.worker_key, error)


def _validated_worker_save_receipt(
    data: Mapping[str, object],
    *,
    requested_path: str,
    byte_count: int,
    sha256: str,
    worker_handle: WorkerHandle | None,
    worker_manager: WorkerManager,
) -> WorkerAttachmentSaveReceipt:
    result = _validate_attachment_save_receipt(
        data,
        requested_path=requested_path,
        byte_count=byte_count,
        sha256=sha256,
    )
    if isinstance(result, str):
        _record_worker_save_failure(
            worker_handle=worker_handle,
            worker_manager=worker_manager,
            error=result,
        )
        raise RuntimeError(result)  # noqa: TRY004
    return result


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
    worker_manager = _get_worker_manager(runtime_paths, proxy_config)
    worker_payload, worker_handle = _build_worker_routing_payload(
        runtime_paths=runtime_paths,
        tool_name="attachments",
        function_name="get_attachment",
        worker_target=worker_target,
        progress_sink=None,
        worker_manager=worker_manager,
    )
    if worker_handle is None and proxy_config.proxy_url is None:
        return None

    attachment_fields = _attachment_save_payload_fields(payload_bytes)
    request_payload: dict[str, object] = {
        **worker_payload,
        "attachment_id": attachment_id,
        "mindroom_output_path": mindroom_output_path,
        **attachment_fields,
        "mime_type": mime_type,
        "filename": filename,
    }

    data = post_worker_proxy_json(
        config=_worker_proxy_client_config(proxy_config),
        payload=request_payload,
        worker_handle=worker_handle,
        worker_manager=worker_manager,
        proxy_path=SANDBOX_PROXY_SAVE_ATTACHMENT_PATH,
        worker_operation="save-attachment",
        client_factory=httpx.Client,
    )

    if not isinstance(data, dict):
        msg = "Sandbox save-attachment returned a non-object response."
        _record_worker_save_failure(
            worker_handle=worker_handle,
            worker_manager=worker_manager,
            error=msg,
        )
        raise TypeError(msg)
    response_data = {str(key): value for key, value in data.items()}
    if response_data.get("ok") is True:
        receipt = _validated_worker_save_receipt(
            response_data,
            requested_path=mindroom_output_path,
            byte_count=byte_count,
            sha256=attachment_fields["sha256"],
            worker_handle=worker_handle,
            worker_manager=worker_manager,
        )
        if worker_handle is not None:
            worker_manager.touch_worker(worker_handle.worker_key)
        return receipt

    error = response_data.get("error") or "Sandbox attachment save failed."
    record_proxy_response_failure_for_worker(
        worker_handle=worker_handle,
        worker_manager=worker_manager,
        error=str(error),
        failure_kind=response_data.get("failure_kind"),
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
    return primary_worker_backend_is_dedicated(runtime_paths)


def _call_proxy_sync(
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
    manager_context = _primary_worker_manager_context(runtime_paths)
    with lease_primary_worker_manager(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
        storage_root=manager_context.storage_root,
        kubernetes_tool_validation_snapshot=manager_context.kubernetes_tool_validation_snapshot,
        worker_grantable_credentials=manager_context.worker_grantable_credentials,
    ) as worker_manager:
        if progress_sink is None:
            worker_payload, worker_handle = _build_worker_routing_payload(
                runtime_paths=runtime_paths,
                tool_name=tool_name,
                function_name=function_name,
                worker_target=worker_target,
                worker_manager=worker_manager,
            )
        else:
            worker_payload, worker_handle = _build_worker_routing_payload(
                runtime_paths=runtime_paths,
                tool_name=tool_name,
                function_name=function_name,
                worker_target=worker_target,
                progress_sink=progress_sink,
                worker_manager=worker_manager,
            )
        payload.update(worker_payload)
        if execution_env:
            payload["execution_env"] = execution_env
        if extra_env_passthrough is not None:
            payload["extra_env_passthrough"] = extra_env_passthrough
        if tool_config_overrides:
            payload["tool_config_overrides"] = to_json_compatible(tool_config_overrides)
        worker_key = worker_payload.get("worker_key")
        portable_tool_init_overrides = _portable_tool_init_overrides(
            tool_init_overrides,
            shared_storage_root_path=shared_storage_root_path,
            worker_key=worker_key if isinstance(worker_key, str) else None,
        )
        if portable_tool_init_overrides:
            payload["tool_init_overrides"] = to_json_compatible(portable_tool_init_overrides)
        return execute_worker_proxy_request(
            config=_worker_proxy_client_config(proxy_config),
            payload=payload,
            credentials_manager=credentials_manager,
            tool_name=tool_name,
            function_name=function_name,
            worker_target=worker_target,
            worker_handle=worker_handle,
            worker_manager=worker_manager,
            client_factory=httpx.Client,
        )


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
