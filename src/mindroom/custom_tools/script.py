"""Primary-only background Python script controls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.script_runs.manager import ScriptRunLimits, ScriptRunManagerError
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tools.native_helpers import validate_action_params
from mindroom.workers.models import ScriptResourceProfileName  # noqa: TC001 - the tool schema reads it at runtime.

if TYPE_CHECKING:
    from mindroom.script_runs.manager import ScriptRunManager
    from mindroom.script_runs.models import ScriptRunRecord
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

__all__ = ["ScriptTools"]

_ReadAction = Literal["list", "get", "resource_profiles"]
_WriteAction = Literal["start", "cancel"]
_READ_ALLOWED: dict[str, frozenset[str]] = {
    "list": frozenset({"include_finished"}),
    "get": frozenset({"run_id"}),
    "resource_profiles": frozenset(),
}
_WRITE_ALLOWED: dict[str, frozenset[str]] = {
    "start": frozenset({"source", "path", "name", "resource_profile"}),
    "cancel": frozenset({"run_id", "force"}),
}


class ScriptTools(Toolkit):
    """Start and control requester-scoped background Python scripts."""

    def __init__(
        self,
        allowed_tools: list[str] | None = None,
        max_concurrent_runs: int = 3,
        max_tool_calls_per_minute: int = 30,
        max_runtime_hours: float = 24,
    ) -> None:
        self.limits = ScriptRunLimits(
            allowed_tools=_normalized_allowed_tools(allowed_tools),
            max_concurrent_runs=max_concurrent_runs,
            max_tool_calls_per_minute=max_tool_calls_per_minute,
            max_runtime_hours=max_runtime_hours,
        )
        super().__init__(
            name="script",
            tools=[self.script_read, self.script_write],
        )

    async def script_read(
        self,
        action: _ReadAction,
        run_id: str | None = None,
        include_finished: bool | None = None,
    ) -> str:
        """Inspect owned background Python scripts and available resource profiles.

        - `list`: List background scripts owned by this requester and agent.
        - `get`: Return durable state and recent process output for one owned script.
        - `resource_profiles`: Return the exact CPU and memory reservations available to background scripts.

        Args:
            action: Read operation to perform (`list`, `get`, or `resource_profiles`).
            run_id: Identifier returned by `script_write(action="start")` (required for `get`).
            include_finished: Include terminal runs in addition to active runs (`list` only, defaults to true).

        """
        if (
            error := validate_action_params(
                "script_read",
                action,
                _READ_ALLOWED,
                {"run_id": run_id, "include_finished": include_finished},
            )
        ) is not None:
            return _payload("error", message=error)
        if action == "get":
            if not isinstance(run_id, str) or not run_id.strip():
                return _payload("error", message="run_id is required for action='get'.")
            resolved_run_id = run_id.strip()
        try:
            manager, context = _runtime()
            if action == "list":
                runs = await manager.list(
                    context,
                    include_finished=True if include_finished is None else include_finished,
                )
                return _payload("ok", action="list", runs=[_public_run(run) for run in runs])
            if action == "get":
                status = await manager.status(context, run_id=resolved_run_id)
                return _payload("ok", action="status", run=_public_run(status.run), output=status.output)
            return _payload("ok", action="resource_profiles", **manager.resource_profiles())
        except ScriptRunManagerError as exc:
            return _payload("error", message=str(exc))

    async def script_write(
        self,
        action: _WriteAction,
        source: str | None = None,
        path: str | None = None,
        name: str | None = None,
        resource_profile: ScriptResourceProfileName | None = None,
        run_id: str | None = None,
        force: bool | None = None,
    ) -> str:
        """Start or cancel a background Python script.

        - `start`: Run a Python script in the background. Provide exactly one of `source` or `path`; a path is relative to this agent's workspace and is snapshotted before execution. The script gets no command-line arguments and calls this agent's tools with `mindroom-agent tools call TOOLKIT FUNCTION '{"argument": "value"}'` (waiting for the result) or `from mindroom.script_sdk import MindRoomTools` and `MindRoomTools().call("toolkit", "function", argument="value")`.
        - `cancel`: Cancel one owned background script after revoking its tool capability.

        Args:
            action: Write operation to perform (`start` or `cancel`).
            source: Inline Python source code (`start` only).
            path: Workspace-relative path to a Python source file (`start` only).
            name: Optional short label shown by status and list operations (`start` only).
            resource_profile: Optional bounded Kubernetes profile (`start` only). Call `script_read(action="resource_profiles")` to inspect the exact resources, or omit it to use the configured default.
            run_id: Identifier returned by `script_write(action="start")` (required for `cancel`).
            force: Force-kill the supervised process instead of requesting graceful termination (`cancel` only, defaults to false).

        """
        params: dict[str, object] = {
            "source": source,
            "path": path,
            "name": name,
            "resource_profile": resource_profile,
            "run_id": run_id,
            "force": force,
        }
        if (error := validate_action_params("script_write", action, _WRITE_ALLOWED, params)) is not None:
            return _payload("error", message=error)
        if action == "cancel":
            if not isinstance(run_id, str) or not run_id.strip():
                return _payload("error", message="run_id is required for action='cancel'.")
            resolved_run_id = run_id.strip()
        try:
            manager, context = _runtime()
            if action == "start":
                run = await manager.run(
                    context,
                    source=source,
                    path=path,
                    name=name,
                    resource_profile=resource_profile,
                    limits=self.limits,
                )
                return _payload("ok", action="run", run=_public_run(run))
            run = await manager.cancel(context, run_id=resolved_run_id, force=False if force is None else force)
            return _payload("ok", action="cancel", run=_public_run(run))
        except ScriptRunManagerError as exc:
            return _payload("error", message=str(exc))


def _runtime() -> tuple[ScriptRunManager, ToolRuntimeContext]:
    context = get_tool_runtime_context()
    if context is None:
        msg = "Background script controls require an active room context."
        raise ScriptRunManagerError(msg)
    manager = None if context.orchestrator is None else context.orchestrator.script_runtime.tool_manager
    if manager is None:
        msg = "Background script runtime is not ready."
        raise ScriptRunManagerError(msg)
    return manager, context


def _payload(status: str, **fields: object) -> str:
    return custom_tool_payload("script", status, **fields)


def _public_run(run: ScriptRunRecord) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "agent_name": run.agent_name,
        "name": run.name,
        "state": run.state.value,
        "execution_mode": "unsafe_local" if run.local_unsafe else "worker",
        "local_unsafe": run.local_unsafe,
        "resource_profile": run.resource_profile,
        "resource_requests": dict(run.resource_requests),
        "resource_limits": dict(run.resource_limits),
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "exit_code": run.exit_code,
        "error": run.error,
        "cancel_requested_at": run.cancel_requested_at,
        "cancellation_reason": run.cancellation_reason,
        "max_tool_calls_per_minute": run.max_tool_calls_per_minute,
        "max_runtime_seconds": run.max_runtime_seconds,
        "grants": [{"toolkit_name": grant.toolkit_name, "function_name": grant.function_name} for grant in run.grants],
    }


def _normalized_allowed_tools(allowed_tools: list[str] | None) -> tuple[str, ...] | None:
    normalized = (name.strip() for name in allowed_tools or ())
    return tuple(dict.fromkeys(name for name in normalized if name)) or None
