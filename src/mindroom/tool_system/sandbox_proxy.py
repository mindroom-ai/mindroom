"""Generic proxy wrapper for routing tool calls to a sandbox runner service."""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import hashlib
import inspect
import json
import math
import secrets
import weakref
from contextlib import suppress
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

from mindroom.browser.call_limit import browser_request_budget_seconds
from mindroom.constants import EXECUTION_ENV_TOOL_NAMES, RETAINED_MEDIA_MAX_BYTES, build_execution_tool_env
from mindroom.egress_broker.env import egress_agent_scope, egress_broker_enabled
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.shell.execution import shell_request_budget_seconds
from mindroom.tool_system.declarations import SupportsPrimaryCallPlacement
from mindroom.tool_system.dedicated_threads import WORKER_PROXY_CALL_EXECUTOR
from mindroom.tool_system.media_transport import decode_media_result, is_media_result_envelope
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT
from mindroom.tool_system.runtime_context import (
    WorkerProgressEvent,
    WorkerProgressPump,
    get_worker_progress_pump,
)
from mindroom.tool_system.tool_environment import ToolEnvironment, current_tool_environment
from mindroom.tool_system.tool_function import ToolValue
from mindroom.tool_system.worker_proxy_client import (
    WorkerCallCancellation,
    WorkerProxyClientConfig,
    execute_worker_proxy_request,
    post_worker_proxy_request,
    record_proxy_response_failure_for_worker,
    to_json_compatible,
)
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    resolve_unscoped_worker_key,
    serialize_tool_execution_identity,
)
from mindroom.workers.backend_kinds import WorkerBackendKind, worker_backend_kind
from mindroom.workers.models import ProgressSink, WorkerHandle, WorkerReadyProgress, WorkerSpec
from mindroom.workers.protocol import (
    WORKER_PROTOCOL_VERSION,
    CallEnvironment,
    ExecuteRequest,
    SaveAttachmentRequest,
    SaveAttachmentResponse,
    ViewFileRequest,
    ViewFileResponse,
    WorkerRoute,
)
from mindroom.workers.runtime import lease_primary_worker_manager, worker_config_snapshot

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.tool_system.declarations import ToolMetadata
    from mindroom.tool_system.registry import ToolRegistry
    from mindroom.workers.backend import WorkerBackend
    from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend
    from mindroom.workers.runtime import PrimaryWorkerManagerLease

_DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS = 120.0
_DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS = 60
_MAX_CREDENTIAL_LEASE_TTL_SECONDS = 3600
INLINE_ATTACHMENT_BYTES_ENV = "MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES"
_SANDBOX_ALL_EXECUTION_MODES = frozenset({"all", "sandbox_all"})
_SANDBOX_SELECTIVE_EXECUTION_MODES = frozenset({"selective", "sandbox_selective"})
_UNSAFE_LOCAL_EXECUTION_MODES = frozenset({"off", "local", "disabled"})


@dataclass(frozen=True)
class WorkerAttachmentSaveReceipt:
    """Receipt returned after writing attachment bytes into a worker workspace."""

    worker_path: str
    size_bytes: int
    sha256: str


def decode_attachment_save_bytes(*, bytes_b64: str, sha256: str, size_bytes: int) -> bytes | str:
    """Decode and integrity-check one save-attachment payload."""
    if type(size_bytes) is not int or size_bytes < 0:
        return "Attachment size_bytes must be a non-negative integer."
    try:
        payload_bytes = base64.b64decode(bytes_b64.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error):
        return "Attachment bytes are not valid base64."
    if len(payload_bytes) != size_bytes:
        return "Attachment byte length does not match the request receipt."
    actual_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    if not secrets.compare_digest(actual_sha256, sha256):
        return "Attachment SHA256 does not match the request payload."
    return payload_bytes


def _validate_attachment_save_receipt(
    data: SaveAttachmentResponse,
    *,
    requested_path: str,
    byte_count: int,
    sha256: str,
) -> WorkerAttachmentSaveReceipt | str:
    """Validate one successful worker save response against the sent bytes."""
    worker_path = data.worker_path
    response_size = data.size_bytes
    response_sha256 = data.sha256
    if worker_path is None or response_size is None or response_size < 0 or response_sha256 is None:
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


