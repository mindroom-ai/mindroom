"""Dynamic Workflow tools for MindRoom agents."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import nio
from agno.run.agent import RunOutput, RunStatus
from agno.tools import Toolkit

from mindroom.authorization import responder_candidate_entities_from_cached_room
from mindroom.credentials import get_runtime_credentials_manager, load_scoped_credentials
from mindroom.custom_tools.dynamic_workflow_context import (
    authorize_dynamic_workflow_run,
    dynamic_workflow_store,
    dynamic_workflow_store_and_owner,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.custom_tools.toolkit_functions import JSON_OBJECT_SCHEMA, register_toolkit_functions
from mindroom.delegation.direct import run_direct_child_turn
from mindroom.delegation.lifecycle import finish_child_turn, prepare_child_turn
from mindroom.delegation.personas import (
    PersonaError,
    PersonaRequest,
    caller_toolkit_names,
    declared_function_names,
    inline_persona,
    load_profile,
    missing_persona_tool,
    persona_allows,
    require_minimal_shell,
    validate_persona_tools,
)
from mindroom.delegation.sessions import SubagentSessionError
from mindroom.dynamic_workflows.runner import DynamicWorkflowExecutionError, ParticipantOutput
from mindroom.dynamic_workflows.service import DynamicWorkflowService
from mindroom.dynamic_workflows.validation import (
    DynamicWorkflowError,
    collect_workflow_spec_errors,
    require_granted_participant_tools,
)
from mindroom.entity_resolution import entity_identity_registry
from mindroom.helper_usage import get_helper_usage_owner, record_helper_usage
from mindroom.response_turn import ResponsePausedForApproval
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_approval import tool_may_require_approval
from mindroom.tool_system.automation_approval import NEVER_PREAPPROVE_TOOLKITS, build_automation_approval_config
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
    tool_runtime_context,
)

if TYPE_CHECKING:
    from agno.agent import Agent

    from mindroom.agent_modes import AgentMode
    from mindroom.config.main import Config
    from mindroom.delegation.state import DelegationChild, SubagentPersona
    from mindroom.dynamic_workflows.runner import AsyncParticipantExecutor, ParticipantExecutor

# Agent-infrastructure toolkits that are built outside the tool registry and presume
# a durable agent runtime; they can never be granted to workflow participants.
_WORKFLOW_RESTRICTED_TOOLS = frozenset(
    {
        "compact_context",
        "delegate",
        "dynamic_tools",
        "dynamic_workflow",
        "invite_router",
        "memory",
        "self_config",
        "skill_manage",
        # Switching the thread model would escape the participant's frozen, permitted model.
        "thread_model",
    },
)

# Each participant's validated persona request, frozen when a run starts.
type _ResolvedParticipants = dict[str, PersonaRequest]

_MINIMAL_SPEC_EXAMPLE = (
    '{"schema_version": 1, "kind": "workflow", "id": "my_flow", "name": "My Flow", '
    '"participants": [{"id": "writer", "system_prompt": "You write short poems."}], '
    '"workflow": [{"id": "draft", "participant": "writer", "prompt": "Write a haiku about {input.topic}."}]}'
)

_SPEC_PARAMETER_DESCRIPTION = (
    "Declarative workflow spec. Minimal valid example: "
    f"{_MINIMAL_SPEC_EXAMPLE} "
    "Required fields: schema_version (must be 1), kind (must be 'workflow'), id, name, "
    "participants (list of {id, system_prompt or profile, ...}), workflow (list of steps such as "
    "{id, participant, prompt})."
)

_TOOL_DESCRIPTIONS = {
    "create_workflow": (
        "Create a Dynamic Workflow from a declarative workflow spec. "
        f"Minimal valid spec: {_MINIMAL_SPEC_EXAMPLE} "
        "Subagent participants are authored copies of you: each sets its entire system_prompt, or a profile "
        "saved as subagents/<name>.md in your workspace, plus tools, model, and mode. A participant gets only "
        "the tools it names from your own toolkits (toolkit or toolkit.function), so list every toolkit it needs. "
        "Participants cannot pause for approval, so every toolkit a participant names must be pre-approved by the "
        "dynamic_workflow allowed_tools config. room_agent participants run another agent available in this room "
        "without tools."
    ),
    "validate_workflow": (
        "Validate a declarative Dynamic Workflow spec without saving it. "
        "Reports every detected validation error in one call. "
        f"Minimal valid spec: {_MINIMAL_SPEC_EXAMPLE}"
    ),
    "update_workflow": "Create and publish a new Dynamic Workflow revision from a patch.",
    "run_workflow": "Run a Dynamic Workflow and persist step outputs plus report artifacts.",
    "get_workflow_run": "Read one Dynamic Workflow run record.",
    "list_workflows": "List Dynamic Workflows available in one scope.",
    "list_workflow_revisions": "List immutable revisions for one Dynamic Workflow.",
}


_TOOL_PARAMETERS: dict[str, dict[str, object]] = {
    "create_workflow": {
        "type": "object",
        "properties": {
            "spec": {**JSON_OBJECT_SCHEMA, "description": _SPEC_PARAMETER_DESCRIPTION},
            "scope": {"type": "string"},
            "reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["spec"],
    },
    "validate_workflow": {
        "type": "object",
        "properties": {"spec": {**JSON_OBJECT_SCHEMA, "description": _SPEC_PARAMETER_DESCRIPTION}},
        "required": ["spec"],
    },
    "update_workflow": {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string"},
            "patch": JSON_OBJECT_SCHEMA,
            "reason": {"type": "string"},
            "scope": {"type": "string"},
        },
        "required": ["workflow_id", "patch", "reason"],
    },
    "run_workflow": {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string"},
            "input": JSON_OBJECT_SCHEMA,
            "scope": {"type": "string"},
        },
        "required": ["workflow_id", "input"],
    },
    "get_workflow_run": {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string"},
            "run_id": {"type": "string"},
            "scope": {"type": "string"},
        },
        "required": ["workflow_id", "run_id"],
    },
    "list_workflows": {
        "type": "object",
        "properties": {"scope": {"type": "string"}},
    },
    "list_workflow_revisions": {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string"},
            "scope": {"type": "string"},
        },
        "required": ["workflow_id"],
    },
}


class DynamicWorkflowTools(Toolkit):
    """Tools that let an agent create, update, inspect, and run Dynamic Workflows."""

    def __init__(self) -> None:
        super().__init__(name="dynamic_workflow", tools=[])
        self._register_functions()

    def _register_functions(self) -> None:
        register_toolkit_functions(
            self,
            sync_entrypoints={
                "create_workflow": self.create_workflow,
                "validate_workflow": self.validate_workflow,
                "update_workflow": self.update_workflow,
                "run_workflow": self.run_workflow,
                "get_workflow_run": self.get_workflow_run,
                "list_workflows": self.list_workflows,
                "list_workflow_revisions": self.list_workflow_revisions,
            },
            async_entrypoints={
                "create_workflow": self.acreate_workflow,
                "validate_workflow": self.avalidate_workflow,
                "update_workflow": self.aupdate_workflow,
                "run_workflow": self.arun_workflow,
                "get_workflow_run": self.aget_workflow_run,
                "list_workflows": self.alist_workflows,
                "list_workflow_revisions": self.alist_workflow_revisions,
            },
            descriptions=_TOOL_DESCRIPTIONS,
            parameters=_TOOL_PARAMETERS,
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

    @classmethod
    def _spec_errors_payload(cls, spec_errors: list[str]) -> str:
        return cls._payload(
            "error",
            message="\n".join(spec_errors),
            errors=spec_errors,
            minimal_valid_spec_example=_MINIMAL_SPEC_EXAMPLE,
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
        if spec_errors := collect_workflow_spec_errors(spec):
            return self._spec_errors_payload(spec_errors)
        try:
            store, owner_id = dynamic_workflow_store_and_owner(context, scope)
            _validate_workflow_policy_for_context(context, spec)
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
        if spec_errors := collect_workflow_spec_errors(spec):
            return self._spec_errors_payload(spec_errors)
        try:
            _validate_workflow_policy_for_context(context, spec)
            validated = dynamic_workflow_store(context).validate_workflow(spec)
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
            store, owner_id = dynamic_workflow_store_and_owner(context, scope)
            summary = store.update_workflow(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
                patch=patch,
                updated_by=context.agent_name,
                reason=reason,
                spec_validator=lambda spec: _validate_workflow_policy_for_context(context, spec),
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
        resolved: _ResolvedParticipants = {}
        try:
            store, owner_id = dynamic_workflow_store_and_owner(context, scope)
            service = DynamicWorkflowService(
                store,
                participant_executor=_participant_executor(context, workflow_id, resolved),
                spec_validator=lambda spec: _validate_workflow_policy_for_context(context, spec, resolved),
            )
            run = service.run_workflow(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
                input_data=input,
                requested_by=context.requester_id,
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
            store, owner_id = dynamic_workflow_store_and_owner(context, scope)
            run = store.get_workflow_run(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
                run_id=run_id,
            )
            authorize_dynamic_workflow_run(context, run)
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
            store, owner_id = dynamic_workflow_store_and_owner(context, scope)
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
            store, owner_id = dynamic_workflow_store_and_owner(context, scope)
            revisions = store.list_workflow_revisions(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
            )
        except DynamicWorkflowError as exc:
            return self._payload("error", workflow_id=workflow_id, message=str(exc))
        return self._payload("ok", workflow_id=workflow_id, revisions=revisions)

    async def acreate_workflow(
        self,
        spec: dict[str, Any],
        scope: str = "agent",
        reason: str | None = None,
    ) -> str:
        """Create a Dynamic Workflow from a declarative workflow spec."""
        return self.create_workflow(spec, scope=scope, reason=reason)

    async def avalidate_workflow(self, spec: dict[str, Any]) -> str:
        """Validate a declarative Dynamic Workflow spec without saving it."""
        return self.validate_workflow(spec)

    async def aupdate_workflow(
        self,
        workflow_id: str,
        patch: dict[str, Any],
        reason: str,
        scope: str = "agent",
    ) -> str:
        """Create and publish a new Dynamic Workflow revision from a patch."""
        return self.update_workflow(workflow_id, patch, reason, scope=scope)

    async def arun_workflow(
        self,
        workflow_id: str,
        input: dict[str, Any],  # noqa: A002
        scope: str = "agent",
    ) -> str:
        """Run a Dynamic Workflow and persist step outputs plus report artifacts."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()
        resolved: _ResolvedParticipants = {}
        try:
            store, owner_id = dynamic_workflow_store_and_owner(context, scope)
            service = DynamicWorkflowService(
                store,
                async_participant_executor=_aparticipant_executor(context, workflow_id, resolved),
                spec_validator=lambda spec: _validate_workflow_policy_for_context(context, spec, resolved),
            )
            run = await service.arun_workflow(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
                input_data=input,
                requested_by=context.requester_id,
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

    async def aget_workflow_run(
        self,
        workflow_id: str,
        run_id: str,
        scope: str = "agent",
    ) -> str:
        """Read one Dynamic Workflow run record."""
        return self.get_workflow_run(workflow_id, run_id, scope=scope)

    async def alist_workflows(self, scope: str = "agent") -> str:
        """List Dynamic Workflows available in one scope."""
        return self.list_workflows(scope=scope)

    async def alist_workflow_revisions(self, workflow_id: str, scope: str = "agent") -> str:
        """List immutable revisions for one Dynamic Workflow."""
        return self.list_workflow_revisions(workflow_id, scope=scope)


def _participant_executor(
    context: ToolRuntimeContext,
    workflow_id: str,
    resolved: _ResolvedParticipants,
) -> ParticipantExecutor:
    run_scope = f"{workflow_id}:{uuid4().hex}"
    children: dict[str, DelegationChild] = {}
    approvals: dict[str, dict[str, frozenset[str]]] = {}

    def execute(
        *,
        participant: dict[str, object],
        prompt: str,
        input_data: dict[str, object],
        step_outputs: dict[str, object],
    ) -> ParticipantOutput:
        del input_data, step_outputs
        return asyncio.run(
            _aexecute_participant(
                context,
                participant,
                prompt,
                run_scope=run_scope,
                children=children,
                resolved=resolved,
                approvals=approvals,
            ),
        )

    return execute


def _aparticipant_executor(
    context: ToolRuntimeContext,
    workflow_id: str,
    resolved: _ResolvedParticipants,
) -> AsyncParticipantExecutor:
    run_scope = f"{workflow_id}:{uuid4().hex}"
    children: dict[str, DelegationChild] = {}
    approvals: dict[str, dict[str, frozenset[str]]] = {}

    async def execute(
        *,
        participant: dict[str, object],
        prompt: str,
        input_data: dict[str, object],
        step_outputs: dict[str, object],
    ) -> ParticipantOutput:
        del input_data, step_outputs
        return await _aexecute_participant(
            context,
            participant,
            prompt,
            run_scope=run_scope,
            children=children,
            resolved=resolved,
            approvals=approvals,
        )

    return execute


async def _aexecute_participant(
    context: ToolRuntimeContext,
    participant: dict[str, object],
    prompt: str,
    *,
    run_scope: str,
    children: dict[str, DelegationChild],
    resolved: _ResolvedParticipants,
    approvals: dict[str, dict[str, frozenset[str]]],
) -> ParticipantOutput:
    participant_kind = str(participant.get("kind", "subagent")).strip() or "subagent"
    if participant_kind == "room_agent":
        return ParticipantOutput(
            await _aexecute_room_agent_participant(context, participant, prompt, run_scope=run_scope),
        )
    if participant_kind == "subagent":
        return await _aexecute_subagent_participant(
            context,
            participant,
            prompt,
            children=children,
            resolved=resolved,
            approvals=approvals,
        )
    msg = f"Unsupported Dynamic Workflow participant kind '{participant_kind}'."
    raise DynamicWorkflowError(msg)


async def _aexecute_room_agent_participant(
    context: ToolRuntimeContext,
    participant: dict[str, object],
    prompt: str,
    *,
    run_scope: str = "manual",
) -> object:
    context = replace(context, config=context.current_config, config_provider=None)
    agent_name = _validate_room_agent_reference_for_context(context, participant)
    participant_id = _required_participant_text(participant, "id")
    runtime_model = context.config.resolve_runtime_model(
        entity_name=agent_name,
        room_id=context.room_id,
        thread_id=context.resolved_thread_id,
        runtime_paths=context.runtime_paths,
    )
    active_model_name = runtime_model.model_name
    session_id = _participant_session_id(context, participant_id, run_scope=run_scope)
    participant_context = replace(
        context,
        agent_name=agent_name,
        active_model_name=active_model_name,
        target=replace(context.target, session_id=session_id),
    )
    execution_identity = build_execution_identity_from_runtime_context(participant_context)
    # Imported lazily to avoid the create_agent -> dynamic_workflow toolkit cycle.
    from mindroom.agents import create_agent  # noqa: PLC0415

    agent = create_agent(
        agent_name,
        context.config,
        context.runtime_paths,
        execution_identity=execution_identity,
        session_id=session_id,
        hook_registry=context.hook_registry,
        knowledge=None,
        active_model_name=active_model_name,
        include_interactive_questions=False,
        persist_runtime_state=False,
        disable_runtime_capabilities=True,
    )
    return await _arun_agent(participant_context, agent, prompt)


def _available_room_agent_names(context: ToolRuntimeContext) -> set[str]:
    context = replace(context, config=context.current_config, config_provider=None)
    room = _candidate_resolution_room(context)
    candidates = responder_candidate_entities_from_cached_room(
        room,
        context.requester_id,
        context.config,
        context.runtime_paths,
        context.require_agent_reply_memberships(),
    )
    registry = entity_identity_registry(context.config, context.runtime_paths)
    names: set[str] = {context.agent_name}
    for candidate in candidates:
        name = registry.current_entity_name_for_user_id(candidate.full_id, include_router=False)
        if name in context.config.agents:
            names.add(name)
    return names


def _candidate_resolution_room(context: ToolRuntimeContext) -> nio.MatrixRoom:
    if context.room is not None:
        return context.room
    rooms = context.client.rooms
    if isinstance(rooms, Mapping):
        room = rooms.get(context.room_id)
        if isinstance(room, nio.MatrixRoom):
            return room
    return nio.MatrixRoom(room_id=context.room_id, own_user_id="")


def _validate_room_agent_reference_for_context(
    context: ToolRuntimeContext,
    participant: dict[str, object],
) -> str:
    context = replace(context, config=context.current_config, config_provider=None)
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
        msg = "Room agent participants use their configured model; model overrides are only available to subagent participants."
        raise DynamicWorkflowError(msg)
    return agent_name


async def _aexecute_subagent_participant(
    context: ToolRuntimeContext,
    participant: dict[str, object],
    prompt: str,
    *,
    children: dict[str, DelegationChild],
    resolved: _ResolvedParticipants,
    approvals: dict[str, dict[str, frozenset[str]]],
) -> ParticipantOutput:
    """Run one step as a turn of this participant's subagent, an authored copy of the caller.

    The first step runs the persona validated when the run started, so a profile edited
    during the run cannot change its prompt, tools, or model.
    """
    context = replace(context, config=context.current_config, config_provider=None)
    participant_id = _required_participant_text(participant, "id")
    previous = children.get(participant_id)
    request = (
        resolved[participant_id]
        if previous is None
        else PersonaRequest(persona=previous.persona, model=None, agent_mode=previous.agent_mode)
    )
    persona = cast("SubagentPersona", request.persona)
    missing = missing_persona_tool(persona.tools, _participant_available_toolkits(context), _participant_cap(context))
    if missing is not None:
        msg = f"Dynamic Workflow participant '{participant_id}' tool '{missing}' is no longer available to you."
        raise DynamicWorkflowExecutionError(msg)
    # Recheck approvals against the current config each step; only the function ownership is cached.
    _reject_unapproved_participant_tools(context, persona.tools or (), allowed=_workflow_allowed_tools(context))
    if participant_id not in approvals:
        approvals[participant_id] = await _participant_function_owners(context, persona)
    approval_config = _participant_run_config(context, approvals[participant_id])
    owner = build_execution_identity_from_runtime_context(context)
    child = prepare_child_turn(
        context.agent_name,
        context.agent_name,
        prompt,
        owner=owner,
        config=context.config,
        runtime_paths=context.runtime_paths,
        depth=0,
        model=request.model,
        agent_mode=request.agent_mode,
        previous=previous,
        persona=persona,
    )
    try:
        result = await run_direct_child_turn(
            child,
            owner=owner,
            parent_run_id=None,
            config=context.config,
            runtime_paths=context.runtime_paths,
            refresh_scheduler=None,
            approval_config=approval_config,
        )
    except ResponsePausedForApproval as exc:
        await finish_child_turn(
            child,
            config=context.config,
            runtime_paths=context.runtime_paths,
            status="failed",
            reason="Dynamic Workflow participants cannot pause for approval.",
        )
        msg = f"Dynamic Workflow participant '{participant_id}' required approval and cannot pause."
        raise DynamicWorkflowExecutionError(msg, delegation_id=child.delegation_id) from exc
    except SubagentSessionError as exc:
        raise DynamicWorkflowExecutionError(str(exc)) from exc
    children[participant_id] = child
    if not result.completed:
        raise DynamicWorkflowExecutionError(result.text, delegation_id=child.delegation_id)
    return ParticipantOutput(result.text, child.delegation_id)


async def _participant_function_owners(
    context: ToolRuntimeContext,
    persona: SubagentPersona,
) -> dict[str, frozenset[str]]:
    """Map each function a participant selected to the toolkits that expose it.

    Declared toolkits are read from their metadata; only toolkits without declared functions,
    such as MCP servers, are built, off the event loop, to learn what they expose.
    """
    entries = persona.tools or ()
    names = sorted({entry.partition(".")[0] for entry in entries})
    functions = {name: declared for name in names if (declared := declared_function_names(name)) is not None}
    built = await asyncio.to_thread(
        _resolve_participant_toolkits,
        context,
        [name for name in names if name not in functions],
    )
    functions |= {name: (*toolkit.functions, *toolkit.async_functions) for name, toolkit in built.items()}
    return _selected_function_owners(entries, functions)


def _selected_function_owners(
    entries: tuple[str, ...],
    functions: Mapping[str, tuple[str, ...]],
) -> dict[str, frozenset[str]]:
    """Map each function the participant selected to every selected toolkit that exposes it."""
    owners: dict[str, set[str]] = {}
    for name, function_names in functions.items():
        for function_name in function_names:
            if persona_allows(entries, name, function_name):
                owners.setdefault(function_name, set()).add(name)
    return {function_name: frozenset(toolkits) for function_name, toolkits in owners.items()}


def _reject_unapproved_participant_tools(
    context: ToolRuntimeContext,
    entries: tuple[str, ...],
    *,
    allowed: frozenset[str],
) -> None:
    """A participant cannot pause, so every tool it names must run without approval.

    A whole toolkit needs a pre-approval through ``allowed_tools``; a single function may instead be
    auto-approved by an operator rule, and a named function an operator rule still gates fails the run.
    """
    for entry in entries:
        toolkit, separator, function = entry.partition(".")
        if not separator:
            if toolkit in NEVER_PREAPPROVE_TOOLKITS:
                msg = (
                    f"Dynamic Workflow participant tool '{entry}' always requires approval; "
                    "name only its functions an operator rule auto-approves."
                )
                raise DynamicWorkflowExecutionError(msg)
            if "*" not in allowed and toolkit not in allowed:
                msg = (
                    f"Dynamic Workflow participant tool '{entry}' is not pre-approved; add it to the "
                    "dynamic_workflow allowed_tools setting or name its auto-approved functions."
                )
                raise DynamicWorkflowExecutionError(msg)
            continue
        overlay = build_automation_approval_config(
            context.config,
            function_owners={function: frozenset({toolkit})},
            preapproved_toolkits=allowed,
            never_preapprove_toolkits=NEVER_PREAPPROVE_TOOLKITS,
        )
        if tool_may_require_approval(overlay, function):
            msg = f"Dynamic Workflow participant functions {function} require approval and cannot suspend for approval."
            raise DynamicWorkflowExecutionError(msg)


def _participant_cap(context: ToolRuntimeContext) -> tuple[str, ...] | None:
    """Return the calling authored subagent's tools that a participant may also use, if the caller is one."""
    if context.persona_tools is None:
        return None
    return tuple(entry for entry in context.persona_tools if entry.partition(".")[0] not in _WORKFLOW_RESTRICTED_TOOLS)


def _participant_available_toolkits(context: ToolRuntimeContext) -> list[str]:
    """Return the caller toolkits a participant may name; infrastructure toolkits never qualify."""
    return [
        name
        for name in caller_toolkit_names(context.agent_name, context.config, delegation_depth=0)
        if name not in _WORKFLOW_RESTRICTED_TOOLS
    ]


def _participant_request(
    context: ToolRuntimeContext,
    participant: dict[str, object],
    *,
    workflow_id: str,
) -> PersonaRequest:
    """Resolve one participant's persona, model, and mode; it uses only the tools it names."""
    participant_id = _required_participant_text(participant, "id")
    source_name = f"{workflow_id}/{participant_id}"
    available = _participant_available_toolkits(context)
    cap = _participant_cap(context)
    try:
        if participant.get("profile") is not None:
            workspace = resolve_agent_runtime(
                context.agent_name,
                context.config,
                context.runtime_paths,
                execution_identity=build_execution_identity_from_runtime_context(context),
            ).workspace
            profile = load_profile(
                workspace.root if workspace is not None else None,
                _required_participant_text(participant, "profile"),
            )
            persona = replace(profile.persona, source_kind="workflow", source_name=source_name)
            raw_model, mode = profile.model, profile.mode or "standard"
        else:
            persona = inline_persona(
                participant.get("system_prompt"),
                participant.get("tools"),
                source_kind="workflow",
                source_name=source_name,
            )
            raw_model, mode = participant.get("model"), cast("AgentMode", participant.get("mode") or "standard")
        persona = replace(persona, tools=persona.tools or ())
        validate_persona_tools(persona.tools, available, cap)
        require_minimal_shell(persona, mode)
    except PersonaError as exc:
        raise DynamicWorkflowError(str(exc)) from exc
    model = _resolve_participant_model_name(context, raw_model, default_model=_caller_runtime_model_name(context))
    return PersonaRequest(persona=persona, model=model, agent_mode=mode)


def _resolve_participant_toolkits(context: ToolRuntimeContext, tool_names: list[str]) -> dict[str, Toolkit]:
    """Build participant toolkits with the caller's tool routing to learn which functions they expose."""
    if not tool_names:
        return {}
    ensure_tool_registry_loaded(context.runtime_paths, context.config)
    _reject_unavailable_workflow_tools(tool_names)
    # Imported lazily to avoid the create_agent -> dynamic_workflow toolkit cycle.
    from mindroom.agents import build_agent_toolkit, resolve_runtime_worker_tools  # noqa: PLC0415

    execution_identity = build_execution_identity_from_runtime_context(context)
    worker_tools = resolve_runtime_worker_tools(
        context.agent_name,
        context.config,
        context.runtime_paths,
        list(tool_names),
        tool_registry_preloaded=True,
    )
    entity_view = context.config.resolve_entity(context.agent_name)
    authored_overrides = {entry.name: entry.tool_config_overrides for entry in entity_view.tool_configs}
    toolkits: dict[str, Toolkit] = {}
    for tool_name in tool_names:
        toolkit = build_agent_toolkit(
            tool_name,
            agent_name=context.agent_name,
            config=context.config,
            runtime_paths=context.runtime_paths,
            worker_tools=worker_tools,
            runtime_overrides=entity_view.tool_runtime_overrides(tool_name),
            tool_config_overrides=authored_overrides.get(tool_name),
            execution_identity=execution_identity,
            session_id=context.session_id,
        )
        if toolkit is None:
            msg = f"Dynamic Workflow participant tool '{tool_name}' is not available in this runtime."
            raise DynamicWorkflowError(msg)
        toolkits[tool_name] = toolkit
    return toolkits


def _reject_unavailable_workflow_tools(tool_names: list[str]) -> None:
    for tool_name in (entry.partition(".")[0] for entry in tool_names):
        if tool_name in _WORKFLOW_RESTRICTED_TOOLS:
            msg = f"Dynamic Workflow participants cannot use agent-infrastructure tool '{tool_name}'."
            raise DynamicWorkflowError(msg)
        if tool_name not in TOOL_METADATA:
            msg = f"Dynamic Workflow participant tool '{tool_name}' is not a registered tool."
            raise DynamicWorkflowError(msg)


def _participant_run_config(context: ToolRuntimeContext, function_owners: Mapping[str, frozenset[str]]) -> Config:
    """Return the participant policy: approval by default, with the caller's allowed tools pre-approved."""
    return build_automation_approval_config(
        context.config,
        function_owners=function_owners,
        preapproved_toolkits=_workflow_allowed_tools(context),
        never_preapprove_toolkits=NEVER_PREAPPROVE_TOOLKITS,
    )


def _workflow_allowed_tools(context: ToolRuntimeContext) -> frozenset[str]:
    """Resolve pre-approved workflow tool names from dashboard and authored tool config."""
    values: dict[str, object] = {}
    credentials_manager = get_runtime_credentials_manager(context.runtime_paths)
    persisted = load_scoped_credentials("dynamic_workflow", credentials_manager=credentials_manager, worker_target=None)
    if persisted:
        values.update(persisted)
    if context.agent_name in context.config.agents:
        # A dashboard save with this agent selected lands in its scoped store and overrides the global value.
        scoped = load_scoped_credentials(
            "dynamic_workflow",
            credentials_manager=credentials_manager,
            worker_target=context.resolve_worker_target(),
            primary_built_tool=True,
        )
        if scoped:
            values.update(scoped)
    for entry in context.config.resolve_entity(context.agent_name).tool_configs:
        if entry.name == "dynamic_workflow":
            values.update(entry.tool_config_overrides)
    raw_allowed = values.get("allowed_tools")
    if isinstance(raw_allowed, str):
        raw_allowed = [raw_allowed]
    if not isinstance(raw_allowed, list):
        return frozenset()
    return frozenset(tool.strip() for tool in raw_allowed if isinstance(tool, str) and tool.strip())


async def _arun_agent(context: ToolRuntimeContext, agent: Agent, prompt: str) -> object:
    # Stream the run: the participant inherits the caller's model and the workflow's runtime
    # budget, and the Anthropic/Vertex SDK refuses a non-streaming request whose budget could
    # exceed 10 minutes. Consuming the event stream drives tool calls; yield_run_output makes
    # the final RunOutput the last streamed item, which works without a db.
    final_output: RunOutput | None = None
    usage_owner = get_helper_usage_owner()
    invocation_id = uuid4().hex
    with tool_runtime_context(context):
        event_stream = agent.arun(
            prompt,
            run_id=invocation_id,
            user_id=context.requester_id,
            session_id=context.session_id,
            stream=True,
            stream_events=True,
            yield_run_output=True,
        )
        async for event in event_stream:
            if isinstance(event, RunOutput):
                final_output = event
                if usage_owner is not None and agent.db is None:
                    await record_helper_usage(
                        event,
                        owner=usage_owner,
                        invocation_id=invocation_id,
                        kind="dynamic_workflow",
                        requester_id=context.requester_id,
                    )
    if final_output is None:
        msg = "Dynamic Workflow participant run produced no output."
        raise DynamicWorkflowExecutionError(msg)
    content = final_output.content if final_output.content is not None else ""
    if final_output.status != RunStatus.completed:
        message = str(content) if content else f"Agent run ended with status {final_output.status.value}."
        raise DynamicWorkflowExecutionError(message)
    return content


def _participant_session_id(context: ToolRuntimeContext, participant_id: str, *, run_scope: str) -> str:
    return f"{context.session_id}:dynamic_workflow:{run_scope}:{participant_id}"


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


def _validate_workflow_policy_for_context(
    context: ToolRuntimeContext,
    spec: dict[str, object],
    resolved: _ResolvedParticipants | None = None,
) -> None:
    """Check participants against the caller's current policy, keeping each validated request in ``resolved``."""
    context = replace(context, config=context.current_config, config_provider=None)
    _validate_workflow_tool_policy_for_context(context, spec)
    permission_models = _workflow_permission_model_refs(context, spec)
    granted_tools = _workflow_permission_tools(spec)
    workflow_id = str(spec.get("id", ""))
    for participant in _workflow_participants(spec):
        if str(participant.get("kind", "subagent")).strip() == "room_agent":
            agent_name = _validate_room_agent_reference_for_context(context, participant)
            model_name = context.config.resolve_runtime_model(
                entity_name=agent_name,
                room_id=context.room_id,
                thread_id=context.resolved_thread_id,
                runtime_paths=context.runtime_paths,
            ).model_name
        else:
            request = _participant_request(context, participant, workflow_id=workflow_id)
            participant_id = _required_participant_text(participant, "id")
            named_tools = request.persona.tools if request.persona is not None else None
            require_granted_participant_tools(participant_id, named_tools or (), granted_tools)
            if resolved is not None:
                resolved[participant_id] = request
            model_name = cast("str", request.model)
        if permission_models and _model_refs(context, model_name).isdisjoint(permission_models):
            msg = (
                f"Dynamic Workflow participant model '{model_name}' is not allowed by permissions.models. "
                "Add the model to workflow permissions before running this revision."
            )
            raise DynamicWorkflowError(msg)


def _validate_workflow_tool_policy_for_context(context: ToolRuntimeContext, spec: dict[str, object]) -> None:
    """Reject tool grants that name unregistered or agent-infrastructure tools."""
    tool_names = _spec_tool_names(spec)
    if not tool_names:
        return
    ensure_tool_registry_loaded(context.runtime_paths, context.config)
    _reject_unavailable_workflow_tools(tool_names)


def _spec_tool_names(spec: dict[str, object]) -> list[str]:
    """Collect declared tool grant names from a possibly un-normalized spec."""
    tool_lists: list[object] = []
    raw_permissions = spec.get("permissions")
    if isinstance(raw_permissions, dict):
        tool_lists.append(cast("dict[str, object]", raw_permissions).get("tools"))
    tool_lists.extend(participant.get("tools") for participant in _workflow_participants(spec))
    tool_names: list[str] = []
    for raw_tools in tool_lists:
        if not isinstance(raw_tools, list):
            continue
        for raw_tool in raw_tools:
            if isinstance(raw_tool, str) and raw_tool.strip() and raw_tool.strip() not in tool_names:
                tool_names.append(raw_tool.strip())
    return tool_names


def _caller_runtime_model_name(context: ToolRuntimeContext) -> str:
    if context.active_model_name:
        return context.active_model_name
    return context.config.resolve_runtime_model(
        entity_name=context.agent_name,
        room_id=context.room_id,
        thread_id=context.resolved_thread_id,
        runtime_paths=context.runtime_paths,
    ).model_name


def _workflow_permission_model_refs(context: ToolRuntimeContext, spec: dict[str, object]) -> set[str]:
    raw_permissions = spec.get("permissions")
    if raw_permissions is None:
        return set()
    if not isinstance(raw_permissions, dict):
        return set()
    permissions = cast("dict[str, object]", raw_permissions)
    raw_models = permissions.get("models")
    if raw_models is None:
        return set()
    if not isinstance(raw_models, list):
        return set()
    refs: set[str] = set()
    for raw_model in raw_models:
        if not isinstance(raw_model, str) or not raw_model.strip():
            continue
        model_ref = raw_model.strip()
        refs.add(model_ref)
        if model_ref in context.config.models:
            refs.update(_model_refs(context, model_ref))
        else:
            for model_name, model_config in context.config.models.items():
                if model_config.id == model_ref:
                    refs.update(_model_refs(context, model_name))
                    break
    return refs


def _workflow_permission_tools(spec: dict[str, object]) -> set[str]:
    permissions = spec.get("permissions")
    tools = cast("dict[str, object]", permissions).get("tools") if isinstance(permissions, dict) else None
    return {tool for tool in cast("list[object]", tools) if isinstance(tool, str)} if isinstance(tools, list) else set()


def _model_refs(context: ToolRuntimeContext, model_name: str) -> set[str]:
    refs = {model_name}
    model_config = context.config.models.get(model_name)
    if model_config is not None:
        refs.add(model_config.id)
    return refs


def _workflow_participants(spec: dict[str, object]) -> list[dict[str, object]]:
    raw_participants = spec.get("participants", [])
    if not isinstance(raw_participants, list):
        return []
    participants: list[dict[str, object]] = []
    for raw_participant in raw_participants:
        if not isinstance(raw_participant, dict):
            continue
        participant: dict[str, object] = {key: value for key, value in raw_participant.items() if isinstance(key, str)}
        participants.append(participant)
    return participants


def _required_participant_text(participant: dict[str, object], field_name: str) -> str:
    value = participant.get(field_name)
    if not isinstance(value, str) or not value.strip():
        msg = f"Dynamic Workflow participant field '{field_name}' must be a non-empty string."
        raise DynamicWorkflowError(msg)
    return value.strip()
