"""Dynamic Workflow tools for MindRoom agents."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import nio
from agno.agent import Agent
from agno.run.agent import RunOutput, RunStatus
from agno.tools import Toolkit

from mindroom import model_loading
from mindroom.authorization import responder_candidate_entities_from_cached_room
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.credentials import get_runtime_credentials_manager, load_scoped_credentials
from mindroom.custom_tools.dynamic_workflow_context import (
    authorize_dynamic_workflow_run,
    dynamic_workflow_store,
    dynamic_workflow_store_and_owner,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.custom_tools.toolkit_functions import JSON_OBJECT_SCHEMA, register_toolkit_functions
from mindroom.dynamic_workflows.runner import DynamicWorkflowExecutionError
from mindroom.dynamic_workflows.service import DynamicWorkflowService
from mindroom.dynamic_workflows.store import DynamicWorkflowError
from mindroom.entity_resolution import entity_identity_registry
from mindroom.tool_approval import ToolCallWorkflowOrigin
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge, prepend_tool_hook_bridge

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.dynamic_workflows.runner import AsyncParticipantExecutor, ParticipantExecutor

# Agent-infrastructure toolkits that are built outside the tool registry and presume
# a durable agent runtime; they can never be granted to workflow participants.
_WORKFLOW_RESTRICTED_TOOLS = frozenset(
    {"compact_context", "delegate", "dynamic_tools", "dynamic_workflow", "memory", "self_config"},
)

# Tools that mutate the MindRoom system itself (rewrite config.yaml, spawn agents, create
# cron jobs, run an autonomous coding agent). Participants may be granted these, but every
# call needs a human decision: allowed_tools (including "*") never pre-approves them.
_WORKFLOW_NO_PREAPPROVAL_TOOLS = frozenset({"claude_agent", "config_manager", "scheduler", "subagents"})

_TOOL_DESCRIPTIONS = {
    "create_workflow": (
        "Create a Dynamic Workflow from a declarative workflow spec. "
        "Ephemeral participants may declare any registered tool when it is also granted in "
        "permissions.tools; participant tool calls require per-call user approval unless the "
        "tool is pre-approved by the dynamic_workflow allowed_tools config."
    ),
    "validate_workflow": "Validate a declarative Dynamic Workflow spec without saving it.",
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
            "spec": JSON_OBJECT_SCHEMA,
            "scope": {"type": "string"},
            "reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["spec"],
    },
    "validate_workflow": {
        "type": "object",
        "properties": {"spec": JSON_OBJECT_SCHEMA},
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
        try:
            store, owner_id = dynamic_workflow_store_and_owner(context, scope)
            service = DynamicWorkflowService(
                store,
                participant_executor=_participant_executor(context, workflow_id),
                spec_validator=lambda spec: _validate_workflow_policy_for_context(context, spec),
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
        try:
            store, owner_id = dynamic_workflow_store_and_owner(context, scope)
            service = DynamicWorkflowService(
                store,
                async_participant_executor=_aparticipant_executor(context, workflow_id),
                spec_validator=lambda spec: _validate_workflow_policy_for_context(context, spec),
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


def _participant_executor(context: ToolRuntimeContext, workflow_id: str) -> ParticipantExecutor:
    run_scope = f"{workflow_id}:{uuid4().hex}"

    def execute(
        *,
        participant: dict[str, object],
        prompt: str,
        input_data: dict[str, object],
        step_outputs: dict[str, object],
    ) -> object:
        del input_data, step_outputs
        return _execute_participant(context, participant, prompt, run_scope=run_scope, workflow_id=workflow_id)

    return execute


def _aparticipant_executor(context: ToolRuntimeContext, workflow_id: str) -> AsyncParticipantExecutor:
    run_scope = f"{workflow_id}:{uuid4().hex}"

    async def execute(
        *,
        participant: dict[str, object],
        prompt: str,
        input_data: dict[str, object],
        step_outputs: dict[str, object],
    ) -> object:
        del input_data, step_outputs
        return await _aexecute_participant(context, participant, prompt, run_scope=run_scope, workflow_id=workflow_id)

    return execute


def _execute_participant(
    context: ToolRuntimeContext,
    participant: dict[str, object],
    prompt: str,
    *,
    run_scope: str,
    workflow_id: str,
) -> object:
    participant_kind = str(participant.get("kind", "ephemeral_agent")).strip() or "ephemeral_agent"
    if participant_kind == "room_agent":
        return _execute_room_agent_participant(context, participant, prompt, run_scope=run_scope)
    if participant_kind == "ephemeral_agent":
        return _execute_ephemeral_agent_participant(
            context,
            participant,
            prompt,
            run_scope=run_scope,
            workflow_id=workflow_id,
        )
    msg = f"Unsupported Dynamic Workflow participant kind '{participant_kind}'."
    raise DynamicWorkflowError(msg)


async def _aexecute_participant(
    context: ToolRuntimeContext,
    participant: dict[str, object],
    prompt: str,
    *,
    run_scope: str,
    workflow_id: str,
) -> object:
    participant_kind = str(participant.get("kind", "ephemeral_agent")).strip() or "ephemeral_agent"
    if participant_kind == "room_agent":
        return await _aexecute_room_agent_participant(context, participant, prompt, run_scope=run_scope)
    if participant_kind == "ephemeral_agent":
        return await _aexecute_ephemeral_agent_participant(
            context,
            participant,
            prompt,
            run_scope=run_scope,
            workflow_id=workflow_id,
        )
    msg = f"Unsupported Dynamic Workflow participant kind '{participant_kind}'."
    raise DynamicWorkflowError(msg)


def _execute_room_agent_participant(
    context: ToolRuntimeContext,
    participant: dict[str, object],
    prompt: str,
    *,
    run_scope: str = "manual",
) -> object:
    return asyncio.run(_aexecute_room_agent_participant(context, participant, prompt, run_scope=run_scope))


async def _aexecute_room_agent_participant(
    context: ToolRuntimeContext,
    participant: dict[str, object],
    prompt: str,
    *,
    run_scope: str = "manual",
) -> object:
    agent_name = _validate_room_agent_reference_for_context(context, participant)
    participant_id = _required_participant_text(participant, "id")
    runtime_model = context.config.resolve_runtime_model(
        entity_name=agent_name,
        room_id=context.room_id,
        runtime_paths=context.runtime_paths,
    )
    active_model_name = runtime_model.model_name
    session_id = _participant_session_id(context, participant_id, run_scope=run_scope)
    participant_context = replace(
        context,
        agent_name=agent_name,
        active_model_name=active_model_name,
        session_id=session_id,
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
    room = _candidate_resolution_room(context)
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
    return agent_name


def _execute_ephemeral_agent_participant(
    context: ToolRuntimeContext,
    participant: dict[str, object],
    prompt: str,
    *,
    run_scope: str,
    workflow_id: str,
) -> object:
    return asyncio.run(
        _aexecute_ephemeral_agent_participant(
            context,
            participant,
            prompt,
            run_scope=run_scope,
            workflow_id=workflow_id,
        ),
    )


async def _aexecute_ephemeral_agent_participant(
    context: ToolRuntimeContext,
    participant: dict[str, object],
    prompt: str,
    *,
    run_scope: str,
    workflow_id: str,
) -> object:
    toolkits_by_name = _resolve_participant_toolkits(context, participant)
    participant_id = _required_participant_text(participant, "id")
    model_name = _resolve_participant_model_name(
        context,
        participant.get("model"),
        default_model=_caller_runtime_model_name(context),
    )
    execution_identity = build_execution_identity_from_runtime_context(context)
    model = model_loading.get_model_instance(context.config, context.runtime_paths, model_name, execution_identity)
    run_config = _participant_run_config(context, toolkits_by_name)
    bridge = build_tool_hook_bridge(
        context.hook_registry,
        agent_name=context.agent_name,
        config=run_config,
        runtime_paths=context.runtime_paths,
        workflow_origin=ToolCallWorkflowOrigin(workflow_id=workflow_id, participant_id=participant_id),
    )
    agent = Agent(
        id=f"dynamic_workflow_{participant_id}",
        name=str(participant.get("name") or participant_id),
        role=str(participant.get("role") or participant.get("description") or "Dynamic Workflow participant."),
        model=model,
        tools=[prepend_tool_hook_bridge(toolkit, bridge) for toolkit in toolkits_by_name.values()],
        instructions=_participant_instructions(participant),
        markdown=True,
        telemetry=False,
    )
    participant_context = replace(
        context,
        config=run_config,
        active_model_name=model_name,
        session_id=_participant_session_id(context, participant_id, run_scope=run_scope),
    )
    return await _arun_agent(participant_context, agent, prompt)


def _resolve_participant_toolkits(context: ToolRuntimeContext, participant: dict[str, object]) -> dict[str, Toolkit]:
    """Resolve participant tool grants to toolkit instances with the caller's tool routing."""
    tool_names = _participant_tool_names(participant)
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
    authored_overrides = {
        entry.name: entry.tool_config_overrides for entry in context.config.get_agent_tool_configs(context.agent_name)
    }
    toolkits: dict[str, Toolkit] = {}
    for tool_name in tool_names:
        toolkit = build_agent_toolkit(
            tool_name,
            agent_name=context.agent_name,
            config=context.config,
            runtime_paths=context.runtime_paths,
            worker_tools=worker_tools,
            runtime_overrides=context.config.get_agent_tool_runtime_overrides(context.agent_name, tool_name),
            tool_config_overrides=authored_overrides.get(tool_name),
            execution_identity=execution_identity,
            session_id=context.session_id,
        )
        if toolkit is None:
            msg = f"Dynamic Workflow participant tool '{tool_name}' is not available in this runtime."
            raise DynamicWorkflowError(msg)
        toolkits[tool_name] = toolkit
    return toolkits