def _read_proxy_url(runtime_paths: RuntimePaths) -> str | None:
    value = runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["proxy_url"])
    return None if value is None else value.strip().rstrip("/")


def _read_proxy_token(runtime_paths: RuntimePaths) -> str | None:
    value = runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"])
    return None if value is None else value.strip()


def _read_proxy_timeout(runtime_paths: RuntimePaths) -> float:
    raw = runtime_paths.env_value(
        SANDBOX_RUNTIME_ENV_BY_KEY["proxy_timeout_seconds"],
        default=str(_DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS),
    )
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS


def inline_attachment_byte_limit(runtime_paths: RuntimePaths) -> int:
    """Return the hard cap for inline primary-to-worker attachment saves, by default the retained media limit."""
    raw_value = runtime_paths.env_value(INLINE_ATTACHMENT_BYTES_ENV, default=str(RETAINED_MEDIA_MAX_BYTES))
    try:
        limit = int(raw_value)
    except ValueError:
        return RETAINED_MEDIA_MAX_BYTES
    if limit <= 0:
        return RETAINED_MEDIA_MAX_BYTES
    return limit


def _read_execution_mode(runtime_paths: RuntimePaths) -> str | None:
    raw = runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["execution_mode"])
    normalized = (raw or "").strip().lower()
    if not normalized:
        return None
    supported = _SANDBOX_ALL_EXECUTION_MODES | _SANDBOX_SELECTIVE_EXECUTION_MODES | _UNSAFE_LOCAL_EXECUTION_MODES
    if normalized not in supported:
        msg = f"Invalid MINDROOM_SANDBOX_EXECUTION_MODE {raw!r}; expected one of {', '.join(sorted(supported))}"
        raise ValueError(msg)
    return normalized


def _read_credential_lease_ttl(runtime_paths: RuntimePaths) -> int:
    raw = runtime_paths.env_value(
        SANDBOX_RUNTIME_ENV_BY_KEY["credential_lease_ttl_seconds"],
        default=str(_DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS),
    )
    try:
        ttl_seconds = int(raw)
    except ValueError:
        ttl_seconds = _DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS
    return max(1, min(_MAX_CREDENTIAL_LEASE_TTL_SECONDS, ttl_seconds))


def _read_proxy_tools(
    runtime_paths: RuntimePaths,
    execution_mode: str | None,
    *,
    proxy_url: str | None,
) -> set[str] | None:
    default = (
        "*"
        if execution_mode in _SANDBOX_ALL_EXECUTION_MODES
        or (
            execution_mode is None
            and proxy_url is not None
            and worker_backend_kind(runtime_paths) is WorkerBackendKind.STATIC_RUNNER
        )
        else ""
    )
    raw_value = runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["proxy_tools"], default=default).strip()
    if raw_value == "*":
        return None
    if not raw_value:
        return set()
    return {part.strip() for part in raw_value.split(",") if part.strip()}


def _read_credential_policy(runtime_paths: RuntimePaths) -> dict[str, tuple[str, ...]]:
    raw_policy = runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["credential_policy_json"])
    if raw_policy is None:
        return {}

    try:
        parsed = json.loads(raw_policy)
    except json.JSONDecodeError:
        return {}

    if not isinstance(parsed, dict):
        return {}

    return {
        selector.strip(): tuple(service.strip() for service in services if isinstance(service, str) and service.strip())
        for selector, services in parsed.items()
        if isinstance(selector, str) and isinstance(services, list)
    }


def sandbox_proxy_config(runtime_paths: RuntimePaths) -> _SandboxProxyConfig:
    """Return sandbox proxy settings for one explicit runtime context."""
    execution_mode = _read_execution_mode(runtime_paths)
    proxy_url = _read_proxy_url(runtime_paths)
    return _SandboxProxyConfig(
        runner_mode=runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]),
        proxy_url=proxy_url,
        proxy_token=_read_proxy_token(runtime_paths),
        proxy_timeout_seconds=_read_proxy_timeout(runtime_paths),
        execution_mode=execution_mode,
        credential_lease_ttl_seconds=_read_credential_lease_ttl(runtime_paths),
        proxy_tools=_read_proxy_tools(runtime_paths, execution_mode, proxy_url=proxy_url),
        credential_policy=_read_credential_policy(runtime_paths),
    )


