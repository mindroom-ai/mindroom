"""Shared Dynamic Workflow helpers for runtime-context-aware tools."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from mindroom.dynamic_workflows.spec import DynamicWorkflowError
from mindroom.dynamic_workflows.store import DynamicWorkflowRun, DynamicWorkflowStore
from mindroom.runtime_resolution import resolve_agent_execution

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def dynamic_workflow_store_and_owner(
    context: ToolRuntimeContext,
    scope: str,
) -> tuple[DynamicWorkflowStore, str]:
    """Return the Dynamic Workflow store and caller-visible owner ID."""
    if not context.agent_name:
        msg = "Agent name is missing in the tool runtime context."
        raise DynamicWorkflowError(msg)
    if scope in {"room", "tenant"}:
        msg = f"{scope} scope requires Dynamic Workflow approval policy and is not available to agent tools yet."
        raise DynamicWorkflowError(msg)
    store = DynamicWorkflowStore(context.runtime_paths.storage_root)
    if scope != "agent":
        msg = f"Unsupported Dynamic Workflow scope '{scope}'."
        raise DynamicWorkflowError(msg)
    return store, _agent_scope_owner_id(context)


def authorize_dynamic_workflow_run(context: ToolRuntimeContext, run: DynamicWorkflowRun) -> None:
    """Require the current requester to match the run requester."""
    if run.requested_by != context.requester_id:
        msg = "Dynamic Workflow run is not available to the current requester."
        raise DynamicWorkflowError(msg)


def _agent_scope_owner_id(context: ToolRuntimeContext) -> str:
    resolved_execution = resolve_agent_execution(
        context.agent_name,
        context.config,
        context.runtime_paths,
        execution_identity=context.execution_identity,
    )
    if not resolved_execution.policy.private_workspace_enabled:
        return context.agent_name
    if resolved_execution.worker_key is None:
        msg = f"Private agent '{context.agent_name}' could not resolve a Dynamic Workflow owner scope."
        raise DynamicWorkflowError(msg)
    digest = hashlib.sha256(f"{context.agent_name}\0{resolved_execution.worker_key}".encode()).hexdigest()[:24]
    return f"private_{digest}"
