"""Dynamic Workflow tools for MindRoom agents."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import nio

from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import RunContext, UnstoredSession
from mindroom.approval.tool_approval import tool_may_require_approval
from mindroom.authorization import responder_candidate_entities_from_cached_room
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.budgets.monitor import budget_model
from mindroom.config.resolution import authored_tool_entries
from mindroom.custom_tools.dynamic_workflow_context import (
    authorize_dynamic_workflow_run,
    dynamic_workflow_store_and_owner,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload, tool_context_or_error
from mindroom.delegation.direct import run_direct_child_turn
from mindroom.delegation.lifecycle import finish_child_turn, prepare_child_turn
from mindroom.delegation.personas import (
    PersonaError,
    PersonaRequest,
    caller_skill_names,
    caller_toolkit_names,
    capped_persona_skills,
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
from mindroom.dynamic_workflows.spec import (
    DynamicWorkflowError,
    RoomAgentParticipant,
    SubagentParticipant,
    WorkflowSpec,
    WorkflowSpecError,
    granted_tool_error,
    parse_workflow_spec,
)
from mindroom.dynamic_workflows.store import AGENT_SCOPE
from mindroom.entity_resolution import entity_identity_registry
from mindroom.helper_usage import get_helper_usage_owner, record_run_usage
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.origins import configured_public_url
from mindroom.response_turn import ResponsePausedForApproval
from mindroom.run_storage import TerminalRecord
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.agno_toolkits import toolkit_function_names
from mindroom.tool_system.authoring import Toolkit, tool
from mindroom.tool_system.automation_approval import NEVER_PREAPPROVE_TOOLKITS, build_automation_approval_config
from mindroom.tool_system.metadata import resolve_tool_settings
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.sandbox_proxy import tool_placement
from mindroom.tool_system.skills import SKILL_READ_FUNCTION_NAMES
from mindroom.turn_model import ModelScope, TurnBudget, resolve_turn_model

if TYPE_CHECKING:
    from mindroom.agent_loop.build import AgentBuild
    from mindroom.config.main import Config
    from mindroom.delegation.state import DelegationChild, SubagentPersona
    from mindroom.dynamic_workflows.runner import ParticipantExecutor
    from mindroom.dynamic_workflows.spec import Participant
    from mindroom.dynamic_workflows.store import DynamicWorkflowRun, DynamicWorkflowSummary
    from mindroom.tool_system.agno_toolkits import AnyToolkit
    from mindroom.tool_system.registry import ToolRegistry
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

# Agent-infrastructure toolkits that are built outside the tool registry and presume
# a durable agent runtime; they can never be granted to workflow participants.
_WORKFLOW_RESTRICTED_TOOLS = frozenset(
    {
        "agent_manager",
        "compact_context",
        "contacts",
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


@dataclass
class _WorkflowRun:
    """One run's participant state: the key naming its sessions, requests frozen at start, continuing children, and cached function owners."""

    run_key: str
    requests: dict[str, PersonaRequest] = field(default_factory=dict)
    children: dict[str, DelegationChild] = field(default_factory=dict)
    function_owners: dict[str, dict[str, frozenset[str]]] = field(default_factory=dict)


_MINIMAL_SPEC_EXAMPLE = (
    '{"schema_version": 1, "kind": "workflow", "id": "my_flow", "name": "My Flow", '
    '"participants": [{"id": "writer", "system_prompt": "You write short poems."}], '
    '"workflow": [{"id": "draft", "participant": "writer", "prompt": "Write a haiku about {input.topic}."}]}'
)

_SPEC_REQUIRED_FIELDS = (
    "Required spec fields: schema_version (must be 1), kind (must be 'workflow'), id, name, participants (list of "
    "{id, system_prompt or profile, ...}), workflow (list of steps such as {id, participant, prompt})."
)

_CREATE_WORKFLOW_DESCRIPTION = (
    "Create a Dynamic Workflow from a declarative workflow spec. "
    f"Minimal valid spec: {_MINIMAL_SPEC_EXAMPLE} "
    f"{_SPEC_REQUIRED_FIELDS} "
    "Subagent participants are authored copies of you: each sets a system_prompt replacing your role, or a profile "
    "saved as subagents/<name>.md in your workspace, plus tools, skills, model, and mode. A participant gets only "
    "the tools it names from your own toolkits (toolkit or toolkit.function), so list every toolkit it needs, "
    "and only the skills it names from your own. "
    "Participants cannot pause for approval, so every toolkit a participant names must be pre-approved by the "
    "dynamic_workflow allowed_tools config. room_agent participants run another agent available in this room "
    "without tools."
)