def _worker_proxy_client_config(
    proxy_config: _SandboxProxyConfig,
    runtime_paths: RuntimePaths,
    *,
    request_budget_seconds: float = 0.0,
) -> WorkerProxyClientConfig:
    return WorkerProxyClientConfig(
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
        # A call the runner lets run longer than the proxy timeout keeps the primary waiting for its answer.
        proxy_timeout_seconds=max(proxy_config.proxy_timeout_seconds, request_budget_seconds),
        credential_lease_ttl_seconds=proxy_config.credential_lease_ttl_seconds,
        credential_policy=proxy_config.credential_policy,
        lease_tool_credentials=worker_backend_kind(runtime_paths) is WorkerBackendKind.STATIC_RUNNER,
    )


def _worker_route(
    *,
    runtime_paths: RuntimePaths,
    runtime_config: Config | None,
    tool_name: str,
    function_name: str,
    worker_target: ResolvedWorkerTarget | None,
    worker_manager: WorkerBackend,
    base_dir: str | None,
    progress_sink: ProgressSink | None = None,
) -> tuple[WorkerRoute, WorkerHandle | None]:
    """Return the route of one request, ensuring the dedicated worker that serves it.

    ``base_dir`` is the calling agent's tool workspace in this primary's paths; a request that names a worker
    carries it relative to the shared storage root, which the worker mounts at its own path.
    """
    worker_scope = worker_target.worker_scope if worker_target is not None else None
    execution_identity = worker_target.execution_identity if worker_target is not None else None
    routing_agent_name = worker_target.routing_agent_name if worker_target is not None else None
    private_agent_names: frozenset[str] | None = None
    worker_handle: WorkerHandle | None = None
    if worker_scope is None:
        worker_key: str | None = None
        if worker_backend_kind(runtime_paths).dedicated:
            if worker_target is None or routing_agent_name is None:
                msg = (
                    f"Unscoped worker-routed tool '{tool_name}.{function_name}' requires an agent name "
                    "when using a dedicated worker backend."
                )
                raise RuntimeError(msg)
            worker_key = resolve_unscoped_worker_key(
                agent_name=routing_agent_name,
                tenant_id=worker_target.tenant_id,
                account_id=worker_target.account_id,
            )
            worker_handle = worker_manager.ensure_worker(WorkerSpec(worker_key), progress_sink=progress_sink)
        else:
            # The shared runner is the only backend that is not dedicated, and an unscoped call reaches it without a
            # worker handle, so its protocol check happens here.
            cast("StaticSandboxRunnerBackend", worker_manager).require_compatible_runner()
    else:
        if worker_target is None or execution_identity is None:
            msg = f"Worker-routed tool '{tool_name}.{function_name}' requires execution identity context."
            raise RuntimeError(msg)
        if worker_scope == "user_agent" and worker_target.private_agent_names is None:
            msg = (
                f"Worker-routed tool '{tool_name}.{function_name}' with scope 'user_agent' "
                "requires explicit private visibility."
            )
            raise RuntimeError(msg)
        worker_key = worker_target.worker_key
        if worker_key is None:
            msg = (
                f"Worker scope '{worker_scope}' for tool '{tool_name}.{function_name}' "
                "could not be resolved from the current execution identity."
            )
            raise RuntimeError(msg)
        private_agent_names = worker_target.private_agent_names if worker_scope == "user_agent" else None
        worker_handle = worker_manager.ensure_worker(
            WorkerSpec(worker_key, private_agent_names=private_agent_names),
            progress_sink=progress_sink,
        )
    route = WorkerRoute(
        protocol=WORKER_PROTOCOL_VERSION,
        worker_key=worker_key,
        worker_scope=worker_scope,
        routing_agent_name=routing_agent_name,
        execution_identity=(
            serialize_tool_execution_identity(execution_identity) if execution_identity is not None else None
        ),
        private_agent_names=sorted(private_agent_names) if private_agent_names is not None else None,
        config_snapshot=runner_config_snapshot(runtime_paths, runtime_config),
        base_dir=_route_base_dir(
            base_dir,
            shared_storage_root=runtime_paths.storage_root,
            keyed=worker_key is not None,
        ),
    )
    return route, worker_handle


