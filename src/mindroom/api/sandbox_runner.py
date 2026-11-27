"""Sandbox runner API for executing tool calls inside isolated containers."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import inspect
import io
import json
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from contextlib import nullcontext, redirect_stderr, redirect_stdout, suppress
from contextvars import copy_context
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any
from uuid import uuid4

import yaml
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.requests import HTTPConnection  # noqa: TC002 - FastAPI resolves dependency annotations.
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, ValidationError

from mindroom import constants, yaml_io
from mindroom.api import (
    sandbox_env_assembly,
    sandbox_exec,
    sandbox_execute_stream,
    sandbox_forkserver,
    sandbox_protocol,
    sandbox_request_cancellation,
    sandbox_worker_prep,
)
from mindroom.api.computer_browser_binding import select_browser_provider
from mindroom.api.worker_responses import (
    SandboxWorkerCleanupResponse,
    SandboxWorkerListResponse,
    serialize_sandbox_worker_response,
)
from mindroom.attachments.ids import normalize_attachment_id
from mindroom.background_tasks import wait_for_future_until_complete
from mindroom.browser.call_limit import browser_request_budget_seconds
from mindroom.config.main import Config, load_config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.config.yaml_includes import load_yaml_config_source
from mindroom.credentials.manager import CredentialsManager, get_runtime_credentials_manager, load_scoped_credentials
from mindroom.file_access import agent_file_access
from mindroom.logging_config import get_logger
from mindroom.media_delivery import view_agent_image
from mindroom.oauth.providers import OAuthConnectionRequired, oauth_connection_required_payload
from mindroom.runtime_env_policy import (
    CREDENTIALS_ENCRYPTION_KEY_ENV,
    KUBERNETES_POD_LIMIT_ENV_NAMES,
    SANDBOX_RUNTIME_ENV_BY_KEY,
    SANDBOX_STARTUP_MANIFEST_PATH_ENV,
    sandbox_runner_startup_process_env,
    strip_authenticated_proxy_env,
)
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.script_runs.models import script_run_id_from_worker_key
from mindroom.shell import supervisor as shell_supervisor
from mindroom.shell.background_scope import ShellBackgroundScope, bind_shell_background_scope
from mindroom.shell.execution import (
    CommandStdin,
    kill_all_records,
    kill_namespace_records,
    shell_request_budget_seconds,
    stream_command,
)
from mindroom.terminal.session import SupportsTerminalLaunch, TerminalError, TerminalLaunch
from mindroom.timing import elapsed_ms_between, elapsed_ms_since
from mindroom.tool_jobs.control import JobControl, job_control_context, job_owns_execution
from mindroom.tool_system.catalog import (
    ToolConfigOverrideError,
    ToolInitOverrideError,
    ToolManagedInitArg,
    ToolMetadata,
    ToolValidationInfo,
    deserialize_tool_validation_snapshot,
    get_tool_by_name,
    safe_tool_init_override_fields,
    sanitize_tool_init_overrides,
    validate_authored_tool_entry_overrides,
)
from mindroom.tool_system.dedicated_threads import JOB_CALL_EXECUTOR
from mindroom.tool_system.media_transport import encode_media_result
from mindroom.tool_system.output_files import (
    OUTPUT_PATH_ARGUMENT,
    ToolOutputFilePolicy,
    normalize_output_path_argument,
    validate_output_path,
    validate_output_path_syntax,
    write_bytes_to_output_path,
)
from mindroom.tool_system.plugins import preload_plugins, refresh_plugins, worker_preload_plugin_paths
from mindroom.tool_system.progress import report_tool_progress, structured_tool_result, tool_progress_reporter
from mindroom.tool_system.registry import ToolRegistry, builtin_tool_registry
from mindroom.tool_system.sandbox_proxy import decode_attachment_save_bytes, sandbox_proxy_config, to_json_compatible
from mindroom.tool_system.tool_environment import ToolEnvironment, collected_tool_environment
from mindroom.tool_system.toolkits import requires_connect, toolkit_function
from mindroom.tool_system.worker_media import serialize_worker_tool_result
from mindroom.tool_system.worker_proxy_client import EXECUTE_KEEPALIVE_SECONDS
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    bind_detached_execution_identity,
    build_worker_target_from_runtime_env,
    parse_tool_execution_identity_payload,
    resolved_worker_key_scope,
    serialize_tool_execution_identity,
    visible_workspace_roots,
)
from mindroom.trafilatura_call_limit import trafilatura_request_budget_seconds
from mindroom.worker_computer.runtime import WorkerComputerRuntime
from mindroom.worker_computer.worker_browser import WorkerBrowserRuntime
from mindroom.workers.backends.local import (
    ensure_local_worker_state_locked,
    get_local_worker_manager,
    local_worker_state_paths_for_root,
)
from mindroom.workers.protocol import (
    WORKER_PROTOCOL_VERSION,
    CancelRequest,
    CancelResponse,
    ExecuteRequest,
    ExecuteResponse,
    KillShellScopeRequest,
    KillShellScopeResponse,
    LeaseRequest,
    LeaseResponse,
    SaveAttachmentRequest,
    SaveAttachmentResponse,
    StdinRequest,
    StdinResponse,
    ViewFileRequest,
    ViewFileResponse,
    WorkerFailure,
    WorkerRoute,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from pydantic.main import IncEx

    from mindroom.config.models import FileAccess
    from mindroom.constants import RuntimePaths
    from mindroom.shell.execution import ProcessRecord
    from mindroom.tool_system.authoring import Toolkit
    from mindroom.worker_computer.protocol import BrowserSession
    from mindroom.workers.backends.local import LocalWorkerStatePaths

logger = get_logger(__name__)

_SUBPROCESS_WORKER_ARG = "--sandbox-subprocess-worker"
# A spawned request child writes its progress lines to this socket descriptor.
_PROGRESS_FD_ARG = "--progress-fd"
# How long a worker's eager setup may run before it counts as failed and the first command prepares what is missing.
_EAGER_SETUP_BUDGET_SECONDS = 180.0
# How long a template may spend importing the worker's opt-in plugins, and an eager setup creating the venv.
_PRELOAD_BUDGET_SECONDS = 60.0
_EAGER_VENV_TIMEOUT_SECONDS = 120.0
# How often a spawn-per-call wait looks up from its child to notice a stop or its deadline.
_SPAWN_POLL_SECONDS = 1.0


def _startup_manifest_path_from_env() -> Path:
    raw_path = os.environ.get(SANDBOX_STARTUP_MANIFEST_PATH_ENV, "").strip()
    if not raw_path:
        msg = f"{SANDBOX_STARTUP_MANIFEST_PATH_ENV} must be set for sandbox runner startup."
        raise RuntimeError(msg)
    return Path(raw_path).expanduser()


def _startup_manifest_from_env() -> dict[str, object]:
    payload = json.loads(_startup_manifest_path_from_env().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        msg = f"{SANDBOX_STARTUP_MANIFEST_PATH_ENV} must point to a JSON object."
        raise TypeError(msg)
    return payload


@dataclass(frozen=True, slots=True)
class _PinnedSandboxMount:
    """One sandbox mount directory and inode pinned in the read-only startup manifest."""

    path: Path
    ino: int


def _parse_pinned_sandbox_mount(raw: object) -> _PinnedSandboxMount | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        msg = "Startup manifest sandbox_mount must be a JSON object."
        raise TypeError(msg)
    raw_path = raw.get("path")
    raw_ino = raw.get("ino")
    if not isinstance(raw_path, str) or not raw_path.strip() or not Path(raw_path).is_absolute():
        msg = "Startup manifest sandbox_mount path must be an absolute path string."
        raise ValueError(msg)
    if not isinstance(raw_ino, int) or isinstance(raw_ino, bool) or raw_ino < 0:
        msg = "Startup manifest sandbox_mount ino must be a non-negative integer."
        raise ValueError(msg)
    return _PinnedSandboxMount(path=Path(raw_path), ino=raw_ino)


def startup_sandbox_mount_from_env() -> _PinnedSandboxMount | None:
    """Read the optional pinned sandbox mount from the startup manifest when configured."""
    if not os.environ.get(SANDBOX_STARTUP_MANIFEST_PATH_ENV, "").strip():
        return None
    return _parse_pinned_sandbox_mount(_startup_manifest_from_env().get(constants.STARTUP_MANIFEST_SANDBOX_MOUNT))


def _verify_pinned_sandbox_mount(pinned: _PinnedSandboxMount) -> None:
    """Verify that a pinned sandbox mount is still the exact real directory recorded at startup."""
    try:
        st = os.lstat(pinned.path)
    except OSError as exc:
        msg = f"Pinned sandbox mount '{pinned.path}' is unavailable: {exc}"
        raise RuntimeError(msg) from exc
    if not stat.S_ISDIR(st.st_mode):
        msg = f"Pinned sandbox mount '{pinned.path}' is not a real directory."
        raise RuntimeError(msg)
    if st.st_ino != pinned.ino:
        msg = f"Pinned sandbox mount '{pinned.path}' inode mismatch (expected {pinned.ino}, found {st.st_ino})."
        raise RuntimeError(msg)


def _startup_runtime_payload_from_env() -> tuple[RuntimePaths, dict[str, ToolValidationInfo]]:
    """Read the startup runtime and primary tool validation snapshot once, from the startup manifest."""
    startup_runtime_paths, tool_validation_snapshot = constants.deserialize_startup_manifest(
        _startup_manifest_from_env(),
    )
    return startup_runtime_paths, deserialize_tool_validation_snapshot(tool_validation_snapshot)


def _committed_startup_runtime_paths(startup_runtime_paths: RuntimePaths) -> RuntimePaths:
    """Commit the startup runtime payload together with this runner's own startup env."""
    # Runners never use the credential encryption key, so scrub one passed against the docs before tool code runs.
    _startup_secret_from_env(CREDENTIALS_ENCRYPTION_KEY_ENV)
    process_env = dict(startup_runtime_paths.process_env)
    process_env.pop(constants.CONTROL_STATE_PATH_ENV, None)
    # Every runner is in runner mode, which keeps the primary's environment-declared credentials out of it.
    runner_mode = {SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]: "true"}
    if sandbox_exec.runner_uses_dedicated_worker(startup_runtime_paths):
        return constants.RuntimePaths(
            config_path=startup_runtime_paths.config_path,
            config_dir=startup_runtime_paths.config_dir,
            env_path=startup_runtime_paths.env_path,
            storage_root=startup_runtime_paths.storage_root,
            process_env=MappingProxyType({**process_env, **runner_mode}),
            env_file_values=startup_runtime_paths.env_file_values,
        )
    process_env.update(sandbox_runner_startup_process_env(os.environ))
    config_path = (
        Path(process_env["MINDROOM_CONFIG_PATH"])
        if process_env.get("MINDROOM_CONFIG_PATH")
        else startup_runtime_paths.config_path
    )
    storage_path = (
        Path(process_env["MINDROOM_STORAGE_PATH"])
        if process_env.get("MINDROOM_STORAGE_PATH")
        else startup_runtime_paths.storage_root
    )
    resolved_runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=storage_path,
        process_env=process_env,
    )
    resolved_process_env = {**resolved_runtime_paths.process_env, **runner_mode}
    resolved_process_env.pop(constants.CONTROL_STATE_PATH_ENV, None)
    env_file_values = dict(startup_runtime_paths.env_file_values)
    env_file_values.update(resolved_runtime_paths.env_file_values)
    env_file_values.pop(constants.CONTROL_STATE_PATH_ENV, None)
    return constants.RuntimePaths(
        config_path=resolved_runtime_paths.config_path,
        config_dir=resolved_runtime_paths.config_dir,
        env_path=resolved_runtime_paths.env_path,
        storage_root=resolved_runtime_paths.storage_root,
        control_state_root=None,
        process_env=MappingProxyType(resolved_process_env),
        env_file_values=MappingProxyType(env_file_values),
    )