def _participant_tool_names(participant: dict[str, object]) -> list[str]:
    raw_tools = participant.get("tools")
    if raw_tools is None:
        return []
    if not isinstance(raw_tools, list) or not all(isinstance(tool, str) and tool.strip() for tool in raw_tools):
        msg = "Dynamic Workflow participant tools must be a list of non-empty strings."
        raise DynamicWorkflowError(msg)
    tool_names: list[str] = []
    for raw_tool in raw_tools:
        tool_name = cast("str", raw_tool).strip()
        if tool_name not in tool_names:
            tool_names.append(tool_name)
    return tool_names


def _reject_unavailable_workflow_tools(tool_names: list[str]) -> None:
    for tool_name in tool_names:
        if tool_name in _WORKFLOW_RESTRICTED_TOOLS:
            msg = f"Dynamic Workflow participants cannot use agent-infrastructure tool '{tool_name}'."
            raise DynamicWorkflowError(msg)
        if tool_name not in TOOL_METADATA:
            msg = f"Dynamic Workflow participant tool '{tool_name}' is not a registered tool."
            raise DynamicWorkflowError(msg)


def _participant_run_config(context: ToolRuntimeContext, toolkits_by_name: dict[str, Toolkit]) -> Config:
    """Return a config that requires per-call approval for granted tools that are not pre-approved."""
    if not toolkits_by_name:
        return context.config
    allowed_tools = _workflow_allowed_tools(context)
    allow_all = "*" in allowed_tools
    # The approval engine matches by bare function name, but function names collide across
    # toolkits (read_file on python and file, run_shell_command on daytona and shell). A function
    # is only safe to pre-approve when every granted toolkit exposing it is itself pre-approved;
    # otherwise a non-pre-approved toolkit's call would inherit the auto-approve rule.
    owning_tools: dict[str, set[str]] = {}
    for tool_name, toolkit in toolkits_by_name.items():
        for function_name in (*toolkit.functions, *toolkit.async_functions):
            owning_tools.setdefault(function_name, set()).add(tool_name)
    pre_approved_tools = {
        tool_name
        for tool_name in toolkits_by_name
        if tool_name not in _WORKFLOW_NO_PREAPPROVAL_TOOLS and (allow_all or tool_name in allowed_tools)
    }
    pre_approved_functions = sorted(
        function_name for function_name, tools in owning_tools.items() if tools <= pre_approved_tools
    )
    tool_approval = context.config.tool_approval.model_copy(
        update={
            "default": "require_approval",
            # Operator-authored rules keep precedence (first match wins); workflow pre-approval
            # only applies to functions the operator has not already ruled on.
            "rules": [
                *context.config.tool_approval.rules,
                *(
                    ApprovalRuleConfig(match=function_name, action="auto_approve")
                    for function_name in pre_approved_functions
                ),
            ],
        },
    )
    return context.config.model_copy(update={"tool_approval": tool_approval})