def _route_base_dir(base_dir: str | None, *, shared_storage_root: Path, keyed: bool) -> str | None:
    """Spell ``base_dir`` relative to the shared storage root for a request that names a worker."""
    if base_dir is None or not keyed:
        return base_dir
    path = Path(base_dir).expanduser()
    if not path.is_absolute():
        return base_dir
    try:
        return path.resolve().relative_to(shared_storage_root.resolve()).as_posix()
    except ValueError:
        return base_dir


def runner_config_snapshot(runtime_paths: RuntimePaths, runtime_config: Config | None) -> dict[str, object] | None:
    """Return the live config fields runners resolve, for one runner or worker request.

    The static runner and Kubernetes workers mount no config and resolve agents only from this
    snapshot.  Docker workers read a per-worker projection of the same fields whose config-relative
    paths are rewritten for the container, so they get no request snapshot.
    """
    if runtime_config is None or worker_backend_kind(runtime_paths) is WorkerBackendKind.DOCKER:
        return None
    return worker_config_snapshot(runtime_config)


def _lease_worker_manager(runtime_paths: RuntimePaths, proxy_config: _SandboxProxyConfig) -> PrimaryWorkerManagerLease:
    """Borrow the primary worker manager for one request under these proxy settings."""
    return lease_primary_worker_manager(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
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


def _tool_defaults_to_worker(metadata: ToolMetadata | None) -> bool:
    return metadata is not None and metadata.default_execution_target.value == "worker"


def save_attachment_to_worker(
    *,
    runtime_paths: RuntimePaths,
    runtime_config: Config | None,
    worker_target: ResolvedWorkerTarget | None,
    workspace_root: Path | None,
    attachment_id: str,
    mindroom_output_path: str,
    payload_bytes: bytes,
) -> WorkerAttachmentSaveReceipt:
    """Write attachment bytes into the calling agent's workspace in the worker that serves it."""
    byte_limit = inline_attachment_byte_limit(runtime_paths)
    byte_count = len(payload_bytes)
    if byte_count > byte_limit:
        msg = (
            f"Attachment {attachment_id} exceeds inline save-to-disk size limit "
            f"({byte_count} bytes > {byte_limit} bytes)."
        )
        raise RuntimeError(msg)

    proxy_config = sandbox_proxy_config(runtime_paths)
    with _lease_worker_manager(runtime_paths, proxy_config) as worker_manager:
        route, worker_handle = _worker_route(
            runtime_paths=runtime_paths,
            runtime_config=runtime_config,
            tool_name="attachments",
            function_name="get_attachment",
            worker_target=worker_target,
            worker_manager=worker_manager,
            base_dir=str(workspace_root) if workspace_root is not None else None,
        )
        sha256 = hashlib.sha256(payload_bytes).hexdigest()
        response = post_worker_proxy_request(
            config=_worker_proxy_client_config(proxy_config, runtime_paths),
            request=SaveAttachmentRequest(
                **dict(route),
                attachment_id=attachment_id,
                mindroom_output_path=mindroom_output_path,
                bytes_b64=base64.b64encode(payload_bytes).decode("ascii"),
                sha256=sha256,
                size_bytes=byte_count,
            ),
            response_model=SaveAttachmentResponse,
            worker_handle=worker_handle,
            worker_manager=worker_manager,
            worker_operation="save-attachment",
            failure="Sandbox attachment save failed.",
            client_factory=httpx.Client,
        )
        receipt = _validate_attachment_save_receipt(
            response,
            requested_path=mindroom_output_path,
            byte_count=byte_count,
            sha256=sha256,
        )
        if isinstance(receipt, str):
            record_proxy_response_failure_for_worker(
                worker_handle=worker_handle,
                worker_manager=worker_manager,
                error=receipt,
                failure_kind=None,
            )
            raise RuntimeError(receipt)  # noqa: TRY004
        if worker_handle is not None:
            worker_manager.touch_worker(worker_handle.worker_key)
        return receipt


def view_file_from_worker(
    *,
    runtime_paths: RuntimePaths,
    runtime_config: Config | None,
    worker_target: ResolvedWorkerTarget | None,
    workspace_root: Path | None,
    path: str,
) -> ToolValue:
    """View one file in the selected worker, for an agent whose workspace tools run in a worker."""
    proxy_config = sandbox_proxy_config(runtime_paths)
    with _lease_worker_manager(runtime_paths, proxy_config) as worker_manager:
        route, worker_handle = _worker_route(
            runtime_paths=runtime_paths,
            runtime_config=runtime_config,
            tool_name="attachments",
            function_name="view_file",
            worker_target=worker_target,
            worker_manager=worker_manager,
            base_dir=str(workspace_root) if workspace_root is not None else None,
        )
        if workspace_root is not None:
            # Translate the mount spelling only; resolve and authorize the path inside the worker.
            with suppress(ValueError):
                path = Path(path).relative_to(workspace_root).as_posix()

        response = post_worker_proxy_request(
            config=_worker_proxy_client_config(proxy_config, runtime_paths),
            request=ViewFileRequest(**dict(route), path=path),
            response_model=ViewFileResponse,
            worker_handle=worker_handle,
            worker_manager=worker_manager,
            worker_operation="view-file",
            failure="Sandbox file view failed.",
            client_factory=httpx.Client,
        )
        try:
            result = decode_media_result(response.result)
        except ValueError as exc:
            record_proxy_response_failure_for_worker(
                worker_handle=worker_handle,
                worker_manager=worker_manager,
                error=str(exc),
                failure_kind=None,
            )
            raise
        if not isinstance(result, ToolValue):
            msg = "Sandbox view-file returned a non-media result."
            record_proxy_response_failure_for_worker(
                worker_handle=worker_handle,
                worker_manager=worker_manager,
                error=msg,
                failure_kind=None,
            )
            raise TypeError(msg)
        if worker_handle is not None:
            worker_manager.touch_worker(worker_handle.worker_key)
        return result


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


def _sandbox_proxy_requested_for_tool(
    *,
    tool_name: str,
    metadata: ToolMetadata | None,
    proxy_config: _SandboxProxyConfig,
    worker_tools: Collection[str] | None,
) -> bool:
    """Return whether config requests worker/proxy routing for one tool."""
    if worker_tools is not None:
        requested = tool_name in worker_tools
    elif proxy_config.execution_mode in _UNSAFE_LOCAL_EXECUTION_MODES:
        requested = False
    elif proxy_config.execution_mode in _SANDBOX_ALL_EXECUTION_MODES:
        requested = True
    elif proxy_config.execution_mode in _SANDBOX_SELECTIVE_EXECUTION_MODES:
        requested = proxy_config.proxy_tools is None or tool_name in proxy_config.proxy_tools
    elif proxy_config.execution_mode is not None:
        requested = False
    elif proxy_config.proxy_tools is None:
        requested = True
    elif proxy_config.proxy_tools:
        requested = tool_name in proxy_config.proxy_tools
    else:
        requested = _tool_defaults_to_worker(metadata)
    return requested


@dataclass(frozen=True)
class ToolPlacement:
    """The sandbox settings of one runtime, which decide where each tool of a registry runs: primary or worker.

    Settings are parsed once; the worker backend is checked only when a tool's placement depends on it.
    """

    runtime_paths: RuntimePaths
    proxy_config: _SandboxProxyConfig
    tools: ToolRegistry

    @cached_property
    def _backend_kind(self) -> WorkerBackendKind:
        return worker_backend_kind(self.runtime_paths)

    def runs_in_worker(self, tool_name: str, worker_tools: Collection[str] | None) -> bool:
        """Return whether calls of ``tool_name`` run in a worker for an agent with these ``worker_tools``.

        ``worker_tools`` is the agent's resolved list, which takes precedence over the env-var based
        execution mode and proxy tools; None follows them, and an empty list routes nothing.
        """
        proxy_config = self.proxy_config
        metadata = self.tools.get(tool_name)
        if proxy_config.runner_mode or (
            metadata is not None and (metadata.requires_primary_runtime or metadata.requires_room_context)
        ):
            return False
        if not _sandbox_proxy_requested_for_tool(
            tool_name=tool_name,
            metadata=metadata,
            proxy_config=proxy_config,
            worker_tools=worker_tools,
        ):
            return False
        if (
            worker_tools is None
            and proxy_config.execution_mode is None
            and proxy_config.proxy_tools is not None
            and not proxy_config.proxy_tools
            and proxy_config.proxy_url is None
            and self._backend_kind is WorkerBackendKind.STATIC_RUNNER
        ):
            return False
        # A dedicated backend routes even when its config is incomplete, so the call fails instead of running here.
        if self._backend_kind.dedicated or proxy_config.proxy_url:
            return True
        # Execution tools also fail closed on the default static backend. To allow
        # host execution, operators must opt in with MINDROOM_SANDBOX_EXECUTION_MODE
        # set to off/local/disabled or MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS=true.
        return not (
            _tool_defaults_to_worker(metadata)
            and self.runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["unsafe_allow_local_execution_tools"])
        )