def startup_runner_token_from_env() -> str | None:
    """Read and remove the runner auth token from process env after startup."""
    return _startup_secret_from_env(SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"])


def _startup_secret_from_env(name: str) -> str | None:
    """Read and remove one startup secret from process env."""
    if name not in os.environ:
        return None
    raw_secret = os.environ.get(name, "")
    raw_process_entry = _process_environment_entry(name)
    if raw_process_entry is not None:
        _wipe_process_environment_entry(*raw_process_entry)
    os.environ.pop(name, None)
    return raw_secret.strip() or None


def _process_environment_entry(name: str) -> tuple[int, int] | None:
    """Return the raw process environment entry address and size for an env var."""
    prefix = os.fsencode(f"{name}=")
    try:
        envp = ctypes.POINTER(ctypes.c_void_p).in_dll(ctypes.CDLL(None), "environ")
    except (AttributeError, OSError, ValueError):
        return None

    index = 0
    while envp[index]:
        address = int(envp[index])
        entry = ctypes.string_at(address)
        if entry.startswith(prefix):
            return address, len(entry)
        index += 1
    return None


def _wipe_process_environment_entry(address: int, size: int) -> None:
    """Overwrite a raw process environment entry exposed by /proc/<pid>/environ."""
    if size > 0:
        ctypes.memset(address, 0, size)


def _runtime_config_or_empty(
    runtime_paths: RuntimePaths,
    *,
    tool_validation_snapshot: Mapping[str, ToolValidationInfo] | None = None,
) -> Config:
    """Return the runtime config visible inside one sandbox runner."""
    if runtime_paths.config_path.exists():
        if not sandbox_exec.runner_uses_dedicated_worker(runtime_paths):
            return load_config(runtime_paths)
        return _dedicated_worker_runtime_config_or_empty(runtime_paths, tool_validation_snapshot)
    return Config.validate_with_runtime({}, runtime_paths)


def _dedicated_worker_runtime_config_or_empty(
    runtime_paths: RuntimePaths,
    tool_validation_snapshot: Mapping[str, ToolValidationInfo] | None,
) -> Config:
    """Return dedicated-worker config, tolerating plugins unavailable in that worker image."""
    # The authored config may be split across !include files; a plain
    # yaml.safe_load crashes the worker on the include tags.
    data, _config_source_files = load_yaml_config_source(runtime_paths.config_path)

    if not tool_validation_snapshot:
        return load_config(runtime_paths)
    return _primary_validated_config(data, runtime_paths, log_skipped_plugins=True)


def _primary_validated_config(data: object, runtime_paths: RuntimePaths, *, log_skipped_plugins: bool) -> Config:
    """Validate config data the primary runtime already validated, as seen from this runner.

    Runners only need the authored config shape plus the subset of plugin
    entries that actually exist in this runtime filesystem. The primary runtime
    is authoritative for full authored tool validation; runners validate the
    requested tool at execution time with their local registry.
    """
    config = Config.model_validate(data, context={"runtime_paths": runtime_paths})
    return _config_with_available_plugins(config, runtime_paths, log_skipped_plugins=log_skipped_plugins)


def _config_with_available_plugins(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    log_skipped_plugins: bool,
) -> Config:
    """Return one config snapshot filtered to plugin entries visible in this runtime."""
    if not config.plugins:
        return config

    from mindroom.tool_system import plugin_imports  # noqa: PLC0415

    available_plugins = []
    skipped_plugin_paths: list[str] = []
    for plugin_entry in config.plugins:
        if not plugin_entry.enabled:
            available_plugins.append(plugin_entry)
            continue

        try:
            plugin_root = plugin_imports._resolve_plugin_root(plugin_entry.path, runtime_paths)
        except Exception:
            skipped_plugin_paths.append(plugin_entry.path)
            continue

        if plugin_root.exists() and plugin_root.is_dir():
            available_plugins.append(plugin_entry)
        else:
            skipped_plugin_paths.append(plugin_entry.path)

    if not skipped_plugin_paths:
        return config

    if log_skipped_plugins:
        logger.info(
            "sandbox_runner_skipping_unavailable_plugins",
            plugin_paths=sorted(skipped_plugin_paths),
        )
    return config.model_copy(update={"plugins": available_plugins})


def load_config_from_startup_runtime() -> tuple[RuntimePaths, Config]:
    """Read the sandbox runner runtime context from explicit startup payload.

    The startup manifest is read only while the runner starts. The runner keeps the result in
    memory for its lifetime, so the primary rewriting the manifest for a replacement
    pod changes nothing in a runner that is still serving.
    """
    startup_runtime_paths, tool_validation_snapshot = _startup_runtime_payload_from_env()
    runtime_paths = _committed_startup_runtime_paths(startup_runtime_paths)
    return runtime_paths, _runtime_config_or_empty(runtime_paths, tool_validation_snapshot=tool_validation_snapshot)


def initialize_sandbox_runner_app(
    api_app: FastAPI,
    runtime_paths: RuntimePaths,
    *,
    config: Config | None = None,
    runner_token: str | None = None,
    sandbox_mount: _PinnedSandboxMount | None = None,
) -> None:
    """Attach one explicit runtime context to a sandbox-runner app instance."""
    if sandbox_mount is not None:
        _verify_pinned_sandbox_mount(sandbox_mount)
    committed_config = config or _runtime_config_or_empty(runtime_paths)
    context = _SandboxRunnerContext(
        runtime_paths=runtime_paths,
        config=committed_config,
        runner_token=runner_token or sandbox_proxy_config(runtime_paths).proxy_token,
        sandbox_mount=sandbox_mount,
    )
    # Requests load their own config's plugins; loading the startup config's now reports a broken one at startup.
    refresh_plugins(committed_config, runtime_paths)
    api_app.state.sandbox_runner_context = context


def start_eager_worker_setup(app: FastAPI) -> asyncio.Task[None] | None:
    """Start preparing this dedicated worker in the background when its startup manifest asks for that.

    Only the Kubernetes backend asks, and never for a script worker. ``/healthz`` answers meanwhile and reports the
    setup's state, and a request prepares whatever a setup that failed or outlasted its budget left undone.
    """
    app.state.worker_setup = None
    runtime_paths = app_runtime_paths(app)
    worker_key = sandbox_exec.runner_dedicated_worker_key(runtime_paths)
    if (
        worker_key is None
        or script_run_id_from_worker_key(worker_key) is not None
        or not sandbox_exec.runner_uses_subprocess(runtime_paths)
        or not os.environ.get(SANDBOX_STARTUP_MANIFEST_PATH_ENV, "").strip()
    ):
        return None
    plugin_paths = _startup_manifest_from_env().get(constants.STARTUP_MANIFEST_EAGER_SETUP_PLUGINS)
    if not isinstance(plugin_paths, list):
        return None
    if plugin_paths:
        preload = {
            "config_path": str(runtime_paths.config_path),
            "storage_root": str(runtime_paths.storage_root),
            "plugins": plugin_paths,
            "budget_seconds": _PRELOAD_BUDGET_SECONDS,
        }
        sandbox_forkserver.start_preloading_forkserver(json.dumps(preload))
    app.state.worker_setup = "preparing"
    return asyncio.create_task(_eager_worker_setup(app, runtime_paths, tuple(plugin_paths)))


async def _eager_worker_setup(app: FastAPI, runtime_paths: RuntimePaths, preloaded: tuple[str, ...] = ()) -> None:
    """Create the worker's venv, then start its shell supervisor and forkserver template together.

    Requests run in the template that preloaded the plugins ``preloaded`` only once all of this succeeded.
    """
    started_at = time.monotonic()
    try:
        async with asyncio.timeout(_EAGER_SETUP_BUDGET_SECONDS):
            paths = local_worker_state_paths_for_root(sandbox_exec.runner_dedicated_worker_root(runtime_paths))
            await asyncio.to_thread(
                ensure_local_worker_state_locked,
                paths,
                venv_timeout_seconds=_EAGER_VENV_TIMEOUT_SECONDS,
            )
            steps = [asyncio.to_thread(shell_supervisor.ensure_shell_supervisor)]
            if sandbox_exec.runner_uses_forkserver(runtime_paths) and sandbox_forkserver.forkserver_supported():
                budget_seconds = _EAGER_SETUP_BUDGET_SECONDS - (time.monotonic() - started_at)
                steps.append(asyncio.to_thread(_warm_forkserver_template, paths, budget_seconds))
            await asyncio.gather(*steps)
    except asyncio.CancelledError:  # The worker shuts down, and must not wait for a preload that is still importing.
        sandbox_forkserver.abandon_preload(None)
        raise
    except Exception as exc:  # Whatever failed, the first request prepares it and reports its error.
        app.state.worker_setup = "failed"
        error = str(exc) or type(exc).__name__
        logger.warning("sandbox_worker_eager_setup_failed", error=error)
        sandbox_forkserver.abandon_preload(f"The worker's setup failed: {error}")
    else:
        if (preloading := sandbox_forkserver.preloading_forkserver()) is not None:
            preloading.serves = preloaded
        app.state.worker_setup = "ready"
        logger.debug("sandbox_worker_eager_setup_ready", setup_ms=elapsed_ms_since(started_at))


def _warm_forkserver_template(paths: LocalWorkerStatePaths, budget_seconds: float) -> None:
    """Start the template this worker's calls fork from, giving a preload up that fails or outlasts the budget.

    The plain template then starts at once, and keeps importing for the first request when the budget is used up.
    """
    python_executable, template_env, _ = sandbox_exec.resolve_subprocess_worker_context(paths)
    deadline = time.monotonic() + budget_seconds
    if (preloading := sandbox_forkserver.preloading_forkserver()) is not None:
        try:
            preloading.warm(python_executable, template_env, budget_seconds)
        except sandbox_forkserver.ForkserverError as exc:
            if sandbox_forkserver.preloading_forkserver() is not preloading:
                return  # The setup's end or the worker's shutdown gave the preload up, and this setup with it.
            sandbox_forkserver.abandon_preload(str(exc) or "The preload outlasted the worker's setup budget.")
        else:
            return
    remaining_seconds = max(deadline - time.monotonic(), 1.0)
    sandbox_forkserver.get_sandbox_forkserver().warm(python_executable, template_env, remaining_seconds)


def _freeze_private_agent_names(private_agent_names: list[str] | None) -> frozenset[str] | None:
    """Freeze one optional private-agent visibility snapshot."""
    if private_agent_names is None:
        return None
    return frozenset(private_agent_names)


def _filter_runtime_tool_init_overrides(
    tool_name: str,
    runtime_overrides: dict[str, object],
    tools: ToolRegistry,
) -> dict[str, object]:
    """Keep only the safe runtime init overrides declared by the target tool."""
    safe_fields = safe_tool_init_override_fields(tool_name, tool_metadata=tools.entries)
    safe_overrides = {name: value for name, value in runtime_overrides.items() if name in safe_fields}
    return sanitize_tool_init_overrides(tool_name, safe_overrides, tool_metadata=tools.entries) or {}


def _request_runtime_overrides(call: _ExecuteCall) -> dict[str, object]:
    """Return runtime overrides for one runner-side tool rebuild: the route's workspace and shell pass-through."""
    request, route = call.request, call.route
    runtime_overrides: dict[str, object] = {"base_dir": str(route.workspace)} if route.workspace is not None else {}
    if request.sandbox or request.tool_name != "shell" or request.extra_env_passthrough is None:
        return runtime_overrides

    # Pre-resolve passthrough patterns against only the client's env snapshot
    # to prevent cross-runtime secret leakage via glob patterns that match
    # runner-only env vars.
    source_env = request.execution_env or (
        route.runtime_paths.process_env
        if sandbox_exec.runner_uses_dedicated_worker(route.runtime_paths)
        else {**os.environ, **route.runtime_paths.process_env}
    )
    resolved = constants.shell_extra_env_values(
        extra_env_passthrough=request.extra_env_passthrough,
        process_env=source_env,
    )
    if not resolved:
        return runtime_overrides
    return {**runtime_overrides, "extra_env_passthrough": ",".join(resolved)}


def _subprocess_credential_overrides(call: _ExecuteCall) -> dict[str, Any]:
    """Preload persisted execution-tool config before serializing a keyless child runtime."""
    request, route = call.request, call.route
    if request.tool_name not in sandbox_exec.EXECUTION_ENV_TOOL_NAMES:
        return call.credential_overrides
    if sandbox_exec.runner_uses_dedicated_worker(route.runtime_paths):
        return call.credential_overrides
    persisted_credentials = load_scoped_credentials(
        request.tool_name,
        credentials_manager=get_runtime_credentials_manager(route.runtime_paths),
        worker_target=build_worker_target_from_runtime_env(
            request.worker_scope,
            request.routing_agent_name,
            runtime_paths=route.runtime_paths,
            execution_identity=route.identity,
            private_agent_names=_freeze_private_agent_names(request.private_agent_names),
        ),
        allowed_shared_services=(
            route.config.get_worker_grantable_credentials() if request.worker_scope is not None else None
        ),
    )
    if not persisted_credentials:
        return call.credential_overrides
    metadata = call.tools.entries[request.tool_name]
    config_field_names = {config_field.name for config_field in metadata.config_fields or ()}
    persisted_config = {name: value for name, value in persisted_credentials.items() if name in config_field_names}
    if not persisted_config:
        return call.credential_overrides
    return {**persisted_config, **call.credential_overrides}


class PreparedSandboxRunnerExecuteRequest(BaseModel):
    """Prepared sandbox request shared by in-process and subprocess execution."""

    tool_name: str
    function_name: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    worker_scope: WorkerScope | None = None
    routing_agent_name: str | None = None
    execution_identity: dict[str, Any] | None = None
    private_agent_names: list[str] | None = None
    credential_overrides: dict[str, Any] = Field(default_factory=dict)
    tool_config_overrides: dict[str, Any] = Field(default_factory=dict)
    tool_init_overrides: dict[str, Any] = Field(default_factory=dict)
    # The call's own values (None for a removal), which its shell applies over the worker's environment, and the
    # directories it puts first on the command's PATH.
    call_environment: dict[str, str | None] = Field(default_factory=dict)
    call_path_prepend: list[str] = Field(default_factory=list)
    runtime_overrides: dict[str, Any] = Field(default_factory=dict)
    tool_output_workspace_root: str | None = None
    run_as_job: bool = False
    structured_result: bool = False
    sandbox: bool = False
    shell_scope_id: str | None = None


@dataclass(frozen=True)
class _PreparedRoute:
    """What one routed request acts on: its config, its caller, its prepared worker and its one workspace.

    ``workspace`` is where the request acts: the HOME and ``.mindroom/worker-env.sh`` of shell, the
    subprocess cwd, the tool output root and the tools' ``base_dir``, the view-file and save-attachment root, and
    the browser's workspace.
    """

    runtime_paths: RuntimePaths
    config: Config
    worker_key: str | None
    identity: ToolExecutionIdentity | None
    worker: sandbox_worker_prep.PreparedWorkerRequest | None
    workspace: Path | None


@dataclass(frozen=True)
class _ExecuteCall:
    """One validated execute request, its prepared route, the tools its config can build, and its lease's credentials."""

    request: ExecuteRequest
    route: _PreparedRoute
    tools: ToolRegistry
    credential_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _SandboxRunnerContext:
    runtime_paths: RuntimePaths
    config: Config
    runner_token: str | None
    sandbox_mount: _PinnedSandboxMount | None = None


@dataclass(frozen=True)
class _PreparedSandboxRequestContext:
    request: PreparedSandboxRunnerExecuteRequest
    runtime_paths: RuntimePaths
    execution_env: dict[str, str]
    route: _PreparedRoute


@dataclass(frozen=True)
class _PreparedSandboxSubprocessContext:
    python_executable: str | None
    subprocess_env: dict[str, str] | None
    subprocess_cwd: str | None
    template_env: dict[str, str] | None


def _app_context(app: FastAPI) -> _SandboxRunnerContext:
    try:
        context = app.state.sandbox_runner_context
    except AttributeError:
        context = None
    if not isinstance(context, _SandboxRunnerContext):
        msg = "Sandbox runner context is not initialized"
        raise TypeError(msg)
    return context


def app_runtime_paths(app: FastAPI) -> RuntimePaths:
    """Return sandbox runner runtime paths stored on the FastAPI app."""
    return _app_context(app).runtime_paths


def app_runtime_config(app: FastAPI) -> Config:
    """Return sandbox runner config stored on the FastAPI app."""
    return _app_context(app).config


def app_sandbox_mount(app: FastAPI) -> _PinnedSandboxMount | None:
    """Return the pinned sandbox mount stored on the FastAPI app, if any."""
    return _app_context(app).sandbox_mount


def request_runtime_config(app: FastAPI, config_snapshot: dict[str, Any] | None) -> Config:
    """Return the config one request runs under: the primary's live snapshot when sent, else the startup config.

    Deployments give runners no config file from the primary, so agents exist only
    in the allowlisted snapshot the authenticated primary sends.
    Plugins missing from this runner are skipped silently here, because every
    request carries the snapshot and would otherwise repeat the same log line.
    """
    context = _app_context(app)
    if config_snapshot is None:
        return context.config
    try:
        return _primary_validated_config(config_snapshot, context.runtime_paths, log_skipped_plugins=False)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid config_snapshot: {exc}") from exc


def _protocol_failure(protocol: int) -> WorkerFailure | None:
    """Refuse a request from a primary that speaks another worker protocol."""
    if protocol == WORKER_PROTOCOL_VERSION:
        return None
    return WorkerFailure(
        error=(
            f"The primary speaks worker protocol {protocol}, this worker {WORKER_PROTOCOL_VERSION}; "
            "run the same MindRoom release on both."
        ),
        failure_kind="worker",
    )


async def _request_body_protocol_failure(request: Request) -> WorkerFailure | None:
    """Return the protocol refusal of a request body that names another worker protocol."""
    try:
        body = await request.json()
    except ValueError:
        return None
    protocol = body.get("protocol") if isinstance(body, dict) else None
    if not isinstance(protocol, int) or isinstance(protocol, bool):
        return None
    return _protocol_failure(protocol)


def prepare_route(app: FastAPI, route: WorkerRoute) -> _PreparedRoute | WorkerFailure:
    """Prepare the config, caller, worker and workspace one routed request acts on, or say why it cannot run."""
    if (failure := _protocol_failure(route.protocol)) is not None:
        return failure
    context = _app_context(app)
    return _prepare_route_with_config(
        route,
        runtime_paths=context.runtime_paths,
        config=request_runtime_config(app, route.config_snapshot),
        runner_token=context.runner_token,
        sandbox_mount=context.sandbox_mount,
    )


async def _prepare_route_beside_setup(app: FastAPI, route: WorkerRoute) -> _PreparedRoute | WorkerFailure:
    """Prepare a route as ``prepare_route`` does, without holding up what else the worker answers.

    A worker that is still preparing itself may hold the lock this waits for, so the wait then happens in a thread.
    """
    if getattr(app.state, "worker_setup", None) == "preparing":
        return await asyncio.to_thread(prepare_route, app, route)
    return prepare_route(app, route)


def _verify_sandbox_mount_preflight(
    route: WorkerRoute,
    *,
    runtime_paths: RuntimePaths,
    sandbox_mount: _PinnedSandboxMount | None,
) -> WorkerFailure | None:
    if (
        isinstance(route, ExecuteRequest)
        and route.sandbox
        and route.base_dir is not None
        and sandbox_mount is None
        and sandbox_exec.runner_uses_dedicated_worker(runtime_paths)
    ):
        return WorkerFailure(
            error="Sandboxed mount_dir request requires a pinned sandbox_mount in the dedicated worker startup manifest.",
            failure_kind="worker",
        )
    if sandbox_mount is not None:
        try:
            _verify_pinned_sandbox_mount(sandbox_mount)
        except RuntimeError as exc:
            return WorkerFailure(error=str(exc), failure_kind="worker")
    return None


def _prepare_worker_for_route(
    route: WorkerRoute,
    *,
    worker_key: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    runner_token: str | None,
    sandbox_mount: _PinnedSandboxMount | None,
) -> sandbox_worker_prep.PreparedWorkerRequest | WorkerFailure | None:
    preflight_failure = _verify_sandbox_mount_preflight(
        route,
        runtime_paths=runtime_paths,
        sandbox_mount=sandbox_mount,
    )
    if preflight_failure is not None:
        return preflight_failure
    worker: sandbox_worker_prep.PreparedWorkerRequest | None = None
    if worker_key is not None:
        try:
            worker = sandbox_worker_prep.prepare_worker_request(
                worker_key=worker_key,
                base_dir=route.base_dir,
                runtime_paths=runtime_paths,
                agent_policies=config.get_agent_policies(),
                private_agent_names=_freeze_private_agent_names(route.private_agent_names),
                runner_token=runner_token,
            )
        except sandbox_worker_prep.WorkerRequestPreparationError as exc:
            return WorkerFailure(error=str(exc), failure_kind=exc.failure_kind)
    elif (
        route.base_dir is not None
        and route.routing_agent_name is None
        and not Path(route.base_dir).expanduser().is_absolute()
    ):
        return WorkerFailure(error="base_dir must be absolute in a request without a worker.", failure_kind="tool")
    if sandbox_mount is not None:
        if worker is None:
            return WorkerFailure(error="Pinned sandbox mount requires a worker route.", failure_kind="worker")
        if not worker.base_dir.resolve().is_relative_to(sandbox_mount.path.resolve()):
            return WorkerFailure(
                error=f"Worker base_dir '{worker.base_dir}' escapes pinned sandbox mount '{sandbox_mount.path}'.",
                failure_kind="worker",
            )
    return worker


def _prepare_route_with_config(
    route: WorkerRoute,
    *,
    runtime_paths: RuntimePaths,
    config: Config,
    runner_token: str | None,
    sandbox_mount: _PinnedSandboxMount | None = None,
) -> _PreparedRoute | WorkerFailure:
    worker_key = sandbox_worker_prep.normalize_request_worker_key(route.worker_key, runtime_paths)
    try:
        identity = (
            parse_tool_execution_identity_payload(route.execution_identity)
            if route.execution_identity is not None
            else None
        )
    except TypeError as exc:
        return WorkerFailure(error=str(exc), failure_kind="tool")
    worker = _prepare_worker_for_route(
        route,
        worker_key=worker_key,
        runtime_paths=runtime_paths,
        config=config,
        runner_token=runner_token,
        sandbox_mount=sandbox_mount,
    )
    if isinstance(worker, WorkerFailure):
        return worker
    try:
        workspace = _route_workspace(route, worker, runtime_paths=runtime_paths, config=config, identity=identity)
    except OSError as exc:
        # A dedicated worker cannot create a workspace its pod does not mount; the next ensure mounts it.
        return WorkerFailure(
            error=f"Agent workspace is not mounted in this worker yet; retry the call: {exc}",
            failure_kind="tool",
        )
    except ValueError as exc:
        return WorkerFailure(error=f"This worker cannot resolve the agent's workspace: {exc}", failure_kind="tool")
    return _PreparedRoute(
        runtime_paths=runtime_paths,
        config=config,
        worker_key=worker_key,
        identity=identity,
        worker=worker,
        workspace=workspace,
    )


def _route_workspace(
    route: WorkerRoute,
    worker: sandbox_worker_prep.PreparedWorkerRequest | None,
    *,
    runtime_paths: RuntimePaths,
    config: Config,
    identity: ToolExecutionIdentity | None,
) -> Path | None:
    """Return the one workspace a request acts in, creating it.

    A worker request acts in its confined ``base_dir`` when it names one, else in its routing agent's workspace,
    else in the worker's default workspace. A request without a worker acts in its routing agent's workspace, else
    in its absolute ``base_dir``.
    """
    if (
        route.routing_agent_name is not None
        and (worker is None or route.base_dir is None)
        and not (isinstance(route, ExecuteRequest) and route.sandbox and worker is not None)
    ):
        return resolve_agent_runtime(
            route.routing_agent_name,
            config,
            _runtime_paths_for_runner_agent_paths(runtime_paths),
            execution_identity=identity,
            create=True,
        ).workspace.root
    if worker is not None:
        workspace = worker.base_dir
    elif route.base_dir is not None:
        workspace = Path(route.base_dir).expanduser()
    else:
        return None
    if not workspace.is_dir():
        workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def resolve_script_state_workspace(
    app: FastAPI,
    config: Config,
    *,
    state_scope_worker_key: str,
    agent_name: str,
    private_agent_names: frozenset[str],
) -> Path:
    """Resolve the canonical agent workspace mounted into one script worker."""
    runtime_paths = app_runtime_paths(app)
    agent_config = config.agents.get(agent_name)
    if agent_config is None:
        msg = "Script state scope does not resolve an agent workspace."
        raise ValueError(msg)
    if (agent_name in private_agent_names) != (agent_config.private is not None):
        msg = "Script state scope does not match private-agent visibility."
        raise ValueError(msg)
    workspaces = visible_workspace_roots(
        sandbox_exec.runner_storage_root(runtime_paths),
        state_scope_worker_key,
        config.get_agent_policies(),
        private_agent_names=private_agent_names,
    )
    if len(workspaces) != 1:
        msg = "Script state scope does not resolve one agent workspace."
        raise ValueError(msg)
    # The workspace is the worker's mount; creating it here would hide script
    # output in this container instead of the canonical workspace.
    workspace = workspaces[0]
    if not workspace.is_dir() or workspace.is_symlink():
        msg = "Script workspace is not mounted in this worker."
        raise ValueError(msg)
    return workspace


def app_runner_token(app: FastAPI) -> str | None:
    """Return the configured sandbox runner token for the FastAPI app."""
    runner_token = _app_context(app).runner_token
    if runner_token is None:
        return None
    if not isinstance(runner_token, str):
        msg = "Sandbox runner token is not initialized"
        raise TypeError(msg)
    return runner_token


async def validate_runner_token(
    request: HTTPConnection,
    x_mindroom_sandbox_token: Annotated[str | None, Header()] = None,
) -> None:
    """Reject requests and WebSockets that do not carry the configured runner token."""
    proxy_token = app_runner_token(request.app)
    # An empty token would match a missing header, so treat it as unconfigured.
    if not proxy_token:
        raise HTTPException(status_code=503, detail="Sandbox runner token is not configured.")
    # Compare bytes: comparing text raises on a header that is not ASCII.
    if not secrets.compare_digest((x_mindroom_sandbox_token or "").encode(), proxy_token.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized sandbox runner request")


def _worker_failure(error: Exception) -> WorkerFailure:
    """Answer one error a request raised, blaming the call for a request the runner refused and else the worker."""
    if isinstance(error, RequestValidationError):
        details = "; ".join(f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors())
        return WorkerFailure(error=f"Invalid request: {details}", failure_kind="tool")
    if isinstance(error, HTTPException) and error.status_code < 500:
        return WorkerFailure(error=str(error.detail), failure_kind="tool")
    if isinstance(error, sandbox_worker_prep.WorkerRequestPreparationError):
        return WorkerFailure(error=str(error), failure_kind=error.failure_kind)
    logger.exception("sandbox_execute_failed")
    return WorkerFailure(error=f"Sandbox execution failed: {type(error).__name__}: {error}", failure_kind="worker")


class _WorkerFailureRoute(APIRoute):
    """Answer a request the runner refuses with a typed failure body, keeping its status code."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Another release's request usually fails on its shape first, so its protocol says why.
                if (failure := await _request_body_protocol_failure(request)) is not None:
                    return JSONResponse(failure.model_dump(mode="json"))
                return JSONResponse(_worker_failure(exc).model_dump(mode="json"), status_code=422)
            except HTTPException as exc:
                # Authentication failures stay untyped, like the token middleware's, so they count against the worker.
                if exc.status_code >= 500 or exc.status_code == 401:
                    raise
                return JSONResponse(_worker_failure(exc).model_dump(mode="json"), status_code=exc.status_code)

        return handle


router = APIRouter(
    prefix="/api/sandbox-runner",
    tags=["sandbox-runner"],
    dependencies=[Depends(validate_runner_token)],
    route_class=_WorkerFailureRoute,
)


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call_entrypoint(entrypoint: Callable[..., object], args: list[Any], kwargs: dict[str, Any]) -> object:
    if inspect.iscoroutinefunction(entrypoint):
        return await entrypoint(*args, **kwargs)
    # A blocking function on the loop would stop keep-alives and every other call in this worker.
    executor = JOB_CALL_EXECUTOR if job_owns_execution() else None
    work = asyncio.get_running_loop().run_in_executor(
        executor,
        copy_context().run,
        partial(entrypoint, *args, **kwargs),
    )
    # A thread cannot be stopped; a cancelled call waits for it, however often cancelled, so nothing such as its
    # toolkit's close runs under it.
    return await _maybe_await(await wait_for_future_until_complete(work))


async def _run_toolkit_entrypoint(
    toolkit: Toolkit,
    entrypoint: Callable[..., object],
    args: list[Any],
    kwargs: dict[str, Any],
) -> object:
    if not requires_connect(toolkit):
        return await _call_entrypoint(entrypoint, args, kwargs)

    # Connect and close can block on a slow database like any tool function. A connect that failed or was
    # cancelled partway may still hold a connection, so close runs on every path.
    try:
        await _call_entrypoint(toolkit.connect, [], {})
        return await _call_entrypoint(entrypoint, args, kwargs)
    finally:
        await _call_entrypoint(toolkit.close, [], {})


def _runtime_paths_for_runner_agent_paths(runtime_paths: RuntimePaths) -> RuntimePaths:
    """Return runtime paths rooted at the shared storage visible to this runner."""
    shared_storage_root = sandbox_exec.runner_storage_root(runtime_paths)
    if shared_storage_root == runtime_paths.storage_root.resolve():
        return runtime_paths
    return replace(runtime_paths, storage_root=shared_storage_root)


def _resolve_entrypoint(
    *,
    runtime_paths: RuntimePaths,
    config: Config,
    tools: ToolRegistry,
    tool_name: str,
    function_name: str,
    execution_identity: ToolExecutionIdentity | None = None,
    credential_overrides: dict[str, object] | None = None,
    tool_config_overrides: dict[str, object] | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    runtime_overrides: dict[str, object] | None = None,
    credentials_manager: CredentialsManager | None = None,
    worker_scope: WorkerScope | None = None,
    routing_agent_name: str | None = None,
    private_agent_names: frozenset[str] | None = None,
    tool_output_workspace_root: Path | None = None,
    sandbox: bool = False,
) -> tuple[Toolkit, Callable[..., object]]:
    """Build one tool's entrypoint from ``tools``, the registry its caller composed for ``config``."""
    worker_target = build_worker_target_from_runtime_env(
        worker_scope,
        routing_agent_name,
        execution_identity=execution_identity,
        runtime_paths=runtime_paths,
        private_agent_names=private_agent_names,
    )
    try:
        toolkit = get_tool_by_name(
            tool_name,
            runtime_paths=runtime_paths,
            tools=tools,
            runs_in_worker=False,
            in_sandbox_request=True,
            credential_overrides=credential_overrides,
            credentials_manager=credentials_manager or get_runtime_credentials_manager(runtime_paths),
            runtime_config=config,
            tool_config_overrides=tool_config_overrides,
            tool_init_overrides=tool_init_overrides,
            runtime_overrides=runtime_overrides,
            allowed_shared_services=(
                frozenset()
                if sandbox
                else (config.get_worker_grantable_credentials() if worker_scope is not None else None)
            ),
            tool_output_workspace_root=tool_output_workspace_root,
            tool_output_auto_save_threshold_bytes=config.defaults.tool_output_auto_save_threshold_bytes,
            worker_target=worker_target,
        )
    except (ToolConfigOverrideError, ToolInitOverrideError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # The function a primary run offers under this name: the async variant of a toolkit that registers both.
    function = toolkit_function(toolkit, function_name)
    if function is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' does not expose '{function_name}'.")
    return toolkit, function.entrypoint


def _prepared_shell_execution_env(
    request: ExecuteRequest,
    runtime_paths: RuntimePaths,
    prepared: sandbox_worker_prep.PreparedWorkerRequest | None,
    execution_env: dict[str, str],
) -> dict[str, str] | None:
    """Return the worker shell env when shell execution is bound to a prepared worker."""
    if request.tool_name != "shell" or prepared is None:
        return None
    worker_execution_env = sandbox_exec.worker_subprocess_env(prepared.paths)
    worker_base_path = worker_execution_env.get("PATH")
    if not request.sandbox:
        worker_execution_env.update(
            constants.shell_extra_env_values(
                extra_env_passthrough=request.extra_env_passthrough,
                process_env=request.execution_env or runtime_paths.process_env,
            ),
        )
    worker_execution_env.update(execution_env)
    worker_path = constants.subprocess_path_with_prepends(
        os.pathsep.join(
            path
            for path in (
                worker_execution_env.get("PATH"),
                worker_base_path,
            )
            if path
        ),
        prepend_entries=(str(prepared.paths.venv_dir / "bin"),),
    )
    if worker_path is not None:
        worker_execution_env["PATH"] = worker_path
    return worker_execution_env


def _prepare_execute_request(call: _ExecuteCall) -> _PreparedSandboxRequestContext:
    request, route = call.request, call.route
    runtime_paths, prepared = route.runtime_paths, route.worker
    if request.sandbox:
        execution_env: dict[str, str] = {}
        kept_set_names: list[str] = []
        removed_names: list[str] = []
        call_path_prepend: list[str] = []
    else:
        execution_env = sandbox_exec.request_execution_env(
            request.tool_name,
            request.execution_env,
            runtime_paths,
            extra_env_passthrough=request.extra_env_passthrough,
        )
        # The call's own changes, except where the worker's call env changes the name, as its CA bundle does.
        kept_set_names = [
            name
            for name in request.environment.set_names
            if name in request.execution_env and execution_env.get(name) == request.execution_env[name]
        ]
        removed_names = [
            name
            for name in request.environment.unset_names
            if name not in execution_env and name not in KUBERNETES_POD_LIMIT_ENV_NAMES
        ]
        call_path_prepend = list(request.environment.path_prepend)
    execution_env = _prepared_shell_execution_env(request, runtime_paths, prepared, execution_env) or execution_env
    if request.sandbox:
        execution_env = strip_authenticated_proxy_env(execution_env)
    # A kept name takes its value as the worker composed it, so its own PATH entries stay around a hook's PATH.
    call_environment: dict[str, str | None] = {name: execution_env[name] for name in kept_set_names}
    call_environment.update(dict.fromkeys(removed_names))
    try:
        trusted_overlay = sandbox_env_assembly.build_request_execution_env(
            request_workspace=route.workspace if request.tool_name in sandbox_exec.EXECUTION_ENV_TOOL_NAMES else None,
            prepared=prepared,
            execution_env=execution_env,
            sandbox=request.sandbox,
        )
    except sandbox_exec.WorkspaceEnvHookError as exc:
        raise sandbox_worker_prep.WorkerRequestPreparationError(str(exc)) from exc
    runtime_overrides = _request_runtime_overrides(call)
    effective_runtime_paths = sandbox_exec.tool_runtime_paths_with_request_env(
        runtime_paths,
        execution_env,
        include_base_execution_env=request.tool_name not in sandbox_exec.EXECUTION_ENV_TOOL_NAMES,
        trusted_env_overlay=trusted_overlay,
        sandbox=request.sandbox,
    )
    tool_output_workspace_root = (
        str(route.workspace)
        if route.workspace is not None
        and (not request.sandbox or (request.base_dir is not None and os.access(route.workspace, os.W_OK)))
        else None
    )
    output_path = normalize_output_path_argument(request.kwargs.get(OUTPUT_PATH_ARGUMENT))
    if request.sandbox and output_path is not None:
        msg = (
            "mindroom_output_path is not supported in a sandboxed named worker; "
            "write to a file inside mount_dir or capture stdout directly."
        )
        raise sandbox_worker_prep.WorkerRequestPreparationError(msg)
    kwargs = dict(request.kwargs)
    if output_path is None:
        kwargs.pop(OUTPUT_PATH_ARGUMENT, None)
    tool_config_overrides = dict(request.tool_config_overrides)
    tool_init_overrides = {
        **request.tool_init_overrides,
        **_filter_runtime_tool_init_overrides(request.tool_name, runtime_overrides, call.tools),
    }
    if request.sandbox:
        tool_config_overrides.pop("extra_env_passthrough", None)
        tool_init_overrides.pop("extra_env_passthrough", None)
    prepared_request = PreparedSandboxRunnerExecuteRequest(
        tool_name=request.tool_name,
        function_name=request.function_name,
        run_as_job=request.run_as_job,
        structured_result=request.structured_result,
        shell_scope_id=request.shell_scope_id,
        args=list(request.args),
        kwargs=kwargs,
        worker_scope=request.worker_scope,
        routing_agent_name=request.routing_agent_name,
        execution_identity=(
            dict(serialize_tool_execution_identity(route.identity)) if route.identity is not None else None
        ),
        private_agent_names=list(request.private_agent_names) if request.private_agent_names is not None else None,
        credential_overrides={} if request.sandbox else dict(_subprocess_credential_overrides(call)),
        tool_config_overrides=tool_config_overrides,
        tool_init_overrides=tool_init_overrides,
        call_environment=call_environment,
        call_path_prepend=call_path_prepend,
        runtime_overrides=runtime_overrides,
        tool_output_workspace_root=tool_output_workspace_root,
        sandbox=request.sandbox,
    )
    return _PreparedSandboxRequestContext(
        request=prepared_request,
        runtime_paths=effective_runtime_paths,
        execution_env=execution_env,
        route=route,
    )


def _prepare_subprocess_context(
    prepared_request: _PreparedSandboxRequestContext,
) -> _PreparedSandboxSubprocessContext:
    route = prepared_request.route
    python_executable, template_env, subprocess_cwd = sandbox_exec.resolve_subprocess_worker_context(
        route.worker.paths if route.worker is not None else None,
    )
    subprocess_env = sandbox_exec.subprocess_env_for_request(
        template_env,
        prepared_request.execution_env,
        sandbox=prepared_request.request.sandbox,
    )
    if route.workspace is not None:
        subprocess_cwd = str(route.workspace.resolve())
    return _PreparedSandboxSubprocessContext(
        python_executable=python_executable,
        subprocess_env=subprocess_env,
        subprocess_cwd=subprocess_cwd,
        template_env=template_env,
    )


def _oauth_connection_required_response(
    prepared: PreparedSandboxRunnerExecuteRequest,
    exc: OAuthConnectionRequired,
) -> ExecuteResponse:
    logger.info(
        "sandbox_tool_oauth_connection_required",
        tool_name=prepared.tool_name,
        function_name=prepared.function_name,
        provider_id=exc.provider_id,
    )
    return ExecuteResponse(ok=True, result=oauth_connection_required_payload(exc))


async def _execute_prepared_request_inprocess(
    prepared: PreparedSandboxRunnerExecuteRequest,
    runtime_paths: RuntimePaths,
    config: Config,
    tools: ToolRegistry,
    *,
    credentials_manager: CredentialsManager | None = None,
) -> ExecuteResponse:
    execution_identity = (
        parse_tool_execution_identity_payload(prepared.execution_identity)
        if prepared.execution_identity is not None
        else None
    )
    private_agent_names = _freeze_private_agent_names(prepared.private_agent_names)
    tool_output_workspace_root = (
        Path(prepared.tool_output_workspace_root) if prepared.tool_output_workspace_root is not None else None
    )

    with bind_detached_execution_identity(execution_identity):
        try:
            toolkit, entrypoint = _resolve_entrypoint(
                runtime_paths=runtime_paths,
                config=config,
                tools=tools,
                tool_name=prepared.tool_name,
                function_name=prepared.function_name,
                execution_identity=execution_identity,
                credential_overrides=prepared.credential_overrides or None,
                tool_config_overrides=prepared.tool_config_overrides or None,
                tool_init_overrides=prepared.tool_init_overrides or None,
                runtime_overrides=prepared.runtime_overrides or None,
                credentials_manager=credentials_manager,
                worker_scope=prepared.worker_scope,
                routing_agent_name=prepared.routing_agent_name,
                private_agent_names=private_agent_names,
                tool_output_workspace_root=tool_output_workspace_root,
                sandbox=prepared.sandbox,
            )
        except OAuthConnectionRequired as exc:
            return _oauth_connection_required_response(prepared, exc)

        try:
            call_environment = ToolEnvironment(
                values=prepared.call_environment,
                path_prepend=tuple(prepared.call_path_prepend),
            )
            structured = structured_tool_result() if prepared.structured_result else nullcontext()
            with collected_tool_environment(call_environment), structured as metadata:
                result = await _run_toolkit_entrypoint(toolkit, entrypoint, prepared.args, prepared.kwargs)
            result = await asyncio.to_thread(serialize_worker_tool_result, result)
        except OAuthConnectionRequired as exc:
            return _oauth_connection_required_response(prepared, exc)
        except Exception as exc:
            logger.warning(
                "sandbox_tool_execution_failed",
                tool_name=prepared.tool_name,
                function_name=prepared.function_name,
                exc_info=True,
            )
            return ExecuteResponse(
                ok=False,
                error=f"Sandbox tool execution failed: {type(exc).__name__}: {exc}",
                failure_kind="tool",
            )

    return ExecuteResponse(ok=True, result=result, metadata=metadata or None)


_inprocess_shell_records: dict[str, ProcessRecord] = {}


def close_inprocess_shell_scopes() -> None:
    """Stop every in-process auto-backgrounded shell process on runner shutdown."""
    kill_all_records(_inprocess_shell_records)


async def _execute_request_inprocess(call: _ExecuteCall) -> ExecuteResponse:
    prepared_request = _prepare_execute_request(call)
    scope_id = prepared_request.request.shell_scope_id
    scope = ShellBackgroundScope(scope_id=scope_id, records=_inprocess_shell_records) if scope_id is not None else None
    with bind_shell_background_scope(scope) if scope is not None else nullcontext():
        response = await _execute_prepared_request_inprocess(
            prepared_request.request,
            prepared_request.runtime_paths,
            call.route.config,
            call.tools,
            credentials_manager=get_runtime_credentials_manager(call.route.runtime_paths),
        )
    if scope is not None and scope.had_backgrounded:
        return response.model_copy(update={"shell_backgrounded": True})
    return response


def prepare_terminal_launch(app: FastAPI, payload: ExecuteRequest, cwd: str | None) -> TerminalLaunch:
    """Return the interactive shell of a terminal opened with ``payload``, a shell call's request, in ``cwd``.

    Blocking.
    """
    if payload.tool_name != "shell":
        raise TerminalError(400, "A terminal opens only with a shell call's request.")
    route = prepare_route(app, payload)
    if isinstance(route, WorkerFailure):
        raise TerminalError(400, route.error)
    tools = refresh_plugins(route.config, route.runtime_paths).tools
    try:
        request = _validated_execute_request(payload, tool_metadata=tools.entries)
        return _shell_launch(
            _ExecuteCall(request=request, route=route, tools=tools),
            lambda toolkit, process_environ: toolkit.terminal_launch(cwd, process_environ=process_environ),
        )
    except HTTPException as exc:
        raise TerminalError(400, str(exc.detail)) from None
    except sandbox_worker_prep.WorkerRequestPreparationError as exc:
        raise TerminalError(400, str(exc)) from None


def _shell_launch[Launch](
    call: _ExecuteCall,
    launch: Callable[[SupportsTerminalLaunch, Mapping[str, str] | None], Launch],
) -> Launch:
    """Return what ``launch`` starts from the shell toolkit ``call``, a shell call, would run.

    The request is prepared as ``/execute`` prepares it and the toolkit is built from it, so what starts gets the
    environment, workspace and ``PATH`` the call's command would. Blocking.
    """
    route = call.route
    prepared = _prepare_execute_request(call)
    shell = prepared.request
    toolkit, _ = _resolve_entrypoint(
        runtime_paths=prepared.runtime_paths,
        config=route.config,
        tools=call.tools,
        tool_name=shell.tool_name,
        function_name=shell.function_name,
        execution_identity=route.identity,
        credential_overrides=shell.credential_overrides or None,
        tool_config_overrides=shell.tool_config_overrides or None,
        tool_init_overrides=shell.tool_init_overrides or None,
        runtime_overrides=shell.runtime_overrides or None,
        worker_scope=shell.worker_scope,
        routing_agent_name=shell.routing_agent_name,
        private_agent_names=_freeze_private_agent_names(shell.private_agent_names),
        tool_output_workspace_root=(
            Path(shell.tool_output_workspace_root) if shell.tool_output_workspace_root is not None else None
        ),
        sandbox=shell.sandbox,
    )
    if not isinstance(toolkit, SupportsTerminalLaunch):
        raise TerminalError(400, "This shell cannot start a terminal.")
    # Where /execute runs a shell call in a subprocess, the command sees that subprocess's environment.
    in_subprocess = sandbox_exec.runner_uses_subprocess(route.runtime_paths) or route.worker_key is not None
    process_environ = (
        _prepare_subprocess_context(prepared).subprocess_env
        if in_subprocess
        else (strip_authenticated_proxy_env(os.environ) if shell.sandbox else None)
    )
    call_environment = ToolEnvironment(values=shell.call_environment, path_prepend=tuple(shell.call_path_prepend))
    with collected_tool_environment(call_environment):
        return launch(toolkit, process_environ)


# The stdin of each running command whose output streams, by its request's ID.
_command_stdins: dict[str, CommandStdin] = {}
# A streamed command waits to write more while this many reads of its output wait for the primary.
_STREAM_QUEUED_FRAMES = 4


async def _execute_streamed(call: _ExecuteCall, frames: asyncio.Queue[bytes]) -> ExecuteResponse:
    """Run a shell call's command, putting its raw output and its exit into ``frames`` as lines of the stream.

    The runner starts the command itself, as it starts a terminal's shell, so the bytes pass no text formatting.
    The result is what the reply's tool trace shows of the call, and holds none of the output.
    """
    request = call.request
    try:
        launch, stop_after = await asyncio.to_thread(
            lambda: _shell_launch(
                call,
                lambda toolkit, process_environ: toolkit.command_launch(
                    *request.args,
                    process_environ=process_environ,
                    **request.kwargs,
                ),
            ),
        )
    except (TerminalError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    stdin = CommandStdin()
    sent = {"out": 0, "err": 0}

    def line(**frame: object) -> bytes:
        return json.dumps(frame).encode() + b"\n"

    async def on_start() -> None:
        if request.request_id:
            _command_stdins[request.request_id] = stdin
        await frames.put(line(t="started"))

    async def on_output(kind: str, chunk: bytes) -> None:
        sent[kind] += len(chunk)
        await frames.put(line(t=kind, b=base64.b64encode(chunk).decode("ascii")))

    async def on_exit(code: int, timed_out: bool) -> None:
        await frames.put(line(t="exit", code=code, timed_out=timed_out))
        sent.update(code=code, timed_out=timed_out)

    def on_stopped() -> None:
        # The command's processes are gone. That takes the place of the output its caller no longer reads.
        while not frames.empty():
            frames.get_nowait()
        frames.put_nowait(line(t="exit", cancelled=True))

    try:
        await stream_command(
            list(launch.argv),
            launch.env,
            launch.cwd,
            timeout=stop_after,
            stdin=stdin,
            on_start=on_start,
            on_output=on_output,
            on_exit=on_exit,
            on_stopped=on_stopped,
        )
    except OSError as exc:
        return ExecuteResponse(ok=False, error=f"Error: {exc}", failure_kind="tool")
    finally:
        _command_stdins.pop(request.request_id or "", None)
    result = f"Stopped after {stop_after:g}s." if sent["timed_out"] else f"Exit code {sent['code']}"
    streamed = f"Streamed {sent['out']} bytes of stdout and {sent['err']} bytes of stderr to the caller."
    return ExecuteResponse(ok=True, result=f"{result}\n{streamed}" if sent["timed_out"] or sent["code"] else streamed)


def _cancelled_response() -> ExecuteResponse:
    return ExecuteResponse(ok=False, error="Tool call was cancelled.", failure_kind="tool")


def _subprocess_failure_response(
    worker_key: str | None,
    error: str,
    runtime_paths: RuntimePaths,
) -> ExecuteResponse:
    if sandbox_request_cancellation.request_cancelled():
        # The primary stopped this call, so its killed process says nothing about worker health.
        return _cancelled_response()
    sandbox_worker_prep.record_worker_failure(worker_key, error, runtime_paths)
    return ExecuteResponse(ok=False, error=error, failure_kind="worker")


def _parse_subprocess_response(
    worker_key: str | None,
    runtime_paths: RuntimePaths,
    completed: subprocess.CompletedProcess[str],
) -> ExecuteResponse:
    # The worker writes the JSON response to stderr after a marker line so that
    # tool stdout does not corrupt the protocol.
    stderr = completed.stderr or ""
    if (timing := sandbox_protocol.extract_timing(stderr)) is not None:
        logger.debug("sandbox_subprocess_child_timing", timing=timing)
    response_json = sandbox_protocol.extract_response_json(stderr)
    if response_json:
        try:
            return ExecuteResponse.model_validate_json(response_json)
        except ValidationError:
            pass

    if completed.returncode != 0:
        error = (
            stderr.strip() or completed.stdout.strip() or f"Sandbox subprocess exited with code {completed.returncode}."
        )
        return _subprocess_failure_response(worker_key, error, runtime_paths)

    return _subprocess_failure_response(worker_key, "Sandbox subprocess returned an invalid response.", runtime_paths)


def _execute_request_forkserver(
    worker_key: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    *,
    subprocess_context: _PreparedSandboxSubprocessContext,
    envelope: str,
    timeout_seconds: float | None,
) -> ExecuteResponse | None:
    """Dispatch one prepared request via the warm template; None means fall back to spawn-per-call.

    The request runs in the template that preloaded plugins only when ``config``'s enabled plugins begin with exactly
    those, as configured, so that every plugin still imports in config order; any other request runs in the plain
    template, as without a preload. When the preloading template is gone or cannot be reached, which no request has
    reached then, the plain one runs the request in the time that is left.
    """
    started_at = time.monotonic()

    def execute(forkserver: sandbox_forkserver._SandboxForkserver) -> subprocess.CompletedProcess[str]:
        spent = time.monotonic() - started_at
        return forkserver.execute(
            python_executable=subprocess_context.python_executable,
            template_env=subprocess_context.template_env,
            request_env=subprocess_context.subprocess_env,
            request_cwd=subprocess_context.subprocess_cwd,
            envelope=envelope,
            timeout_seconds=None if timeout_seconds is None else timeout_seconds - spent,
            startup_timeout_seconds=sandbox_exec.runner_subprocess_timeout_seconds(runtime_paths) - spent,
            bind_stop=sandbox_request_cancellation.bind_request_stop,
            on_progress=report_tool_progress,
        )

    try:
        completed = None
        preloading = sandbox_forkserver.preloading_forkserver()
        enabled = tuple(entry.path for entry in config.plugins if entry.enabled)
        if preloading is not None and (serves := preloading.serves) and enabled[: len(serves)] == serves:
            try:
                completed = execute(preloading)
            except (sandbox_forkserver.ForkserverStartupError, sandbox_forkserver.ForkserverUnreachableError) as exc:
                sandbox_forkserver.abandon_preload(str(exc))
        if completed is None:
            completed = execute(sandbox_forkserver.get_sandbox_forkserver())
    except sandbox_forkserver.ForkserverTimeoutError:
        return _subprocess_failure_response(worker_key, "Sandbox subprocess timed out.", runtime_paths)
    except sandbox_forkserver.ForkserverStartupError as exc:
        # The warm template could not start; fall back to spawn-per-call so
        # tool calls keep working at the old per-request import cost.
        logger.warning("sandbox_forkserver_fallback_to_spawn", error=str(exc))
        return None
    except sandbox_forkserver.ForkserverError as exc:
        return _subprocess_failure_response(worker_key, str(exc), runtime_paths)
    return _parse_subprocess_response(worker_key, runtime_paths, completed)


def _shell_subprocess_dispatch_context(
    prepared: PreparedSandboxRunnerExecuteRequest,
    subprocess_context: _PreparedSandboxSubprocessContext,
    timeout_seconds: float | None,
) -> tuple[_PreparedSandboxSubprocessContext, float | None]:
    """Attach the shell supervisor socket and cover the shell timeout in the budget.

    Shell commands run in the runner's long-lived supervisor process, which
    stops a command whose relay dies, so the per-request subprocess only relays
    the call. Its budget must outlast the shell command's own timeout or the
    runner would kill the relay (and with it the supervised command) mid-wait;
    a job's call has no budget to extend.
    """
    socket_path = shell_supervisor.ensure_shell_supervisor()
    subprocess_env = dict(subprocess_context.subprocess_env or {})
    subprocess_env[shell_supervisor.SHELL_SUPERVISOR_SOCKET_ENV] = socket_path
    if timeout_seconds is not None:
        timeout_seconds = max(
            timeout_seconds,
            shell_request_budget_seconds(
                prepared.tool_name,
                prepared.function_name,
                prepared.kwargs,
                auto_background=prepared.shell_scope_id is not None,
            ),
        )
    return replace(subprocess_context, subprocess_env=subprocess_env), timeout_seconds


def _subprocess_config_yaml(config: Config, tool_name: str) -> str:
    """Serialize the validated config needed by one subprocess tool call."""
    builtin_metadata = builtin_tool_registry().get(tool_name)
    include: dict[str, IncEx] | None = None
    if builtin_metadata is not None and ToolManagedInitArg.RUNTIME_CONFIG not in builtin_metadata.managed_init_args:
        default_fields = {"worker_grantable_credentials", "tool_output_auto_save_threshold_bytes"}
        # Plugin paths let the child register plugin tools; their settings stay out of the process running tool code.
        include = {"plugins": {"__all__": {"path", "enabled"}}, "defaults": default_fields}
        if ToolManagedInitArg.FILE_ACCESS in builtin_metadata.managed_init_args:
            # The injected file_access resolves the routing agent through config.resolution.file_access().
            default_fields.add("file_access")
            include["agents"] = {"__all__": {"display_name", "file_access"}}
    payload = config.model_dump(
        include=include,
        exclude_unset=include is None,
    )
    return yaml_io.safe_dump(payload, sort_keys=False)


def _execute_request_subprocess_sync(call: _ExecuteCall) -> ExecuteResponse:
    runtime_paths, worker_key = call.route.runtime_paths, call.route.worker_key
    prepared_request = _prepare_execute_request(call)
    subprocess_context = _prepare_subprocess_context(prepared_request)
    envelope = sandbox_protocol.serialize_subprocess_envelope(
        request=prepared_request.request.model_dump(mode="json"),
        runtime_paths=constants.serialize_runtime_paths(prepared_request.runtime_paths),
        config_yaml=_subprocess_config_yaml(call.route.config, prepared_request.request.tool_name),
    )
    # A scoped shell call auto-backgrounds at 120s even inside a subagent job, so only unscoped job calls are unbounded.
    timeout_seconds = (
        None
        if prepared_request.request.run_as_job and prepared_request.request.shell_scope_id is None
        else max(
            sandbox_exec.runner_subprocess_timeout_seconds(runtime_paths),
            trafilatura_request_budget_seconds(
                prepared_request.request.tool_name,
                prepared_request.request.function_name,
            ),
        )
    )
    if prepared_request.request.tool_name == "shell":
        try:
            subprocess_context, timeout_seconds = _shell_subprocess_dispatch_context(
                prepared_request.request,
                subprocess_context,
                timeout_seconds,
            )
        except shell_supervisor.ShellSupervisorStartupError as exc:
            return _subprocess_failure_response(worker_key, str(exc), runtime_paths)

    if sandbox_request_cancellation.request_cancelled():
        # Cancelled while the worker was being prepared; never start the tool.
        return _cancelled_response()
    if sandbox_exec.runner_uses_forkserver(runtime_paths) and sandbox_forkserver.forkserver_supported():
        forkserver_response = _execute_request_forkserver(
            worker_key,
            runtime_paths,
            call.route.config,
            subprocess_context=subprocess_context,
            envelope=envelope,
            timeout_seconds=timeout_seconds,
        )
        if forkserver_response is not None:
            return forkserver_response
    return _execute_request_spawn(
        worker_key,
        runtime_paths,
        subprocess_context=subprocess_context,
        envelope=envelope,
        timeout_seconds=timeout_seconds,
    )


def _execute_request_spawn(
    worker_key: str | None,
    runtime_paths: RuntimePaths,
    *,
    subprocess_context: _PreparedSandboxSubprocessContext,
    envelope: str,
    timeout_seconds: float | None,
) -> ExecuteResponse:
    """Dispatch one prepared request to a freshly spawned child process."""
    try:
        completed = _run_request_subprocess(
            sandbox_exec.subprocess_worker_command(
                _SUBPROCESS_WORKER_ARG,
                python_executable=subprocess_context.python_executable,
            ),
            input=envelope,
            timeout=timeout_seconds,
            env=subprocess_context.subprocess_env,
            cwd=subprocess_context.subprocess_cwd,
        )
    except subprocess.TimeoutExpired:
        return _subprocess_failure_response(worker_key, "Sandbox subprocess timed out.", runtime_paths)
    except OSError as exc:
        return _subprocess_failure_response(worker_key, f"Failed to start sandbox subprocess: {exc}", runtime_paths)
    return _parse_subprocess_response(worker_key, runtime_paths, completed)


def _kill_request_process_group(process: subprocess.Popen[str]) -> None:
    """Kill a request child and every process it started, which share its process group."""
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def _decoded(output: bytes | str | None) -> str:
    """Decode output a timed-out ``communicate`` read so far, which arrives as bytes even in text mode."""
    if output is None:
        return ""
    return output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output


def _relay_child_progress(conn: socket.socket) -> None:
    """Report each progress line a request child writes on its socket as this call's progress."""
    with suppress(OSError), conn.makefile("rb") as lines:
        for line in lines:
            with suppress(ValueError, KeyError, TypeError):
                report_tool_progress(str(json.loads(line)["progress"]))


def _run_request_subprocess(
    args: list[str],
    *,
    input: str,  # noqa: A002 - mirrors subprocess.run
    timeout: float | None,
    env: dict[str, str] | None,
    cwd: str | None,
) -> subprocess.CompletedProcess[str]:
    """Run one request child like ``subprocess.run``, killable by the primary's cancel, relaying its progress.

    ``None`` waits however long the child runs. The child leads its own process group, so stopping it stops
    everything it started; like subprocess.run, a stopped child is waited for, not its pipes, which a process it
    started may still hold.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    progress_reader, progress_writer = socket.socketpair()
    try:
        with subprocess.Popen(
            [*args, _PROGRESS_FD_ARG, str(progress_writer.fileno())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=cwd,
            start_new_session=True,
            pass_fds=(progress_writer.fileno(),),
        ) as process:
            progress_writer.close()
            threading.Thread(
                target=copy_context().run,
                args=(_relay_child_progress, progress_reader),
                daemon=True,
            ).start()
            stopped = threading.Event()

            def stop() -> None:
                stopped.set()
                _kill_request_process_group(process)

            sandbox_request_cancellation.bind_request_stop(stop)
            pending_input: str | None = input
            slices_since_exit = 0
            try:
                while True:
                    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                    try:
                        stdout, stderr = process.communicate(
                            pending_input,
                            timeout=_SPAWN_POLL_SECONDS if remaining is None else min(_SPAWN_POLL_SECONDS, remaining),
                        )
                        break
                    except subprocess.TimeoutExpired as expired:
                        # Retrying keeps the input already sent and the output already read.
                        pending_input = None
                        if remaining == 0.0:
                            raise
                        if process.poll() is not None:
                            slices_since_exit += 1
                            # The child exited, but a process it started, such as a server, still holds its pipes:
                            # after one slice to read what the child wrote, stop waiting for their end.
                            if stopped.is_set() or slices_since_exit > 1:
                                stdout, stderr = _decoded(expired.stdout), _decoded(expired.stderr)
                                break
            except BaseException:
                _kill_request_process_group(process)
                process.wait()
                raise
    finally:
        progress_writer.close()
        # Wakes a relay still waiting on a process the child started; the child's own end is closed by now.
        with suppress(OSError):
            progress_reader.shutdown(socket.SHUT_RDWR)
        progress_reader.close()
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


async def _execute_request_subprocess(call: _ExecuteCall) -> ExecuteResponse:
    # A job's request may wait for its child for hours, so it never holds a thread of the shared pool.
    executor = JOB_CALL_EXECUTOR if call.request.run_as_job else None
    return await asyncio.get_running_loop().run_in_executor(
        executor,
        copy_context().run,
        partial(_execute_request_subprocess_sync, call),
    )


def _run_subprocess_worker_payload(payload: str, report_progress: Callable[[str], None]) -> tuple[int, str, str]:
    """Execute one serialized envelope, reporting its tool's progress; return (exit_code, tool_output, marked_response).

    A job's request binds its job, so its tools know they run as part of one.
    """
    if not payload.strip():
        response = ExecuteResponse(
            ok=False,
            error="Sandbox subprocess received empty payload.",
            failure_kind="worker",
        )
        return 1, "", sandbox_protocol.response_marker_payload(response.model_dump_json())

    stage_ends = [time.monotonic()]

    try:
        envelope = sandbox_protocol.parse_subprocess_envelope(payload)
        request = PreparedSandboxRunnerExecuteRequest.model_validate(envelope.request)
        runtime_paths = constants.deserialize_runtime_paths(envelope.runtime_paths)
        config = Config.model_validate(
            yaml_io.safe_load(envelope.config_yaml),
            context={"runtime_paths": runtime_paths},
        )
    except (TypeError, ValidationError, yaml.YAMLError) as exc:
        response = ExecuteResponse(
            ok=False,
            error=f"Sandbox subprocess payload validation failed: {exc}",
            failure_kind="worker",
        )
        return 1, "", sandbox_protocol.response_marker_payload(response.model_dump_json())
    if sandbox_exec.runner_uses_dedicated_worker(runtime_paths):
        config = _config_with_available_plugins(config, runtime_paths, log_skipped_plugins=True)
    stage_ends.append(time.monotonic())

    # Redirect stdout/stderr during tool execution so tool output doesn't
    # interfere with the protocol marker in the returned response text.
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    job = job_control_context(JobControl()) if request.run_as_job else nullcontext()
    shell_scope = ShellBackgroundScope(scope_id=request.shell_scope_id) if request.shell_scope_id is not None else None
    scope_binding = bind_shell_background_scope(shell_scope) if shell_scope is not None else nullcontext()
    with (
        redirect_stdout(captured_out),
        redirect_stderr(captured_err),
        tool_progress_reporter(report_progress),
        job,
        scope_binding,
    ):
        # A fresh child loads the request's plugins itself, except the ones its template preloaded.
        tools = refresh_plugins(config, runtime_paths).tools
        stage_ends.append(time.monotonic())
        response = asyncio.run(_execute_prepared_request_inprocess(request, runtime_paths, config, tools))
        stage_ends.append(time.monotonic())
    if shell_scope is not None and shell_scope.had_backgrounded:
        response = response.model_copy(update={"shell_backgrounded": True})

    stages = zip(("config_ms", "plugins_ms", "tool_ms"), stage_ends, stage_ends[1:], strict=False)
    timing: dict[str, object] = {stage: elapsed_ms_between(start, end) for stage, start, end in stages}
    tool_output = captured_out.getvalue() + captured_err.getvalue()
    return 0, tool_output, sandbox_protocol.response_marker_payload(response.model_dump_json(), timing=timing)


def _spawned_progress_reporter() -> Callable[[str], None]:
    """Write progress lines to the socket the runner passed this spawned child, if it passed one."""
    if _PROGRESS_FD_ARG not in sys.argv:
        return lambda _text: None
    descriptor = int(sys.argv[sys.argv.index(_PROGRESS_FD_ARG) + 1])
    # Processes the tool starts must not hold the runner's progress socket open.
    os.set_inheritable(descriptor, False)
    conn = socket.socket(fileno=descriptor)
    lock = threading.Lock()

    def report(text: str) -> None:
        with lock, suppress(OSError):
            conn.sendall(json.dumps({"progress": text}).encode("utf-8") + b"\n")

    return report


def _run_subprocess_worker() -> int:
    exit_code, tool_output, marked_response = _run_subprocess_worker_payload(
        sys.stdin.read(),
        _spawned_progress_reporter(),
    )
    # Flush captured tool output to real stdout (informational only), then
    # write the response JSON to stderr after the marker.
    if tool_output:
        sys.stdout.write(tool_output)
    print(marked_response, file=sys.stderr)
    return exit_code


def _run_forkserver_template() -> int:
    """Serve warm forkserver requests from an already-imported runtime template."""
    socket_path = sys.argv[sys.argv.index(sandbox_forkserver.TEMPLATE_ARG) + 1]
    # Pre-warm the full tool import graph once so fork children skip it; this
    # belongs to template startup, not to importing this module.
    from mindroom.mcp import registry as mcp_registry  # noqa: PLC0415
    from mindroom.tool_system import plugins as tool_system_plugins  # noqa: PLC0415

    _ = mcp_registry, tool_system_plugins
    import mindroom.tools  # noqa: F401, PLC0415

    preload = os.environ.pop(sandbox_forkserver.PRELOAD_ENV, None)
    if preload is not None and (error := _preload_template_plugins(json.loads(preload))) is not None:
        # An import cannot be undone, so this template ends, and the runner serves from one that never tried it.
        os.write(2, error.encode())
        os._exit(1)
    return sandbox_forkserver.serve_template(socket_path, _run_subprocess_worker_payload)


def _preload_template_plugins(spec: dict[str, Any]) -> str | None:
    """Import the worker's opt-in plugins for every request child, and return why the template cannot keep them.

    The kernel ends the template when the import outlasts its budget, also one that never returns to Python.
    """
    threads_and_files = sandbox_forkserver.threads_and_open_files()
    os.write(2, f"Importing the plugins that set worker_preload, for at most {spec['budget_seconds']} s.\n".encode())
    signal.setitimer(signal.ITIMER_REAL, spec["budget_seconds"])
    try:
        runtime_paths = constants.resolve_primary_runtime_paths(
            config_path=Path(spec["config_path"]),
            storage_path=Path(spec["storage_root"]),
            process_env={},
        )
        config = Config(plugins=[PluginEntryConfig(path=path) for path in spec["plugins"]])
        # The primary named these from its own copies; this worker's copies decide, before any of them is imported.
        if worker_preload_plugin_paths(config, runtime_paths) != tuple(spec["plugins"]):
            return "A plugin to preload is missing in this worker, or its manifest here does not set worker_preload."
        if not preload_plugins(config, runtime_paths):
            return "A plugin that sets worker_preload failed to import."
    except Exception as exc:  # Whatever a plugin's import raises, the runner gives the preload up.
        return f"Preloading plugins failed: {exc}"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    if sandbox_forkserver.threads_and_open_files() != threads_and_files:
        return "A plugin that sets worker_preload left a thread running or a file open when it was imported."
    return None


@router.post("/leases")
async def create_credential_lease(request: LeaseRequest) -> LeaseResponse | WorkerFailure:
    """Create a short-lived, one-or-few-use credential lease."""
    if (failure := _protocol_failure(request.protocol)) is not None:
        return failure
    lease = sandbox_worker_prep.create_credential_lease(
        tool_name=request.tool_name,
        function_name=request.function_name,
        credential_overrides=request.credential_overrides,
        ttl_seconds=request.ttl_seconds,
        max_uses=request.max_uses,
    )
    return LeaseResponse(
        lease_id=lease.lease_id,
        expires_at=lease.expires_at,
        max_uses=lease.uses_remaining,
    )


@router.get("/workers", response_model=SandboxWorkerListResponse)
async def list_workers(request: Request, include_idle: bool = True) -> SandboxWorkerListResponse:
    """List known workers and their current lifecycle status."""
    runtime_paths = app_runtime_paths(request.app)
    workers = [
        serialize_sandbox_worker_response(worker)
        for worker in get_local_worker_manager(runtime_paths).list_workers(include_idle=include_idle)
    ]
    return SandboxWorkerListResponse(workers=workers)


@router.post("/workers/cleanup", response_model=SandboxWorkerCleanupResponse)
async def cleanup_idle_workers(request: Request) -> SandboxWorkerCleanupResponse:
    """Mark idle workers inactive while retaining their persisted state."""
    runtime_paths = app_runtime_paths(request.app)
    worker_manager = get_local_worker_manager(runtime_paths)
    cleaned_workers = [serialize_sandbox_worker_response(worker) for worker in worker_manager.cleanup_idle_workers()]
    return SandboxWorkerCleanupResponse(
        idle_timeout_seconds=worker_manager.idle_timeout_seconds,
        cleaned_workers=cleaned_workers,
    )


def _validated_execute_request(request: ExecuteRequest, *, tool_metadata: Mapping[str, ToolMetadata]) -> ExecuteRequest:
    """Return the request with its override channels validated, before execution dispatch."""
    if "base_dir" in request.tool_init_overrides:
        raise HTTPException(status_code=400, detail="Send the workspace as the request's base_dir.")
    tool_init_overrides = request.tool_init_overrides
    if tool_init_overrides and request.tool_name in tool_metadata:
        try:
            tool_init_overrides = (
                sanitize_tool_init_overrides(request.tool_name, tool_init_overrides, tool_metadata=tool_metadata) or {}
            )
        except ToolInitOverrideError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    tool_config_overrides = request.tool_config_overrides
    if tool_config_overrides:
        try:
            tool_config_overrides = validate_authored_tool_entry_overrides(
                request.tool_name,
                tool_config_overrides,
                config_path_prefix="request.tool_config_overrides",
                tool_metadata=tool_metadata,
            )
        except ToolConfigOverrideError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if request.execution_env and request.tool_name not in sandbox_exec.EXECUTION_ENV_TOOL_NAMES:
        raise HTTPException(status_code=400, detail="execution_env is only supported for execution tools.")
    if request.extra_env_passthrough is not None and request.tool_name != "shell":
        raise HTTPException(status_code=400, detail="extra_env_passthrough is only supported for shell.")
    if request.shell_scope_id is not None and (
        not request.shell_scope_id.strip()
        or (request.tool_name, request.function_name) != ("shell", "run_shell_command")
        or request.stream_io
    ):
        raise HTTPException(
            status_code=400,
            detail="shell_scope_id is only supported for non-streamed shell run_shell_command calls.",
        )
    return request.model_copy(
        update={"tool_init_overrides": tool_init_overrides, "tool_config_overrides": tool_config_overrides},
    )


def _missing_workspace_failure() -> WorkerFailure:
    return WorkerFailure(error="This request has no workspace in this worker.", failure_kind="tool")


@router.post("/save-attachment")
async def save_attachment_to_worker(  # noqa: PLR0911
    request: Request,
    payload: SaveAttachmentRequest,
) -> SaveAttachmentResponse | WorkerFailure:
    """Save one context-authorized attachment into the request's workspace."""
    route = await _prepare_route_beside_setup(request.app, payload)
    if isinstance(route, WorkerFailure):
        return route
    if route.workspace is None:
        return _missing_workspace_failure()
    if normalize_attachment_id(payload.attachment_id) is None:
        return WorkerFailure(error="attachment_id is invalid.", failure_kind="tool")
    output_path = payload.mindroom_output_path
    raw_path_error = validate_output_path_syntax(output_path)
    if raw_path_error is not None:
        return WorkerFailure(error=raw_path_error, failure_kind="tool")

    decoded_bytes = decode_attachment_save_bytes(
        bytes_b64=payload.bytes_b64,
        sha256=payload.sha256,
        size_bytes=payload.size_bytes,
    )
    if isinstance(decoded_bytes, str):
        return WorkerFailure(error=decoded_bytes, failure_kind="tool")

    policy = ToolOutputFilePolicy.from_runtime(route.workspace, route.runtime_paths)
    path_error = validate_output_path(policy, output_path)
    if path_error is not None:
        return WorkerFailure(error=path_error, failure_kind="tool")

    write_result = write_bytes_to_output_path(policy, output_path, decoded_bytes, file_mode=0o600)
    if isinstance(write_result, str):
        return WorkerFailure(error=write_result, failure_kind="tool")

    return SaveAttachmentResponse(
        ok=True,
        worker_path=output_path,
        size_bytes=write_result.byte_count,
        sha256=payload.sha256,
    )


@router.post("/view-file")
async def view_file_in_worker(request: Request, payload: ViewFileRequest) -> ViewFileResponse | WorkerFailure:
    """View an image only after resolving the request's workspace."""
    route = await _prepare_route_beside_setup(request.app, payload)
    if isinstance(route, WorkerFailure):
        return route
    if route.workspace is None:
        return _missing_workspace_failure()
    result = await asyncio.to_thread(
        _view_file_result_envelope,
        payload.path,
        route.workspace,
        agent_file_access(route.config, payload.routing_agent_name),
    )
    return ViewFileResponse(ok=True, result=result)


def _view_file_result_envelope(path: str, workspace: Path, file_access: FileAccess) -> dict[str, object]:
    """Read, decode, and encode one image outside the runner event loop."""
    return encode_media_result(view_agent_image(path, workspace=workspace, file_access=file_access))


async def _execute_worker_browser(
    computer: WorkerComputerRuntime | WorkerBrowserRuntime,
    call: _ExecuteCall,
) -> ExecuteResponse:
    """Bind only the validated built-in browser to the persistent ASGI runtime."""
    payload, route = call.request, call.route
    config, runtime_paths, prepared_worker = route.config, route.runtime_paths, route.worker
    agent_name = payload.routing_agent_name or (route.identity.agent_name if route.identity is not None else None)
    if (
        isinstance(computer, WorkerComputerRuntime)
        and agent_name in config.agents
        and all(
            config.agent_has_tool_at_execution_scope(agent_name, provider, "user_agent")
            for provider in ("browser", "browser_mcp")
        )
    ):
        raise HTTPException(status_code=400, detail="Computer requires exactly one browser provider.")

    if (
        prepared_worker is None
        or not sandbox_exec.runner_uses_dedicated_worker(runtime_paths)
        or route.worker_key is None
        or payload.worker_scope is None
        or resolved_worker_key_scope(route.worker_key) != payload.worker_scope
        or (isinstance(computer, WorkerComputerRuntime) and payload.worker_scope != "user_agent")
    ):
        raise HTTPException(
            status_code=400,
            detail="Worker computer requires an unambiguous dedicated user_agent worker.",
        )
    workspace = route.workspace
    assert workspace is not None, "a request with a prepared worker always acts in a workspace"
    provider = select_browser_provider(
        payload.tool_name,
        payload.function_name,
        call.tools.factory(payload.tool_name),
        to_json_compatible,
    )
    prepared_context = _prepare_execute_request(call)
    prepared = prepared_context.request
    browser_paths = replace(prepared_context.runtime_paths, storage_root=prepared_worker.paths.root)
    # A browser tool call stops itself at its own limit with an error that says so, which this limit must outlast.
    timeout_seconds = max(
        sandbox_exec.runner_subprocess_timeout_seconds(runtime_paths),
        browser_request_budget_seconds(payload.tool_name, payload.function_name),
    )
    with bind_detached_execution_identity(route.identity):
        toolkit, current_entrypoint = _resolve_entrypoint(
            runtime_paths=browser_paths,
            config=config,
            tools=call.tools,
            tool_name=payload.tool_name,
            function_name=payload.function_name,
            execution_identity=route.identity,
            credential_overrides=prepared.credential_overrides or None,
            tool_config_overrides=prepared.tool_config_overrides or None,
            tool_init_overrides=prepared.tool_init_overrides or None,
            runtime_overrides=prepared.runtime_overrides or None,
            worker_scope=prepared.worker_scope,
            routing_agent_name=prepared.routing_agent_name,
            private_agent_names=_freeze_private_agent_names(prepared.private_agent_names),
            tool_output_workspace_root=(
                Path(prepared.tool_output_workspace_root) if prepared.tool_output_workspace_root is not None else None
            ),
            sandbox=prepared.sandbox,
        )
        browser_toolkit = provider.validate_toolkit(toolkit)

        async def invoke(
            retained_toolkit: Toolkit,
            entrypoint: Callable[..., object],
            args: list[object],
            kwargs: dict[str, object],
        ) -> object:
            async with asyncio.timeout(timeout_seconds):
                return await _run_toolkit_entrypoint(retained_toolkit, entrypoint, args, kwargs)

        try:
            if isinstance(computer, WorkerBrowserRuntime):
                process_env = _prepare_subprocess_context(prepared_context).subprocess_env or {}
                headless_toolkit, browser_config_key = provider.bind_headless(
                    browser_toolkit,
                    workspace,
                    process_env,
                )
                result = await computer.run(
                    headless_toolkit,
                    (browser_config_key, tuple(sorted(process_env.items()))),
                    partial(invoke, headless_toolkit, current_entrypoint, prepared.args, prepared.kwargs),
                )
                return ExecuteResponse(ok=True, result=provider.encode_result(result))

            browser_config_key, session = provider.bind(
                browser_toolkit,
                computer.display.display,
                workspace,
                invoke,
            )

            def factory(_display: str) -> BrowserSession:
                return session

            binding_key = json.dumps(
                {
                    "browser": browser_config_key,
                    "provider": payload.tool_name,
                    "worker_key": route.worker_key,
                    "runtime": constants.serialize_runtime_paths(browser_paths),
                    "config": config.model_dump(mode="json"),
                    "workspace": str(workspace),
                },
                sort_keys=True,
            )
            result = await computer.run_browser_call(
                binding_key,
                factory,
                [payload.function_name, *prepared.args],
                prepared.kwargs,
            )
            serialized_result = provider.encode_result(result)
        except Exception as exc:
            return ExecuteResponse(
                ok=False,
                error=f"Sandbox tool execution failed: {type(exc).__name__}: {exc}",
                failure_kind="tool",
            )
    return ExecuteResponse(ok=True, result=serialized_result)


@router.post("/execute/cancel")
async def cancel_tool_call(payload: CancelRequest) -> CancelResponse | WorkerFailure:
    """Stop one execute request; a cancel that arrives first stops that request on arrival."""
    if (failure := _protocol_failure(payload.protocol)) is not None:
        return failure
    return CancelResponse(cancelled=sandbox_request_cancellation.cancel_request(payload.request_id))


@router.post("/execute/shell-scope/kill")
async def kill_shell_scope(payload: KillShellScopeRequest) -> KillShellScopeResponse | WorkerFailure:
    """Stop every auto-backgrounded shell process owned by one turn scope."""
    if (failure := _protocol_failure(payload.protocol)) is not None:
        return failure
    namespace = f"shell-scope:{payload.shell_scope_id}"
    kill_namespace_records(_inprocess_shell_records, namespace)
    await asyncio.to_thread(shell_supervisor.kill_shell_supervisor_namespace, namespace)
    return KillShellScopeResponse()


@router.post("/execute/{request_id}/stdin")
async def write_command_stdin(request_id: str, payload: StdinRequest) -> StdinResponse | WorkerFailure:
    """Write the next chunk of a streamed command's stdin, answering once it has left for the command.

    504 says the chunk, or the one before it, has not left yet, and that it is to be sent again.
    """
    if (failure := _protocol_failure(payload.protocol)) is not None:
        return failure
    stdin = _command_stdins.get(request_id)
    if stdin is None:
        return StdinResponse(open=False)
    try:
        return StdinResponse(open=await stdin.write(payload.seq, base64.b64decode(payload.b), eof=payload.eof))
    except TimeoutError:
        raise HTTPException(status_code=504, detail="The command has not read its stdin yet") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@dataclass(frozen=True)
class _PreparedToolCall:
    execute: Callable[[], Coroutine[object, object, ExecuteResponse]]
    # Persistent browser actions are short and keep their own deadline, and cancelling one closes the shared
    # browser, so they answer in one response that neither a dropped connection nor /execute/cancel stops.
    streamed: bool = True
    # The lines a call that streams its command's output sends before its result.
    frames: asyncio.Queue[bytes] | None = None


@router.post("/execute", response_model=None)
async def execute_tool_call(request: Request, payload: ExecuteRequest) -> Response:
    """Execute a tool function locally, streaming its progress and keep-alives, then its serialized result.

    A request the runner refuses before the call starts answers at once. The primary stops the call through
    /execute/cancel or by hanging up.
    """
    route = await _prepare_route_beside_setup(request.app, payload)
    if isinstance(route, WorkerFailure):
        return JSONResponse(route.model_dump(mode="json"))
    call = _prepare_tool_call(request.app, payload, route)
    if not call.streamed:
        result: BaseModel
        with sandbox_request_cancellation.track_request(payload.request_id):
            try:
                result = (
                    _cancelled_response() if sandbox_request_cancellation.request_cancelled() else await call.execute()
                )
            except Exception as error:
                result = _worker_failure(error)
        return JSONResponse(result.model_dump(mode="json"))
    return StreamingResponse(
        sandbox_execute_stream.stream_execution(
            payload.request_id or uuid4().hex,
            call.execute,
            keepalive_seconds=EXECUTE_KEEPALIVE_SECONDS,
            failure=_worker_failure,
            frames=call.frames,
        ),
        media_type="application/x-ndjson",
    )


def _persistent_browser_runtime(
    app: FastAPI,
    payload: ExecuteRequest,
) -> WorkerComputerRuntime | WorkerBrowserRuntime | None:
    """Return the worker's long-lived browser runtime when this call drives it."""
    if payload.tool_name != "browser_mcp" and (payload.tool_name, payload.function_name) != (
        "browser",
        "browser_control",
    ):
        return None
    computer = getattr(app.state, "worker_computer", None)
    if isinstance(computer, WorkerComputerRuntime):
        return computer
    if payload.tool_name == "browser_mcp":
        raise HTTPException(status_code=400, detail="browser_mcp requires an enabled dedicated computer worker.")
    browser = getattr(app.state, "worker_browser", None)
    return browser if isinstance(browser, WorkerBrowserRuntime) else None


def _prepare_tool_call(app: FastAPI, payload: ExecuteRequest, route: _PreparedRoute) -> _PreparedToolCall:
    """Validate one call and return its dispatch; a job's request runs as part of its job."""
    # Plugin tools come from the request's config, not from a startup config runners never receive.
    # The loader re-runs only plugins whose tools, hooks, or OAuth module changed since their last load.
    tools = refresh_plugins(route.config, route.runtime_paths).tools
    request = _validated_execute_request(payload, tool_metadata=tools.entries)
    credential_overrides: dict[str, Any] = {}
    if request.lease_id is not None:
        credential_overrides = sandbox_worker_prep.consume_credential_lease(
            request.lease_id,
            tool_name=request.tool_name,
            function_name=request.function_name,
        )
    call = _ExecuteCall(request=request, route=route, tools=tools, credential_overrides=credential_overrides)
    if (computer := _persistent_browser_runtime(app, request)) is not None:
        return _PreparedToolCall(partial(_execute_worker_browser, computer, call), streamed=False)

    frames: asyncio.Queue[bytes] | None = None
    if request.stream_io:
        if (request.tool_name, request.function_name) != ("shell", "run_shell_command"):
            raise HTTPException(status_code=400, detail="Only a shell command's output streams.")
        frames = asyncio.Queue(maxsize=_STREAM_QUEUED_FRAMES)

    async def dispatch() -> ExecuteResponse:
        if frames is not None:
            return await _run_cancellable(_execute_streamed(call, frames))
        # Worker-routed calls also take the subprocess path, so the per-worker virtualenv and worker-specific
        # process environment stay authoritative even when this pod is itself a dedicated worker runtime.
        if sandbox_exec.runner_uses_subprocess(route.runtime_paths) or route.worker_key is not None:
            return await _execute_request_subprocess(call)
        return await _run_cancellable(_execute_request_inprocess(call))

    async def execute() -> ExecuteResponse:
        if sandbox_request_cancellation.request_cancelled():
            return _cancelled_response()
        with job_control_context(JobControl()) if request.run_as_job else nullcontext():
            return await dispatch()

    return _PreparedToolCall(execute, frames=frames)


async def _run_cancellable(
    execution: Coroutine[object, object, ExecuteResponse],
) -> ExecuteResponse:
    """Run one request inside this process as a task the primary's cancel can stop."""
    call = asyncio.create_task(execution)
    loop = asyncio.get_running_loop()
    sandbox_request_cancellation.bind_request_stop(lambda: loop.call_soon_threadsafe(call.cancel))
    try:
        return await call
    except asyncio.CancelledError:
        current = asyncio.current_task()
        assert current is not None
        if current.cancelling() or not sandbox_request_cancellation.request_cancelled():
            call.cancel()
            raise
        return _cancelled_response()


if __name__ == "__main__":
    if _SUBPROCESS_WORKER_ARG in sys.argv:
        raise SystemExit(_run_subprocess_worker())
    if sandbox_forkserver.TEMPLATE_ARG in sys.argv:
        raise SystemExit(_run_forkserver_template())