def _workflow_allowed_tools(context: ToolRuntimeContext) -> frozenset[str]:
    """Resolve pre-approved workflow tool names from dashboard and authored tool config."""
    values: dict[str, object] = {}
    credentials_manager = get_runtime_credentials_manager(context.runtime_paths)
    persisted = load_scoped_credentials("dynamic_workflow", credentials_manager=credentials_manager, worker_target=None)
    if persisted:
        values.update(persisted)
    for entry in context.config.get_agent_tool_configs(context.agent_name):
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
    # exceed 10 minutes. Consuming the event stream drives tool calls and their approval gating;
    # yield_run_output makes the final RunOutput the last streamed item, which works without a db.
    final_output: RunOutput | None = None
    with tool_runtime_context(context):
        event_stream = agent.arun(
            prompt,
            user_id=context.requester_id,
            session_id=context.session_id,
            stream=True,
            stream_events=True,
            yield_run_output=True,
        )
        async for event in event_stream:
            if isinstance(event, RunOutput):
                final_output = event
    if final_output is None:
        msg = "Dynamic Workflow participant run produced no output."
        raise DynamicWorkflowExecutionError(msg)
    content = final_output.content if final_output.content is not None else ""
    if final_output.status != RunStatus.completed:
        message = str(content) if content else f"Agent run ended with status {final_output.status.value}."
        raise DynamicWorkflowExecutionError(message)
    return content