def tool_placement(runtime_paths: RuntimePaths, tools: ToolRegistry) -> ToolPlacement:
    """Return where each tool of ``tools`` runs in one runtime."""
    return ToolPlacement(runtime_paths=runtime_paths, proxy_config=sandbox_proxy_config(runtime_paths), tools=tools)


def primary_owns_tool_settings(tool_name: str, tools: ToolRegistry, *, runtime_paths: RuntimePaths) -> bool:
    """Return whether this primary process owns the settings of a tool in ``tools``.

    The primary builds every tool, including tools whose calls run in a worker, so a worker-writable store never
    configures it; routed calls receive the settings through a per-call lease instead.
    """
    return tool_name in tools and not sandbox_proxy_config(runtime_paths).runner_mode


def _worker_argument_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_worker_argument_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _worker_argument_value(item) for key, item in value.items()}
    msg = f"Unsupported worker argument type: {type(value).__name__}"
    raise TypeError(msg)


def _authored_arguments(kwargs: dict[str, object]) -> dict[str, object]:
    """Return a call's keyword arguments without ``mindroom_output_path``, which its function's own signature lacks."""
    return {name: value for name, value in kwargs.items() if name != OUTPUT_PATH_ARGUMENT}


def _worker_call_arguments(
    args: tuple[object, ...],
    kwargs: dict[str, object],
    *,
    entrypoint: Callable[..., object],
    inert_agent: bool,
) -> tuple[list[object], dict[str, object]]:
    """Serialize one call's arguments for the worker as the call sent them, keeping its shape.

    A function declared to leave its injected ``agent`` unused receives None there, since the primary's run state
    stays on the primary.
    """
    if inert_agent:
        bound = inspect.signature(entrypoint).bind(*args, **_authored_arguments(kwargs))
        if "agent" in bound.arguments:
            if "agent" in kwargs:
                kwargs = {**kwargs, "agent": None}
            else:
                output_path = {name: value for name, value in kwargs.items() if name == OUTPUT_PATH_ARGUMENT}
                bound.arguments["agent"] = None
                args, kwargs = bound.args, {**bound.kwargs, **output_path}
    return [_worker_argument_value(arg) for arg in args], cast("dict[str, object]", _worker_argument_value(kwargs))


