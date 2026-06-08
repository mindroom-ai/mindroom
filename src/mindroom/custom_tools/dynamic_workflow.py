"""Dynamic Workflow tools for MindRoom agents."""

from __future__ import annotations

from typing import Any

from agno.tools import Toolkit

from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.dynamic_workflows.store import DynamicWorkflowError, DynamicWorkflowStore
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context


class DynamicWorkflowTools(Toolkit):
    """Tools that let an agent create, update, inspect, and run Dynamic Workflows."""

    def __init__(self) -> None:
        super().__init__(
            name="dynamic_workflow",
            tools=[
                self.create_workflow,
                self.validate_workflow,
                self.update_workflow,
                self.run_workflow,
                self.get_workflow_run,
                self.list_workflows,
                self.list_workflow_revisions,
            ],
        )

    @staticmethod
    def _payload(status: str, **fields: object) -> str:
        return custom_tool_payload("dynamic_workflow", status, **fields)

    @classmethod
    def _context_error(cls) -> str:
        return cls._payload(
            "error",
            message="Dynamic Workflow tool context is unavailable in this runtime path.",
        )

    def create_workflow(
        self,
        spec: dict[str, Any],
        scope: str = "agent",
        reason: str | None = None,
    ) -> str:
        """Create a Dynamic Workflow from a declarative workflow spec."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()
        try:
            store, owner_id = _store_and_owner(context, scope)
            summary = store.create_workflow(
                spec=spec,
                scope=scope,
                owner_id=owner_id,
                created_by=context.agent_name,
                reason=reason,
            )
        except DynamicWorkflowError as exc:
            return self._payload("error", message=str(exc))
        return self._payload(
            "ok",
            workflow_id=summary.workflow_id,
            scope=summary.scope,
            owner_id=summary.owner_id,
            active_revision=summary.active_revision,
            name=summary.name,
        )

    def validate_workflow(self, spec: dict[str, Any]) -> str:
        """Validate a declarative Dynamic Workflow spec without saving it."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()
        try:
            validated = _store(context).validate_workflow(spec)
        except DynamicWorkflowError as exc:
            return self._payload("error", message=str(exc))
        return self._payload("ok", workflow_id=validated["id"], name=validated["name"])

    def update_workflow(
        self,
        workflow_id: str,
        patch: dict[str, Any],
        reason: str,
        scope: str = "agent",
    ) -> str:
        """Create and publish a new Dynamic Workflow revision from a patch."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()
        try:
            store, owner_id = _store_and_owner(context, scope)
            summary = store.update_workflow(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
                patch=patch,
                updated_by=context.agent_name,
                reason=reason,
            )
        except DynamicWorkflowError as exc:
            return self._payload("error", workflow_id=workflow_id, message=str(exc))
        return self._payload(
            "ok",
            workflow_id=summary.workflow_id,
            scope=summary.scope,
            owner_id=summary.owner_id,
            active_revision=summary.active_revision,
            name=summary.name,
        )

    def run_workflow(
        self,
        workflow_id: str,
        input: dict[str, Any],  # noqa: A002
        scope: str = "agent",
    ) -> str:
        """Run a Dynamic Workflow and persist step outputs plus report artifacts."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()
        try:
            store, owner_id = _store_and_owner(context, scope)
            run = store.run_workflow(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
                input_data=input,
                requested_by=context.agent_name,
                base_url=context.runtime_paths.env_value("MINDROOM_PUBLIC_URL"),
            )
        except DynamicWorkflowError as exc:
            return self._payload("error", workflow_id=workflow_id, message=str(exc))
        return self._payload(
            run.status,
            workflow_id=run.workflow_id,
            run_id=run.run_id,
            revision=run.revision,
            report_url=run.report_url,
            artifacts=run.artifacts,
            outputs=run.outputs,
            error=run.error,
            step_count=len(run.steps),
        )

    def get_workflow_run(
        self,
        workflow_id: str,
        run_id: str,
        scope: str = "agent",
    ) -> str:
        """Read one Dynamic Workflow run record."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()
        try:
            store, owner_id = _store_and_owner(context, scope)
            run = store.get_workflow_run(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
                run_id=run_id,
            )
        except DynamicWorkflowError as exc:
            return self._payload("error", workflow_id=workflow_id, run_id=run_id, message=str(exc))
        return self._payload(
            run.status,
            workflow_id=run.workflow_id,
            run_id=run.run_id,
            revision=run.revision,
            report_url=run.report_url,
            artifacts=run.artifacts,
            outputs=run.outputs,
            error=run.error,
            steps=run.steps,
        )

    def list_workflows(self, scope: str = "agent") -> str:
        """List Dynamic Workflows available in one scope."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()
        try:
            store, owner_id = _store_and_owner(context, scope)
            workflows = store.list_workflows(scope=scope, owner_id=owner_id)
        except DynamicWorkflowError as exc:
            return self._payload("error", message=str(exc))
        return self._payload(
            "ok",
            scope=scope,
            owner_id=owner_id,
            workflows=[
                {
                    "workflow_id": workflow.workflow_id,
                    "active_revision": workflow.active_revision,
                    "name": workflow.name,
                    "description": workflow.description,
                    "updated_at": workflow.updated_at,
                }
                for workflow in workflows
            ],
        )

    def list_workflow_revisions(self, workflow_id: str, scope: str = "agent") -> str:
        """List immutable revisions for one Dynamic Workflow."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()
        try:
            store, owner_id = _store_and_owner(context, scope)
            revisions = store.list_workflow_revisions(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
            )
        except DynamicWorkflowError as exc:
            return self._payload("error", workflow_id=workflow_id, message=str(exc))
        return self._payload("ok", workflow_id=workflow_id, revisions=revisions)


def _store(context: ToolRuntimeContext) -> DynamicWorkflowStore:
    return DynamicWorkflowStore(context.runtime_paths.storage_root)


def _store_and_owner(context: ToolRuntimeContext, scope: str) -> tuple[DynamicWorkflowStore, str]:
    if not context.agent_name:
        msg = "Agent name is missing in the tool runtime context."
        raise DynamicWorkflowError(msg)
    return _store(context), _owner_id(context, scope)


def _owner_id(context: ToolRuntimeContext, scope: str) -> str:
    if scope == "agent":
        return context.agent_name
    if scope == "room":
        if not context.room_id:
            msg = "Room ID is missing in the tool runtime context."
            raise DynamicWorkflowError(msg)
        return context.room_id
    if scope == "tenant":
        return "tenant"
    msg = f"Unsupported Dynamic Workflow scope '{scope}'."
    raise DynamicWorkflowError(msg)