_VALIDATE_WORKFLOW_DESCRIPTION = (
    "Validate a declarative Dynamic Workflow spec without saving it. "
    "Reports every detected validation error in one call. "
    f"Minimal valid spec: {_MINIMAL_SPEC_EXAMPLE} "
    f"{_SPEC_REQUIRED_FIELDS}"
)


class DynamicWorkflowTools(Toolkit):
    """Tools that let an agent create, update, inspect, and run Dynamic Workflows."""

    def __init__(self) -> None:
        super().__init__(name="dynamic_workflow")

    @staticmethod
    def _payload(status: str, **fields: object) -> str:
        return custom_tool_payload("dynamic_workflow", status, **fields)

    @classmethod
    def _run_payload(cls, run: DynamicWorkflowRun, **fields: object) -> str:
        return cls._payload(
            run.status,
            workflow_id=run.workflow_id,
            run_id=run.run_id,
            revision=run.revision,
            report_url=run.report_url,
            artifacts=run.artifacts,
            outputs=run.outputs,
            error=run.error,
            **fields,
        )

    @classmethod
    def _summary_payload(cls, summary: DynamicWorkflowSummary) -> str:
        return cls._payload(
            "ok",
            workflow_id=summary.workflow_id,
            owner_id=summary.owner_id,
            active_revision=summary.active_revision,
            name=summary.name,
        )

    @classmethod
    def _spec_errors_payload(cls, exc: WorkflowSpecError, **fields: object) -> str:
        return cls._payload(
            "error",
            message=str(exc),
            errors=list(exc.errors),
            minimal_valid_spec_example=_MINIMAL_SPEC_EXAMPLE,
            **fields,
        )

    @tool(description=_CREATE_WORKFLOW_DESCRIPTION)
    async def create_workflow(
        self,
        spec: dict[str, Any],
        reason: str | None = None,
    ) -> str:
        """Create a Dynamic Workflow from a declarative workflow spec.

        Args:
            spec: Declarative workflow spec with the required fields the description lists.
            reason: Why the workflow is created, recorded with its first revision.

        """
        if isinstance(context := tool_context_or_error(self._payload, "Dynamic Workflow"), str):
            return context
        try:
            parsed = parse_workflow_spec(spec)
            store, owner_id = dynamic_workflow_store_and_owner(context)

            def create() -> DynamicWorkflowSummary:
                _validate_workflow_policy_for_context(context, parsed)
                return store.create_workflow(
                    spec=parsed,
                    scope=AGENT_SCOPE,
                    owner_id=owner_id,
                    created_by=context.agent_name,
                    reason=reason,
                )

            summary = await run_blocking_until_complete(create)
        except WorkflowSpecError as exc:
            return self._spec_errors_payload(exc)
        except DynamicWorkflowError as exc:
            return self._payload("error", message=str(exc))
        return self._summary_payload(summary)

    @tool(description=_VALIDATE_WORKFLOW_DESCRIPTION)
    async def validate_workflow(self, spec: dict[str, Any]) -> str:
        """Validate a declarative Dynamic Workflow spec without saving it.

        Args:
            spec: Declarative workflow spec with the required fields the description lists.

        """
        if isinstance(context := tool_context_or_error(self._payload, "Dynamic Workflow"), str):
            return context
        try:
            parsed = parse_workflow_spec(spec)
            _validate_workflow_policy_for_context(context, parsed)
        except WorkflowSpecError as exc:
            return self._spec_errors_payload(exc)
        except DynamicWorkflowError as exc:
            return self._payload("error", message=str(exc))
        return self._payload("ok", workflow_id=parsed.id, name=parsed.name)

    @tool
    async def update_workflow(
        self,
        workflow_id: str,
        patch: dict[str, Any],
        reason: str,
    ) -> str:
        """Create and publish a new Dynamic Workflow revision from a patch."""
        if isinstance(context := tool_context_or_error(self._payload, "Dynamic Workflow"), str):
            return context
        try:
            store, owner_id = dynamic_workflow_store_and_owner(context)
            summary = await run_blocking_until_complete(
                partial(
                    store.update_workflow,
                    workflow_id=workflow_id,
                    scope=AGENT_SCOPE,
                    owner_id=owner_id,
                    patch=patch,
                    updated_by=context.agent_name,
                    reason=reason,
                    spec_validator=lambda spec: _validate_workflow_policy_for_context(context, spec),
                ),
            )
        except WorkflowSpecError as exc:
            return self._spec_errors_payload(exc, workflow_id=workflow_id)
        except DynamicWorkflowError as exc:
            return self._payload("error", workflow_id=workflow_id, message=str(exc))
        return self._summary_payload(summary)

    @tool
    async def run_workflow(
        self,
        workflow_id: str,
        input: dict[str, Any],  # noqa: A002
    ) -> str:
        """Run a Dynamic Workflow and persist step outputs plus report artifacts."""
        if isinstance(context := tool_context_or_error(self._payload, "Dynamic Workflow"), str):
            return context
        state = _WorkflowRun(f"{workflow_id}:{uuid4().hex}")
        try:
            store, owner_id = dynamic_workflow_store_and_owner(context)
            service = DynamicWorkflowService(
                store,
                participant_executor=_participant_executor(context, state),
                spec_validator=lambda spec: state.requests.update(_validate_workflow_policy_for_context(context, spec)),
            )
            run = await service.run_workflow(
                workflow_id=workflow_id,
                scope=AGENT_SCOPE,
                owner_id=owner_id,
                input_data=input,
                requested_by=context.requester_id,
                base_url=configured_public_url(context.runtime_paths),
            )
        except DynamicWorkflowError as exc:
            return self._payload("error", workflow_id=workflow_id, message=str(exc))
        return self._run_payload(run, step_count=len(run.steps))

    @tool
    async def get_workflow_run(self, workflow_id: str, run_id: str) -> str:
        """Read one Dynamic Workflow run record."""
        if isinstance(context := tool_context_or_error(self._payload, "Dynamic Workflow"), str):
            return context
        try:
            store, owner_id = dynamic_workflow_store_and_owner(context)
            run = await run_blocking_until_complete(
                partial(
                    store.get_workflow_run,
                    workflow_id=workflow_id,
                    scope=AGENT_SCOPE,
                    owner_id=owner_id,
                    run_id=run_id,
                ),
            )
            authorize_dynamic_workflow_run(context, run)
        except DynamicWorkflowError as exc:
            return self._payload("error", workflow_id=workflow_id, run_id=run_id, message=str(exc))
        return self._run_payload(run, steps=run.steps)

    @tool
    async def list_workflows(self) -> str:
        """List your saved Dynamic Workflows."""
        if isinstance(context := tool_context_or_error(self._payload, "Dynamic Workflow"), str):
            return context
        try:
            store, owner_id = dynamic_workflow_store_and_owner(context)
            workflows = await run_blocking_until_complete(
                partial(store.list_workflows, scope=AGENT_SCOPE, owner_id=owner_id),
            )
        except DynamicWorkflowError as exc:
            return self._payload("error", message=str(exc))
        return self._payload(
            "ok",
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

    @tool
    async def list_workflow_revisions(self, workflow_id: str) -> str:
        """List immutable revisions for one Dynamic Workflow."""
        if isinstance(context := tool_context_or_error(self._payload, "Dynamic Workflow"), str):
            return context
        try:
            store, owner_id = dynamic_workflow_store_and_owner(context)
            revisions = await run_blocking_until_complete(
                partial(store.list_workflow_revisions, workflow_id=workflow_id, scope=AGENT_SCOPE, owner_id=owner_id),
            )
        except DynamicWorkflowError as exc:
            return self._payload("error", workflow_id=workflow_id, message=str(exc))
        return self._payload("ok", workflow_id=workflow_id, revisions=revisions)


def _participant_executor(context: ToolRuntimeContext, state: _WorkflowRun) -> ParticipantExecutor:
    async def execute(*, participant: Participant, prompt: str) -> ParticipantOutput:
        return await _execute_participant(context, participant, prompt, state)

    return execute


async def _execute_participant(
    context: ToolRuntimeContext,
    participant: Participant,
    prompt: str,
    state: _WorkflowRun,
) -> ParticipantOutput:
    match participant:
        case RoomAgentParticipant():
            return ParticipantOutput(
                await _execute_room_agent_participant(context, participant, prompt, run_key=state.run_key),
            )
        case SubagentParticipant():
            return await _execute_subagent_participant(context, participant, prompt, state)


async def _execute_room_agent_participant(
    context: ToolRuntimeContext,
    participant: RoomAgentParticipant,
    prompt: str,
    *,
    run_key: str,
) -> object:
    context = replace(context, config=context.current_config, config_provider=None)
    agent_name = _validate_room_agent_reference_for_context(context, participant)
    session_id = f"{context.session_id}:dynamic_workflow:{run_key}:{participant.id}"
    participant_context, build = await run_blocking_until_complete(
        _build_room_agent_participant,
        context,
        agent_name,
        session_id,
    )
    return await _run_room_agent_turn(participant_context, build, prompt)


def _build_room_agent_participant(
    context: ToolRuntimeContext,
    agent_name: str,
    session_id: str,
) -> tuple[ToolRuntimeContext, AgentBuild]:
    """Resolve a room agent's model and build it; both walk the filesystem, so callers run this off the event loop."""
    model = resolve_turn_model(
        context.config,
        context.runtime_paths,
        _participant_scope(context, agent_name),
        TurnBudget(monitor=context.budget_monitor, requester_id=context.requester_id),
    )
    participant_context = replace(
        context,
        agent_name=agent_name,
        active_model_name=model.model_name,
        target=replace(context.target, session_id=session_id),
    )
    # Imported lazily to avoid the create_agent -> dynamic_workflow toolkit cycle.
    from mindroom.agents import create_agent  # noqa: PLC0415

    build = create_agent(
        agent_name,
        context.config,
        context.runtime_paths,
        execution_identity=participant_context.execution_identity,
        knowledge=None,
        model=model,
        plugins=context.plugins,
        include_interactive_questions=False,
        disable_runtime_capabilities=True,
    )
    return participant_context, build


def _available_room_agent_names(context: ToolRuntimeContext) -> set[str]:
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
    participant: RoomAgentParticipant,
) -> str:
    """Return the participant's room agent; ``context`` already reads the live config."""
    agent_name = participant.agent
    if agent_name not in context.config.agents:
        msg = f"Dynamic Workflow participant references unknown room agent '{agent_name}'."
        raise DynamicWorkflowError(msg)
    if agent_name not in _available_room_agent_names(context):
        msg = f"Dynamic Workflow room agent participant '{agent_name}' is not available to this requester in this room."
        raise DynamicWorkflowError(msg)
    return agent_name