def _call_proxy_sync(
    *,
    runtime_paths: RuntimePaths,
    tool_name: str,
    function_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    credentials_manager: CredentialsManager | None,
    function_entrypoint: Callable[..., object],
    tool_config_overrides: dict[str, object] | None,
    tool_init_overrides: dict[str, object] | None,
    execution_env: dict[str, str] | None,
    extra_env_passthrough: str | None,
    worker_target: ResolvedWorkerTarget | None,
    runtime_config: Config | None,
    tools: ToolRegistry,
    cancellation: WorkerCallCancellation | None = None,
) -> object:
    metadata = tools.get(tool_name)
    wire_args, wire_kwargs = _worker_call_arguments(
        args,
        kwargs,
        entrypoint=function_entrypoint,
        inert_agent=metadata is not None and function_name in metadata.worker_inert_agent_functions,
    )
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
    # The tool's workspace travels as the request's workspace, which the worker confines before acting in it.
    init_overrides = dict(tool_init_overrides or {})
    base_dir = init_overrides.pop("base_dir", None)
    with _lease_worker_manager(runtime_paths, proxy_config) as worker_manager:
        route, worker_handle = _worker_route(
            runtime_paths=runtime_paths,
            runtime_config=runtime_config,
            tool_name=tool_name,
            function_name=function_name,
            worker_target=worker_target,
            worker_manager=worker_manager,
            base_dir=str(base_dir) if base_dir is not None else None,
            progress_sink=progress_sink,
        )
        # A shell call's own environment, plugin hooks' changes and a response's CLI grant, travels with each command;
        # the runner passes a variable on only when told to, so the call's pass-through patterns join the configured ones.
        call_environment = current_tool_environment() if tool_name == "shell" else ToolEnvironment()
        execution_env = call_environment.applied(execution_env or {}, runtime_paths.process_env)
        extra_env_passthrough = ",".join(filter(None, (extra_env_passthrough, *call_environment.pass_through))) or None
        agent_name = worker_target.routing_agent_name if worker_target is not None else None
        # Only an agent that opted into Agent Keys gets the broker env; a call without a configured agent never does.
        if (
            tool_name in EXECUTION_ENV_TOOL_NAMES
            and egress_broker_enabled(runtime_paths)
            and runtime_config is not None
            and agent_name is not None
            and egress_agent_scope(runtime_config, agent_name, runtime_paths) is not None
        ):
            # Deferred: the broker loads h11 and its TLS stack, which the slim tool registry must not import.
            from mindroom.egress_broker.service import execution_env_for_worker  # noqa: PLC0415

            execution_env = {
                **execution_env,
                **execution_env_for_worker(
                    runtime_paths,
                    config=runtime_config,
                    worker_target=worker_target,
                    routed_worker_key=route.worker_key,
                    call_env=execution_env,
                ),
            }
        request = ExecuteRequest(
            **dict(route),
            tool_name=tool_name,
            function_name=function_name,
            args=wire_args,
            kwargs=wire_kwargs,
            tool_config_overrides=cast("dict[str, Any]", to_json_compatible(tool_config_overrides or {})),
            tool_init_overrides=cast("dict[str, Any]", to_json_compatible(init_overrides)),
            execution_env=execution_env,
            # The call's set and removed names apply over the worker's own environment and workspace script too,
            # except where the worker's own composition changes them; a set name takes its value from execution_env.
            environment=CallEnvironment(
                set_names=sorted(name for name, value in call_environment.values.items() if value is not None),
                unset_names=sorted(call_environment.removed),
                path_prepend=list(call_environment.path_prepend),
            ),
            extra_env_passthrough=extra_env_passthrough,
        )
        result = execute_worker_proxy_request(
            config=_worker_proxy_client_config(
                proxy_config,
                runtime_paths,
                request_budget_seconds=max(
                    shell_request_budget_seconds(tool_name, function_name, wire_kwargs),
                    browser_request_budget_seconds(tool_name, function_name),
                ),
            ),
            request=request,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            worker_handle=worker_handle,
            worker_manager=worker_manager,
            client_factory=httpx.Client,
            # Leased tool settings come from the primary stores the dashboard saves them to.
            primary_built_service=functools.partial(
                primary_owns_tool_settings,
                tools=tools,
                runtime_paths=runtime_paths,
            ),
            worker_grantable_credentials=(
                runtime_config.get_worker_grantable_credentials() if runtime_config is not None else None
            ),
            cancellation=cancellation,
        )
        # Deferred: it loads the attachment store and the provider registry, which the slim tool registry must not.
        from mindroom.tool_system.media_attachments import finalize_tool_media  # noqa: PLC0415

        if tool_name == "browser_mcp" or is_media_result_envelope(result):
            decoded = decode_media_result(result)
            return finalize_tool_media(decoded) if isinstance(decoded, ToolValue) else decoded
        return result


