"""Shared Dynamic Workflow helpers for runtime-context-aware tools."""

from __future__ import annotations

import hashlib

from mindroom.dynamic_workflows.store import DynamicWorkflowError, DynamicWorkflowRun, DynamicWorkflowStore
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.tool_system.runtime_context import ToolRuntimeContext, build_execution_identity_from_runtime_context


def dynamic_workflow_store(context: ToolRuntimeContext) -> DynamicWorkflowStore:
    """Return the Dynamic Workflow store for the current tool runtime."""
    return DynamicWorkflowStore(context.runtime_paths.storage_root)


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
    return dynamic_workflow_store(context), _dynamic_workflow_owner_id(context, scope)


def _dynamic_workflow_owner_id(context: ToolRuntimeContext, scope: str) -> str:
    """Resolve the owner ID for a Dynamic Workflow scope."""
    if scope == "agent":
        return _agent_scope_owner_id(context)
    if scope == "room":
        if not context.room_id:
            msg = "Room ID is missing in the tool runtime context."
            raise DynamicWorkflowError(msg)
        return context.room_id
    if scope == "tenant":
        return "tenant"
    msg = f"Unsupported Dynamic Workflow scope '{scope}'."
    raise DynamicWorkflowError(msg)


def authorize_dynamic_workflow_run(context: ToolRuntimeContext, run: DynamicWorkflowRun) -> None:
    """Require the current requester to match the run requester."""
    if run.requested_by != context.requester_id:
        msg = "Dynamic Workflow run is not available to the current requester."
        raise DynamicWorkflowError(msg)


def _agent_scope_owner_id(context: ToolRuntimeContext) -> str:
    execution_identity = build_execution_identity_from_runtime_context(context)
    resolved_execution = resolve_agent_execution(
        context.agent_name,
        context.config,
        execution_identity=execution_identity,
    )
    if not resolved_execution.policy.private_workspace_enabled:
        return context.agent_name
    if resolved_execution.worker_key is None:
        msg = f"Private agent '{context.agent_name}' could not resolve a Dynamic Workflow owner scope."
        raise DynamicWorkflowError(msg)
    digest = hashlib.sha256(f"{context.agent_name}\0{resolved_execution.worker_key}".encode()).hexdigest()[:24]
    return f"private_{digest}"
