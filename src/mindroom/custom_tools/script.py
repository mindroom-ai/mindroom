"""Primary-only background Python script controls."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.script_runs.manager import ScriptRunLimits, ScriptRunManagerError
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.workers.models import ScriptResourceProfileName  # noqa: TC001 - the tool schema reads it at runtime.

if TYPE_CHECKING:
    from mindroom.script_runs.manager import ScriptRunManager
    from mindroom.script_runs.models import ScriptRunRecord
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

__all__ = ["ScriptTools"]


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
            tools=[
                self.start_script,
                self.get_script_resource_profiles,
                self.get_script,
                self.cancel_script,
                self.list_scripts,
            ],
        )

    async def get_script_resource_profiles(self) -> str:
        """Return the exact CPU and memory reservations available to background scripts."""
        resolved = _runtime()
        if isinstance(resolved, str):
            return resolved
        manager, _ = resolved
        return _payload("ok", action="resource_profiles", **manager.resource_profiles())

    async def start_script(
        self,
        source: str | None = None,
        path: str | None = None,
        name: str | None = None,
        resource_profile: ScriptResourceProfileName | None = None,
    ) -> str:
        """Run a Python script in the background.

        Provide exactly one of `source` or `path`.
        A path is relative to this agent's workspace and is snapshotted before execution.

        Args:
            source: Inline Python source code.
            path: Workspace-relative path to a Python source file.
            name: Optional short label shown by status and list operations.
            resource_profile: Optional bounded Kubernetes profile. Call get_script_resource_profiles to inspect the
                exact resources, or omit it to use the configured default.

        """
        resolved = _runtime()
        if isinstance(resolved, str):
            return resolved
        manager, context = resolved
        try:
            run = await manager.run(
                context,
                source=source,
                path=path,
                name=name,
                resource_profile=resource_profile,
                limits=self.limits,
            )
        except ScriptRunManagerError as exc:
            return _payload("error", message=str(exc))
        return _payload("ok", action="run", run=_public_run(run))

    async def get_script(self, run_id: str) -> str:
        """Return durable state and recent process output for one owned script.

        Args:
            run_id: Identifier returned by `start_script`.

        """
        resolved = _runtime()
        if isinstance(resolved, str):
            return resolved
        manager, context = resolved
        try:
            status = await manager.status(context, run_id=run_id)
        except ScriptRunManagerError as exc:
            return _payload("error", message=str(exc))
        return _payload("ok", action="status", run=_public_run(status.run), output=status.output)

    async def cancel_script(self, run_id: str, force: bool = False) -> str:
        """Cancel one owned background script after revoking its tool capability.

        Args:
            run_id: Identifier returned by `start_script`.
            force: Force-kill the supervised process instead of requesting graceful termination.

        """
        resolved = _runtime()
        if isinstance(resolved, str):
            return resolved
        manager, context = resolved
        try:
            run = await manager.cancel(context, run_id=run_id, force=force)
        except ScriptRunManagerError as exc:
            return _payload("error", message=str(exc))
        return _payload("ok", action="cancel", run=_public_run(run))

    async def list_scripts(self, include_finished: bool = True) -> str:
        """List background scripts owned by this requester and agent.

        Args:
            include_finished: Include terminal runs in addition to active runs.

        """
        resolved = _runtime()
        if isinstance(resolved, str):
            return resolved
        manager, context = resolved
        runs = await manager.list(context, include_finished=include_finished)
        return _payload("ok", action="list", runs=[_public_run(run) for run in runs])


def _runtime() -> tuple[ScriptRunManager, ToolRuntimeContext] | str:
    context = get_tool_runtime_context()
    if context is None:
        return _payload("error", message="Background script controls require an active room context.")
    manager = None if context.orchestrator is None else context.orchestrator.script_runtime.tool_manager
    if manager is None:
        return _payload("error", message="Background script runtime is not ready.")
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