async def _run_sync_tool_call(call: Callable[[], object]) -> object:
    """Run one call of a synchronous tool function exactly as its sync entrypoint would run, completion owners included."""
    # Deferred: the tool hook bridge loads OAuth and the job runtime, which the slim tool registry must not import.
    from mindroom.tool_system.tool_hooks import run_sync_tool_entrypoint  # noqa: PLC0415

    return await run_sync_tool_entrypoint(call)


async def _run_in_worker_proxy_executor(call: Callable[[], object]) -> object:
    """Run one blocking worker proxy call outside asyncio's default executor."""
    # Deferred: the tool hook bridge loads OAuth and the job runtime, which the slim tool registry must not import.
    from mindroom.tool_system.tool_hooks import run_async_entrypoint_blocking_call  # noqa: PLC0415

    return await run_async_entrypoint_blocking_call(call, executor=WORKER_PROXY_CALL_EXECUTOR)


def _worker_stand_in(
    function_name: str,
    local: Callable[..., object],
    *,
    tool_name: str,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager | None,
    tool_config_overrides: dict[str, object] | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    execution_env: dict[str, str] | None = None,
    extra_env_passthrough: str | None = None,
    worker_target: ResolvedWorkerTarget | None = None,
    runtime_config: Config | None = None,
    primary_placement: weakref.ref[SupportsPrimaryCallPlacement] | None = None,
    tools: ToolRegistry,
) -> Callable[..., object]:
    """Return ``local``'s stand-in sending each call to the worker, except one its toolkit keeps here.

    ``local`` is the finished entrypoint: output files, then validation, then the function. A call goes to the worker
    with its arguments as sent, where the worker's own finished function handles and validates them; a call the
    toolkit (``primary_placement``, when it places calls) keeps here runs ``local``. A synchronous function keeps the
    completion ownership its own body has; an async one keeps prompt cancellation.
    """
    # The toolkit being adapted, which the stand-in keeps for as long as its function lives.
    placement = None if primary_placement is None else primary_placement()
    local_is_async = inspect.iscoroutinefunction(local)
    run_blocking = _run_in_worker_proxy_executor if local_is_async else _run_sync_tool_call

    @functools.wraps(local)
    async def proxy_entrypoint(*args: object, **kwargs: object) -> object:
        if placement is not None and placement.runs_on_primary(
            function_name,
            inspect.signature(local).bind(*args, **_authored_arguments(kwargs)).arguments,
        ):
            if local_is_async:
                return await cast("Callable[..., Awaitable[object]]", local)(*args, **kwargs)
            return await _run_sync_tool_call(functools.partial(local, *args, **kwargs))
        cancellation = WorkerCallCancellation()
        call = functools.partial(
            _call_proxy_sync,
            function_entrypoint=local,
            runtime_paths=runtime_paths,
            tool_name=tool_name,
            function_name=function_name,
            args=args,
            kwargs=dict(kwargs),
            credentials_manager=credentials_manager,
            tool_config_overrides=tool_config_overrides,
            tool_init_overrides=tool_init_overrides,
            execution_env=execution_env,
            extra_env_passthrough=extra_env_passthrough,
            worker_target=worker_target,
            runtime_config=runtime_config,
            tools=tools,
            cancellation=cancellation,
        )
        try:
            return await run_blocking(call)
        except asyncio.CancelledError:
            # The blocking request keeps running after this await is cancelled; stop it at the worker too.
            cancellation.cancel()
            raise

    return proxy_entrypoint


