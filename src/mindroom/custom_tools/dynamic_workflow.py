"""Dynamic Workflow tools for MindRoom agents."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import nio
from agno.agent import Agent
from agno.tools import Toolkit

from mindroom import model_loading
from mindroom.authorization import responder_candidate_entities_from_cached_room
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.dynamic_workflows.service import DynamicWorkflowService
from mindroom.dynamic_workflows.store import DynamicWorkflowError, DynamicWorkflowStore
from mindroom.entity_resolution import entity_identity_registry
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
    tool_runtime_context,
)

if TYPE_CHECKING:
    from mindroom.dynamic_workflows.runner import ParticipantExecutor


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
            service = DynamicWorkflowService(store, participant_executor=_participant_executor(context))
            run = service.run_workflow(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
                input_data=input,
                requested_by=context.agent_name,
                base_url=context.runtime_paths.env_value("MINDROOM_PUBLIC_URL"),
                background=True,
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
    if scope in {"room", "tenant"}:
        msg = f"{scope} scope requires Dynamic Workflow approval policy and is not available to agent tools yet."
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


def _participant_executor(context: ToolRuntimeContext) -> ParticipantExecutor:
    def execute(
        *,
        participant: dict[str, object],
        prompt: str,
        input_data: dict[str, object],
        step_outputs: dict[str, object],
    ) -> object:
        del input_data, step_outputs
        return _execute_participant(context, participant, prompt)

    return execute


def _execute_participant(context: ToolRuntimeContext, participant: dict[str, object], prompt: str) -> object:
    participant_kind = str(participant.get("kind", "ephemeral_agent")).strip() or "ephemeral_agent"
    if participant_kind == "room_agent":
        return _execute_room_agent_participant(context, participant, prompt)
    if participant_kind == "ephemeral_agent":
        return _execute_ephemeral_agent_participant(context, participant, prompt)
    msg = f"Unsupported Dynamic Workflow participant kind '{participant_kind}'."
    raise DynamicWorkflowError(msg)


def _execute_room_agent_participant(
    context: ToolRuntimeContext,
    participant: dict[str, object],
    prompt: str,
) -> object:
    raw_agent_name = participant.get("agent") or participant.get("agent_name")
    if not isinstance(raw_agent_name, str) or not raw_agent_name.strip():
        msg = "Room agent participants must declare an 'agent' field."
        raise DynamicWorkflowError(msg)
    agent_name = raw_agent_name.strip()
    if agent_name not in context.config.agents:
        msg = f"Dynamic Workflow participant references unknown room agent '{agent_name}'."
        raise DynamicWorkflowError(msg)
    if agent_name not in _available_room_agent_names(context):
        msg = f"Dynamic Workflow room agent participant '{agent_name}' is not available to this requester in this room."
        raise DynamicWorkflowError(msg)
    if participant.get("model") not in (None, ""):
        msg = "Room agent participants use their configured model; model overrides are only available to ephemeral agents."
        raise DynamicWorkflowError(msg)

    agent_config = context.config.get_agent(agent_name)
    active_model_name = agent_config.model
    execution_identity = build_execution_identity_from_runtime_context(context)
    session_id = _participant_session_id(context, agent_name)
    # Imported lazily to avoid the create_agent -> dynamic_workflow toolkit cycle.
    from mindroom.agents import create_agent  # noqa: PLC0415

    agent = create_agent(
        agent_name,
        context.config,
        context.runtime_paths,
        execution_identity=execution_identity,
        session_id=session_id,
        hook_registry=context.hook_registry,
        active_model_name=active_model_name,
        include_interactive_questions=False,
    )
    return _run_agent(context, agent, prompt, session_id)


def _available_room_agent_names(context: ToolRuntimeContext) -> set[str]:
    room = context.room or nio.MatrixRoom(room_id=context.room_id, own_user_id="")
    candidates = responder_candidate_entities_from_cached_room(
        room,
        context.requester_id,
        context.config,
        context.runtime_paths,
    )
    registry = entity_identity_registry(context.config, context.runtime_paths)
    names: set[str] = {context.agent_name}
    for candidate in candidates:
        name = registry.current_entity_name_for_user_id(candidate.full_id, include_router=False)
        if name in context.config.agents:
            names.add(name)
    return names


def _execute_ephemeral_agent_participant(
    context: ToolRuntimeContext,
    participant: dict[str, object],
    prompt: str,
) -> object:
    tools = participant.get("tools", [])
    if tools not in (None, []):
        msg = "Ephemeral Dynamic Workflow agents cannot use tools; use room_agent participants for configured tools."
        raise DynamicWorkflowError(msg)
    participant_id = _required_participant_text(participant, "id")
    model_name = _resolve_participant_model_name(
        context,
        participant.get("model"),
        default_model=context.active_model_name or "default",
    )
    execution_identity = build_execution_identity_from_runtime_context(context)
    model = model_loading.get_model_instance(context.config, context.runtime_paths, model_name, execution_identity)
    agent = Agent(
        id=f"dynamic_workflow_{participant_id}",
        name=str(participant.get("name") or participant_id),
        role=str(participant.get("role") or participant.get("description") or "Dynamic Workflow participant."),
        model=model,
        tools=[],
        instructions=_participant_instructions(participant),
        markdown=True,
        telemetry=False,
    )
    return _run_agent(context, agent, prompt, _participant_session_id(context, participant_id))


def _run_agent(context: ToolRuntimeContext, agent: Agent, prompt: str, session_id: str) -> object:
    async def run() -> object:
        response = await agent.arun(
            prompt,
            user_id=context.requester_id,
            session_id=session_id,
        )
        return response.content if response.content is not None else ""

    with tool_runtime_context(context):
        return asyncio.run(run())


def _participant_session_id(context: ToolRuntimeContext, participant_id: str) -> str:
    base_session_id = context.session_id or context.resolved_thread_id or context.thread_id or context.room_id
    return f"{base_session_id}:dynamic_workflow:{participant_id}"


def _resolve_participant_model_name(
    context: ToolRuntimeContext,
    raw_model: object,
    *,
    default_model: str,
) -> str:
    if raw_model is None:
        return default_model
    if not isinstance(raw_model, str) or not raw_model.strip():
        msg = "Dynamic Workflow participant model must be a non-empty string."
        raise DynamicWorkflowError(msg)
    model_ref = raw_model.strip()
    if model_ref in context.config.models:
        return model_ref
    for model_name, model_config in context.config.models.items():
        if model_config.id == model_ref:
            return model_name
    msg = f"Dynamic Workflow participant model '{model_ref}' is not allowlisted in config.models."
    raise DynamicWorkflowError(msg)


def _participant_instructions(participant: dict[str, object]) -> list[str]:
    raw_instructions = participant.get("instructions", [])
    if raw_instructions is None:
        return []
    if isinstance(raw_instructions, str):
        return [raw_instructions]
    if isinstance(raw_instructions, list):
        return [str(instruction) for instruction in raw_instructions]
    msg = "Dynamic Workflow participant instructions must be a string or list."
    raise DynamicWorkflowError(msg)


def _required_participant_text(participant: dict[str, object], field_name: str) -> str:
    value = participant.get(field_name)
    if not isinstance(value, str) or not value.strip():
        msg = f"Dynamic Workflow participant field '{field_name}' must be a non-empty string."
        raise DynamicWorkflowError(msg)
    return value.strip()