async def _execute_subagent_participant(
    context: ToolRuntimeContext,
    participant: SubagentParticipant,
    prompt: str,
    state: _WorkflowRun,
) -> ParticipantOutput:
    """Run one step as a turn of this participant's subagent, an authored copy of the caller.

    Every step runs the persona validated when the run started, so a profile edited
    during the run cannot change its prompt, tools, or model.
    """
    context = replace(context, config=context.current_config, config_provider=None)
    participant_id = participant.id
    request = state.requests[participant_id]
    previous = state.children.get(participant_id)
    persona = cast("SubagentPersona", request.persona)
    missing = missing_persona_tool(persona.tools, _participant_available_toolkits(context), _participant_cap(context))
    if missing is not None:
        msg = f"Dynamic Workflow participant '{participant_id}' tool '{missing}' is no longer available to you."
        raise DynamicWorkflowExecutionError(msg)
    # Recheck approvals against the current config each step; only the function ownership is cached.
    allowed = _workflow_allowed_tools(context)
    _reject_unapproved_participant_tools(context, persona.tools or (), allowed=allowed)
    if participant_id not in state.function_owners:
        state.function_owners[participant_id] = await _participant_function_owners(context, persona)
    approval_config = _participant_run_config(
        context,
        state.function_owners[participant_id],
        allowed=allowed,
        reads_skills=bool(persona.skills),
    )
    owner = context.execution_identity
    child = prepare_child_turn(
        context.agent_name,
        context.agent_name,
        prompt,
        owner=owner,
        config=context.config,
        runtime_paths=context.runtime_paths,
        depth=0,
        budget_monitor=context.budget_monitor,
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
    state.children[participant_id] = child
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
    tools = config_plugins(context.config, context.runtime_paths).tools
    functions = {name: declared for name in names if (declared := declared_function_names(name, tools)) is not None}
    built = await asyncio.to_thread(
        _resolve_participant_toolkits,
        context,
        [name for name in names if name not in functions],
    )
    functions |= {name: toolkit_function_names(toolkit) for name, toolkit in built.items()}
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
    """Return the calling authored subagent's tools that a participant may also use, if the caller lists them."""
    if context.persona is None or context.persona.tools is None:
        return None
    return tuple(entry for entry in context.persona.tools if entry.partition(".")[0] not in _WORKFLOW_RESTRICTED_TOOLS)


def _participant_available_toolkits(context: ToolRuntimeContext) -> dict[str, tuple[str, ...] | None]:
    """Return the caller toolkits a participant may name; infrastructure toolkits never qualify."""
    tools = config_plugins(context.config, context.runtime_paths).tools
    return {
        name: declared
        for name, declared in caller_toolkit_names(
            context.agent_name,
            context.config,
            tools,
            delegation_depth=0,
        ).items()
        if name not in _WORKFLOW_RESTRICTED_TOOLS
    }


def _participant_request(
    context: ToolRuntimeContext,
    participant: SubagentParticipant,
    *,
    workflow_id: str,
) -> PersonaRequest:
    """Resolve one participant's persona, model, and mode; it uses only the tools and skills it names."""
    source_name = f"{workflow_id}/{participant.id}"
    available = _participant_available_toolkits(context)
    cap = _participant_cap(context)
    try:
        if participant.profile is not None:
            profile = load_profile(context.agent_workspace_root(), participant.profile)
            persona = replace(profile.persona, source_kind="workflow", source_name=source_name)
            raw_model, mode = profile.model, profile.mode or "standard"
        else:
            persona = inline_persona(
                participant.system_prompt,
                participant.tools,
                participant.skills,
                None,
                source_kind="workflow",
                source_name=source_name,
            )
            raw_model, mode = participant.model, participant.mode or "standard"
        # Participants run without knowledge until workflow data grants are supported.
        persona = replace(persona, tools=persona.tools or (), knowledge=())
        validate_persona_tools(persona.tools, available, cap)
        persona = capped_persona_skills(
            persona,
            lambda: caller_skill_names(
                context.agent_name,
                context.config,
                context.runtime_paths,
                context.agent_workspace_root(),
            ),
            context.persona.kept_skills if context.persona is not None else None,
        )
        require_minimal_shell(persona, mode)
    except PersonaError as exc:
        raise DynamicWorkflowError(str(exc)) from exc
    model = _resolve_participant_model_name(context, raw_model, default_model=_caller_runtime_model_name(context))
    return PersonaRequest(persona=persona, model=model, agent_mode=mode)


def _resolve_participant_toolkits(context: ToolRuntimeContext, tool_names: list[str]) -> dict[str, AnyToolkit]:
    """Build participant toolkits with the caller's tool routing to learn which functions they expose."""
    if not tool_names:
        return {}
    # Imported lazily to avoid the create_agent -> dynamic_workflow toolkit cycle.
    from mindroom.agents import build_agent_toolkit  # noqa: PLC0415

    execution_identity = context.execution_identity
    tools = config_plugins(context.config, context.runtime_paths).tools
    agent_tools = resolve_agent_tools(context.config, context.agent_name, tools)
    placement = tool_placement(context.runtime_paths, tools)
    authored_overrides = {entry.name: entry.tool_config_overrides for entry in agent_tools.candidates()}
    workspace_in_worker = agent_tools.workspace_in_worker(tool_names, placement)
    toolkits: dict[str, AnyToolkit] = {}
    for tool_name in tool_names:
        toolkit = build_agent_toolkit(
            tool_name,
            agent_tools=agent_tools,
            placement=placement,
            runtime_paths=context.runtime_paths,
            tool_config_overrides=authored_overrides.get(tool_name),
            execution_identity=execution_identity,
            workspace_in_worker=workspace_in_worker,
        )
        if toolkit is None:
            msg = f"Dynamic Workflow participant tool '{tool_name}' is not available in this runtime."
            raise DynamicWorkflowError(msg)
        toolkits[tool_name] = toolkit
    return toolkits


def _reject_unavailable_workflow_tools(tool_names: list[str], tools: ToolRegistry) -> None:
    for tool_name in (entry.partition(".")[0] for entry in tool_names):
        if tool_name in _WORKFLOW_RESTRICTED_TOOLS:
            msg = f"Dynamic Workflow participants cannot use agent-infrastructure tool '{tool_name}'."
            raise DynamicWorkflowError(msg)
        if tool_name not in tools:
            msg = f"Dynamic Workflow participant tool '{tool_name}' is not a registered tool."
            raise DynamicWorkflowError(msg)


def _participant_run_config(
    context: ToolRuntimeContext,
    function_owners: Mapping[str, frozenset[str]],
    *,
    allowed: frozenset[str],
    reads_skills: bool,
) -> Config:
    """Return the participant policy: approval by default, with the caller's allowed tools pre-approved.

    A participant may read the skills it names whenever its caller could without approval;
    reading a skill's scripts still needs an operator rule.
    """
    return build_automation_approval_config(
        context.config,
        function_owners=function_owners,
        preapproved_toolkits=allowed,
        never_preapprove_toolkits=NEVER_PREAPPROVE_TOOLKITS,
        auto_approved_functions=[
            name
            for name in SKILL_READ_FUNCTION_NAMES
            if reads_skills and not tool_may_require_approval(context.config, name)
        ],
    )


def _workflow_allowed_tools(context: ToolRuntimeContext) -> frozenset[str]:
    """Resolve the caller's pre-approved participant toolkits from its dashboard and authored tool settings."""
    settings = resolve_tool_settings(
        "dynamic_workflow",
        context.runtime_paths,
        tools=config_plugins(context.config, context.runtime_paths).tools,
        worker_target=context.resolve_worker_target(),
        # A global dashboard value applies to every agent; a save with this agent selected overrides it.
        allowed_shared_services=frozenset({"dynamic_workflow"}),
        tool_config_overrides=next(
            (
                entry.tool_config_overrides
                for entry in authored_tool_entries(context.config, context.agent_name)
                if entry.name == "dynamic_workflow"
            ),
            None,
        ),
    )
    # An authored value arrives validated as a list; a dashboard-saved one is whatever JSON the credentials API took.
    raw_allowed = settings.values.get("allowed_tools")
    if isinstance(raw_allowed, str):
        raw_allowed = [raw_allowed]
    if not isinstance(raw_allowed, list):
        return frozenset()
    return frozenset(tool.strip() for tool in raw_allowed if isinstance(tool, str) and tool.strip())


async def _run_room_agent_turn(context: ToolRuntimeContext, build: AgentBuild, prompt: str) -> object:
    """Run one turn of a room agent on the prompt alone and return its answer.

    Like every agent turn it streams, which the Anthropic and Vertex SDKs require of a request whose budget could
    exceed 10 minutes. The participant keeps no history, so the usage of its turn, however the turn ends, counts
    against the conversation that runs the workflow. Its build offers no tools, so the turn runs without loop
    hooks: it makes no tool call for a tool policy to wrap or a notice to follow.
    """
    usage_owner = get_helper_usage_owner()
    invocation_id = uuid4().hex
    events = run_agent(
        replace(build.spec, system_messages=prompt_for_turn(build)),
        [Message(role="user", content=prompt)],
        ctx=RunContext(
            run_id=invocation_id,
            session=UnstoredSession(context.session_id),
            user_id=context.requester_id,
        ),
        persist=(
            None
            if usage_owner is None
            else partial(
                record_run_usage,
                owner=usage_owner,
                kind="dynamic_workflow",
                requester_id=context.requester_id,
            )
        ),
        request_template=build.request_template,
    )
    finished = TerminalRecord(RunRecord)
    async for event in events:
        finished.collect(event)
    content = finished.terminal(invocation_id).content
    return content if content is not None else ""


def _resolve_participant_model_name(
    context: ToolRuntimeContext,
    raw_model: str | None,
    *,
    default_model: str,
) -> str:
    if raw_model is None:
        return default_model
    model_ref = raw_model.strip()
    if (model_name := _configured_model_name(context, model_ref)) is not None:
        return model_name
    msg = f"Dynamic Workflow participant model '{model_ref}' is not allowlisted in config.models."
    raise DynamicWorkflowError(msg)


def _validate_workflow_policy_for_context(
    context: ToolRuntimeContext,
    spec: WorkflowSpec,
) -> dict[str, PersonaRequest]:
    """Check a spec's participants against the caller's current policy.

    Returns each subagent participant's validated request, which a run freezes when it starts.
    """
    context = replace(context, config=context.current_config, config_provider=None)
    granted_tools = set(spec.permissions.tools)
    subagents = [participant for participant in spec.participants if isinstance(participant, SubagentParticipant)]
    tool_names = [*granted_tools, *(tool for participant in subagents for tool in participant.tools or ())]
    if tool_names:
        _reject_unavailable_workflow_tools(tool_names, config_plugins(context.config, context.runtime_paths).tools)
    permission_models = _with_budget_substitutes(
        context,
        _workflow_permission_model_refs(context, spec.permissions.models or []),
    )
    requests: dict[str, PersonaRequest] = {}
    for participant in spec.participants:
        if isinstance(participant, RoomAgentParticipant):
            model_name = _runtime_model_name(context, _validate_room_agent_reference_for_context(context, participant))
        else:
            request = _participant_request(context, participant, workflow_id=spec.id)
            persona_tools = cast("SubagentPersona", request.persona).tools or ()
            if error := granted_tool_error(participant.id, persona_tools, granted_tools):
                raise DynamicWorkflowError(error)
            requests[participant.id] = request
            model_name = cast("str", request.model)
        if permission_models and _model_refs(context, model_name).isdisjoint(permission_models):
            msg = (
                f"Dynamic Workflow participant model '{model_name}' is not allowed by permissions.models. "
                "Add the model to workflow permissions before running this revision."
            )
            raise DynamicWorkflowError(msg)
    return requests


def _with_budget_substitutes(context: ToolRuntimeContext, model_refs: set[str]) -> set[str]:
    """Permit the fallback model wherever a permitted model would be swapped for it under the caller's budget."""
    substitutes = {
        budget_model(
            context.config,
            context.runtime_paths,
            context.budget_monitor,
            context.requester_id,
            model_name,
            log_fallback=False,
        )
        for model_name in context.config.models
        if not _model_refs(context, model_name).isdisjoint(model_refs)
    }
    return model_refs.union(*(_model_refs(context, model_name) for model_name in substitutes))


def _participant_scope(context: ToolRuntimeContext, agent_name: str) -> ModelScope:
    return ModelScope(entity=agent_name, room_id=context.room_id, thread_id=context.resolved_thread_id)


def _runtime_model_name(context: ToolRuntimeContext, agent_name: str) -> str:
    return resolve_turn_model(
        context.config,
        context.runtime_paths,
        _participant_scope(context, agent_name),
        None,
    ).model_name


def _caller_runtime_model_name(context: ToolRuntimeContext) -> str:
    return context.active_model_name or _runtime_model_name(context, context.agent_name)


def _workflow_permission_model_refs(context: ToolRuntimeContext, models: list[str]) -> set[str]:
    refs: set[str] = set()
    for model_ref in models:
        refs.add(model_ref)
        if (model_name := _configured_model_name(context, model_ref)) is not None:
            refs.update(_model_refs(context, model_name))
    return refs


def _configured_model_name(context: ToolRuntimeContext, model_ref: str) -> str | None:
    """Return the configured model a reference names, by its config name or else by its provider ID."""
    if model_ref in context.config.models:
        return model_ref
    return next(
        (model_name for model_name, model_config in context.config.models.items() if model_config.id == model_ref),
        None,
    )


def _model_refs(context: ToolRuntimeContext, model_name: str) -> set[str]:
    refs = {model_name}
    model_config = context.config.models.get(model_name)
    if model_config is not None:
        refs.add(model_config.id)
    return refs