def worker_stand_ins(
    tool_name: str,
    toolkit: object,
    *,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager | None,
    tool_config_overrides: dict[str, object] | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    extra_env_passthrough: str | None = None,
    worker_target: ResolvedWorkerTarget | None = None,
    runtime_config: Config | None = None,
    tools: ToolRegistry,
) -> Callable[[str, Callable[..., object]], Callable[..., object]]:
    """Return the factory of the stand-ins that send the calls of ``toolkit``, built to run in a worker, there.

    Given a function's name and finished entrypoint, the factory returns the stand-in that sends its calls there.
    ``runtime_config`` is the config the toolkit was built from; each call carries the parts runners need from it.
    ``tools`` is the registry the toolkit was built from.
    """
    return functools.partial(
        _worker_stand_in,
        tool_name=tool_name,
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        tool_config_overrides=tool_config_overrides,
        tool_init_overrides=tool_init_overrides,
        execution_env=_execution_env_payload(
            tool_name,
            runtime_paths=runtime_paths,
            extra_env_passthrough=extra_env_passthrough,
        ),
        extra_env_passthrough=extra_env_passthrough,
        worker_target=worker_target,
        runtime_config=runtime_config,
        # Weak, because the toolkit keeps this factory as long as it lives; a strong reference would keep it forever.
        primary_placement=weakref.ref(toolkit) if isinstance(toolkit, SupportsPrimaryCallPlacement) else None,
        tools=tools,
    )