def _participant_session_id(context: ToolRuntimeContext, participant_id: str, *, run_scope: str) -> str:
    base_session_id = context.session_id or context.resolved_thread_id or context.thread_id or context.room_id
    return f"{base_session_id}:dynamic_workflow:{run_scope}:{participant_id}"


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


def _validate_workflow_policy_for_context(context: ToolRuntimeContext, spec: dict[str, object]) -> None:
    caller_models = _caller_allowed_model_refs(context)
    permission_models = _workflow_permission_model_refs(context, spec)
    for participant in _workflow_participants(spec):
        participant_kind = str(participant.get("kind", "ephemeral_agent")).strip() or "ephemeral_agent"
        raw_model = participant.get("model")
        if participant_kind == "room_agent":
            agent_name = _validate_room_agent_reference_for_context(context, participant)
            model_name = context.config.resolve_runtime_model(
                entity_name=agent_name,
                room_id=context.room_id,
                runtime_paths=context.runtime_paths,
            ).model_name
        else:
            model_name = _resolve_participant_model_name(
                context,
                raw_model,
                default_model=_caller_runtime_model_name(context),
            )
        model_refs = _model_refs(context, model_name)
        if permission_models and model_refs.isdisjoint(permission_models):
            msg = (
                f"Dynamic Workflow participant model '{model_name}' is not allowed by permissions.models. "
                "Add the model to workflow permissions before running this revision."
            )
            raise DynamicWorkflowError(msg)
        if participant_kind != "room_agent" and model_refs.isdisjoint(caller_models):
            requested_model = raw_model if raw_model is not None else model_name
            msg = (
                f"Dynamic Workflow participant model '{requested_model}' is not allowed for agent '{context.agent_name}'. "
                "Use the caller's active model or add an approval policy before requesting another model."
            )
            raise DynamicWorkflowError(msg)
    _validate_workflow_tool_policy_for_context(context, spec)


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


def _caller_allowed_model_refs(context: ToolRuntimeContext) -> set[str]:
    model_names = {_caller_runtime_model_name(context)}
    refs: set[str] = set()
    for model_name in model_names:
        if model_name is None:
            continue
        refs.add(model_name)
        model_config = context.config.models.get(model_name)
        if model_config is not None:
            refs.add(model_config.id)
    return refs


def _caller_runtime_model_name(context: ToolRuntimeContext) -> str:
    if context.active_model_name:
        return context.active_model_name
    return context.config.resolve_runtime_model(
        entity_name=context.agent_name,
        room_id=context.room_id,
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
