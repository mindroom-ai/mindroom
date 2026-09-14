"""Drive durable external delegation requirements without retaining Python waits."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, Unpack, cast
from uuid import uuid4

from agno.db.base import BaseDb, SessionType
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput, RunPausedEvent, ToolCallCompletedEvent, ToolCallStartedEvent
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement
from agno.run.team import RunPausedEvent as TeamRunPausedEvent
from agno.run.team import TeamRunOutput
from agno.run.team import ToolCallCompletedEvent as TeamToolCallCompletedEvent
from agno.session.agent import AgentSession

from mindroom.agent_storage import create_session_storage
from mindroom.approval_receipt import install_approval_receipt_hooks
from mindroom.delegation_audit import (
    child_audit_context,
    finish_child_record,
    observe_child_event,
    record_child_response,
    start_child_record,
)
from mindroom.delegation_hooks import after_delegation, before_delegation
from mindroom.delegation_state import DELEGATION_STATE_KEY, DelegationChild, DelegationState
from mindroom.delegation_storage import delegation_storage_config, freeze_delegation_storage
from mindroom.dynamic_tool_continuation import continuation_decision_from_tools
from mindroom.history.native import restore_native_history
from mindroom.history.runtime import close_agent_runtime_state_dbs, create_scope_session_storage
from mindroom.history.types import HistoryScope
from mindroom.tool_approval import POLICY_CONFIRMATION_APPROVAL_TYPE, tool_may_require_approval
from mindroom.tool_system.runtime_context import get_tool_runtime_context, tool_runtime_context
from mindroom.tool_system.worker_routing import (
    parse_tool_execution_identity_payload,
    run_with_tool_execution_identity,
    serialize_tool_execution_identity,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from agno.agent import Agent
    from agno.team import Team

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.custom_tools.delegate import DelegateTools
    from mindroom.event_journal import ApprovalContinuation
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


class _DelegationOptions(TypedDict):
    """Explicit dependencies shared by blocking and streaming delegation drivers."""

    agent_name: str
    config: Config
    runtime_paths: RuntimePaths
    execution_identity: ToolExecutionIdentity | None
    delegation_depth: NotRequired[int]
    refresh_scheduler: NotRequired[KnowledgeRefreshScheduler | None]
    decisions: NotRequired[dict[str, bool] | None]
    denial_reasons: NotRequired[dict[str, str | None] | None]
    member_config_names: NotRequired[Mapping[str, str] | None]


_RUNNING_CHILD_ID: ContextVar[str | None] = ContextVar("running_delegated_child", default=None)


def _external_requirements(response: RunOutput | TeamRunOutput) -> list[RunRequirement]:
    return [
        requirement
        for requirement in response.requirements or ()
        if requirement.needs_external_execution
        and requirement.tool_execution is not None
        and requirement.tool_execution.tool_name == "run_subagent"
    ]


def has_delegation_state(response: RunOutput | TeamRunOutput) -> bool:
    """Return whether the native delegation driver owns this run's pause."""
    return bool(_external_requirements(response) or (response.metadata or {}).get(DELEGATION_STATE_KEY))


async def _persist(entity: Agent | Team, response: RunOutput | TeamRunOutput, state: DelegationState) -> None:
    response.metadata = {**(response.metadata or {}), DELEGATION_STATE_KEY: state.to_dict()}
    if not isinstance(entity.db, BaseDb) or not response.session_id:
        msg = "Durable delegation requires persisted session storage"
        raise RuntimeError(msg)
    await asyncio.to_thread(
        entity.db.upsert_run,
        run=deepcopy(response),
        session_id=response.session_id,
        user_id=response.user_id,
    )


def _toolkit(
    caller: str,
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    depth: int,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
) -> DelegateTools:
    # Delegation invokes the normal agent envelope, which imports this driver.
    from mindroom.custom_tools.delegate import DelegateTools  # noqa: PLC0415

    caller_config = config.agents.get(caller)
    return DelegateTools(
        caller,
        list(caller_config.delegate_to) if caller_config else [],
        runtime_paths,
        config,
        execution_identity=replace(identity, agent_name=caller),
        delegation_depth=depth,
        refresh_scheduler=refresh_scheduler,
    )


def _child_identity(child: DelegationChild) -> ToolExecutionIdentity:
    identity = parse_tool_execution_identity_payload(child.execution_identity, strict=True)
    if identity is None or identity.agent_name != child.child_agent_name or identity.session_id != child.session_id:
        msg = "Delegation child execution identity does not match its retained wait"
        raise RuntimeError(msg)
    return identity


async def _read_child(child: DelegationChild, config: Config, paths: RuntimePaths) -> RunOutput | None:
    config = delegation_storage_config(config, child.storage_bindings)

    def read() -> RunOutput | None:
        storage = create_session_storage(
            child.child_agent_name,
            config,
            paths,
            execution_identity=_child_identity(child),
        )
        try:
            session = storage.get_session(child.session_id, session_type=SessionType.AGENT)
            # The fresh session belongs exclusively to this delegation. Empty-run
            # retries and dynamic-tool continuations replace its active run ID.
            response = next(reversed(session.runs or ()), None) if isinstance(session, AgentSession) else None
            return deepcopy(response) if isinstance(response, RunOutput) else None
        finally:
            storage.close()

    response = await asyncio.to_thread(read)
    if response is not None:
        if (
            not response.run_id
            or response.session_id != child.session_id
            or response.user_id != _child_identity(child).requester_id
        ):
            msg = "Delegation session contains an outcome outside its requester identity"
            raise RuntimeError(msg)
        child.run_id = response.run_id
    return response


async def _settle_interrupted_child(
    child: DelegationChild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    reason: str,
    status: Literal["cancelled", "failed"] = "cancelled",
) -> None:
    config = delegation_storage_config(config, child.storage_bindings)
    response = await _read_child(child, config, runtime_paths)
    if response is not None and response.status == RunStatus.completed:
        await record_child_response(child, response, config=config, runtime_paths=runtime_paths)
        return
    if response is not None:
        await _cancel_delegations(response, config=config, runtime_paths=runtime_paths, reason=reason)
        response.status = RunStatus.cancelled if status == "cancelled" else RunStatus.error
        response.content = reason
        response.requirements = []
        for tool in response.tools or ():
            if tool.is_paused:
                tool.result = reason
                tool.tool_call_error = True
            tool.requires_confirmation = False
            tool.external_execution_required = False
            tool.requires_user_input = False

        def persist() -> None:
            storage = create_session_storage(
                child.child_agent_name,
                config,
                runtime_paths,
                execution_identity=_child_identity(child),
            )
            try:
                storage.upsert_run(run=response, session_id=child.session_id, user_id=response.user_id)
            finally:
                storage.close()

        await asyncio.to_thread(persist)
    child.status = status
    child.result = reason
    if child.record_locator:
        await finish_child_record(child, config=config, runtime_paths=runtime_paths, reason=reason)


async def _cancel_delegations(
    response: RunOutput | TeamRunOutput,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    reason: str = "Delegation cancelled.",
) -> None:
    """Cancel retained descendants without running any of their pending tools."""
    state = DelegationState.from_metadata(response.metadata)
    for child in state.children:
        if child.status not in {"completed", "failed", "cancelled", "denied"}:
            await _settle_interrupted_child(child, config=config, runtime_paths=runtime_paths, reason=reason)
    for requirement_id, hook_state in state.hooks.items():
        child = next((item for item in state.children if item.parent_requirement_id == requirement_id), None)
        await after_delegation(
            hook_state,
            config=config,
            runtime_paths=runtime_paths,
            result=child.result if child is not None else None,
            error=asyncio.CancelledError(reason) if child is None or child.status == "cancelled" else None,
        )
    state.clear_pending()
    if (response.metadata or {}).get(DELEGATION_STATE_KEY):
        response.metadata = {**(response.metadata or {}), DELEGATION_STATE_KEY: state.to_dict()}


async def cancel_approval_delegations(
    continuation: ApprovalContinuation,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    reason: str,
) -> None:
    """Read the frozen root scope and settle its retained child waits on failure."""
    if not continuation.execution_identity:
        return
    config = delegation_storage_config(config, continuation.delegation_storage_bindings)
    identity = parse_tool_execution_identity_payload(continuation.execution_identity, strict=True)
    if identity is None:
        return
    scope = continuation.history_scope
    if scope is None:
        if continuation.entity_kind == "team":
            return
        scope = HistoryScope(kind="agent", scope_id=continuation.entity_name)
    if scope.kind == "agent" and continuation.entity_name not in config.agents:
        return
    storage = await asyncio.to_thread(
        create_scope_session_storage,
        scope=scope,
        agent_name=continuation.entity_name,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )
    try:
        response = await asyncio.to_thread(storage.get_run, continuation.run_id)
        if isinstance(response, (RunOutput, TeamRunOutput)) and has_delegation_state(response):
            await _cancel_delegations(response, config=config, runtime_paths=runtime_paths, reason=reason)
            await asyncio.to_thread(
                storage.upsert_run,
                run=response,
                session_id=continuation.session_id,
                user_id=continuation.requester_id,
            )
    finally:
        storage.close()


async def _run_child(
    child: DelegationChild,
    *,
    toolkit: DelegateTools,
    config: Config,
    runtime_paths: RuntimePaths,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    decisions: dict[str, bool] | None,
    denial_reasons: dict[str, str | None] | None,
    fresh: bool,
) -> RunOutput:
    token = _RUNNING_CHILD_ID.set(child.delegation_id)
    try:
        async with child_audit_context(child, config=config, runtime_paths=runtime_paths):
            response = await run_with_tool_execution_identity(
                _child_identity(child),
                operation=lambda: _execute_child(
                    child,
                    toolkit=toolkit,
                    config=config,
                    runtime_paths=runtime_paths,
                    refresh_scheduler=refresh_scheduler,
                    decisions=decisions,
                    denial_reasons=denial_reasons,
                    fresh=fresh,
                ),
            )
            await record_child_response(child, response, config=config, runtime_paths=runtime_paths)
            return response
    finally:
        _RUNNING_CHILD_ID.reset(token)


async def _execute_child(  # noqa: C901
    child: DelegationChild,
    *,
    toolkit: DelegateTools,
    config: Config,
    runtime_paths: RuntimePaths,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    decisions: dict[str, bool] | None,
    denial_reasons: dict[str, str | None] | None,
    fresh: bool,
) -> RunOutput:
    """Start a fresh envelope or resume the exact stored child under its own scope."""
    # These imports cross the creation/envelope cycle only during execution.
    from mindroom.agents import create_agent  # noqa: PLC0415
    from mindroom.knowledge.utils import resolve_agent_knowledge_access_async  # noqa: PLC0415

    identity = _child_identity(child)
    context = get_tool_runtime_context()
    if context is None or context.requester_id != identity.requester_id or context.room_id != identity.room_id:
        msg = "Delegation requester context does not match its retained wait"
        raise RuntimeError(msg)
    child_context = replace(
        context,
        agent_name=child.child_agent_name,
        active_model_name=child.model_name,
        target=replace(context.target, session_id=child.session_id),
    )
    if fresh:
        return await _start_child_envelope(child, toolkit, config, runtime_paths, prompt=child.task)
    persisted = await _read_child(child, config, runtime_paths)
    if persisted is None:
        msg = "Delegated run was interrupted before retaining an outcome"
        raise RuntimeError(msg)
    if persisted.status != RunStatus.paused:
        return persisted
    if decisions is not None:
        await record_child_response(
            child,
            persisted,
            config=config,
            runtime_paths=runtime_paths,
            decisions=decisions,
            denial_reasons=denial_reasons,
        )
    knowledge = await resolve_agent_knowledge_access_async(
        child.child_agent_name,
        config,
        runtime_paths,
        refresh_scheduler=refresh_scheduler,
        execution_identity=identity,
    )
    storage = await asyncio.to_thread(
        create_session_storage,
        child.child_agent_name,
        config,
        runtime_paths,
        execution_identity=identity,
    )
    try:
        agent = await asyncio.to_thread(
            create_agent,
            child.child_agent_name,
            config,
            runtime_paths,
            identity,
            session_id=child.session_id,
            active_model_name=child.model_name,
            knowledge=knowledge.knowledge,
            history_storage=storage,
            refresh_scheduler=refresh_scheduler,
            delegation_depth=child.depth,
            supports_native_tool_approval=True,
            dynamic_tool_continuation=True,
            include_interactive_questions=False,
            tool_function_filter=context.tool_function_filter,
        )
    except BaseException:
        storage.close()
        raise
    try:
        if agent.model is not None:
            install_approval_receipt_hooks(agent.model, agent.fallback_config)
        session = await agent.aget_session(session_id=child.session_id, user_id=identity.requester_id)
        restore_native_history(agent.model, persisted_run=persisted, session=cast("AgentSession | None", session))
        with tool_runtime_context(child_context):
            response = await _continue_child(
                agent,
                child,
                persisted,
                config=config,
                runtime_paths=runtime_paths,
                identity=identity,
                refresh_scheduler=refresh_scheduler,
                decisions=decisions,
                denial_reasons=denial_reasons,
            )
    finally:
        close_agent_runtime_state_dbs(agent, shared_scope_storage=storage)
        storage.close()

    if response.status == RunStatus.completed:
        decision = continuation_decision_from_tools(response.tools, original_prompt=child.task, continuation_count=0)
        if decision.next_prompt is not None:
            if decision.model_switch_when == "after-toolcall" and decision.model_switch_name is not None:
                child.model_name = decision.model_switch_name
            child.run_id = uuid4().hex
            return await _start_child_envelope(child, toolkit, config, runtime_paths, prompt=decision.next_prompt)
    return response


async def _start_child_envelope(
    child: DelegationChild,
    toolkit: DelegateTools,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    prompt: str,
) -> RunOutput:
    """Run the normal response loop in the child's retained conversation scope."""
    from mindroom.response_turn import ResponsePausedForApproval  # noqa: PLC0415

    def note_child_run_id(run_id: str) -> None:
        child.run_id = run_id

    with suppress(ResponsePausedForApproval):
        await toolkit.run_delegated_task(
            child.child_agent_name,
            prompt,
            session_id=child.session_id,
            run_id=child.run_id,
            active_model_name=child.model_name,
            supports_native_tool_approval=True,
            run_id_callback=note_child_run_id,
        )
    response = await _read_child(child, config, runtime_paths)
    if response is None:
        msg = "Delegated execution did not retain its exact run outcome"
        raise RuntimeError(msg)
    return response


async def _continue_child(
    agent: Agent,
    child: DelegationChild,
    persisted: RunOutput,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    decisions: dict[str, bool] | None,
    denial_reasons: dict[str, str | None] | None,
) -> RunOutput:
    """Apply child decisions and drive any further nested delegations."""
    from mindroom.response_turn import apply_exact_approval_decisions  # noqa: PLC0415

    if has_delegation_state(persisted):
        return cast(
            "RunOutput",
            await drive_delegations(
                agent,
                persisted,
                agent_name=child.child_agent_name,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=identity,
                delegation_depth=child.depth,
                refresh_scheduler=refresh_scheduler,
                decisions=decisions,
                denial_reasons=denial_reasons,
            ),
        )
    if decisions is None:
        return persisted
    requirements = apply_exact_approval_decisions(
        deepcopy(persisted.requirements or []),
        decisions=decisions,
        denial_reasons=denial_reasons or {},
    )
    events = agent.acontinue_run(
        run_id=persisted.run_id,
        requirements=requirements,
        session_id=child.session_id,
        user_id=identity.requester_id,
        metadata=deepcopy(persisted.metadata),
        stream=True,
        stream_events=True,
        yield_run_output=True,
    )
    continued = None
    async for event in events:
        await observe_child_event(event)
        if isinstance(event, RunOutput):
            continued = event
    if continued is None:
        msg = "Delegated continuation did not yield its retained outcome"
        raise RuntimeError(msg)
    return cast(
        "RunOutput",
        await drive_delegations(
            agent,
            continued,
            agent_name=child.child_agent_name,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=identity,
            delegation_depth=child.depth,
            refresh_scheduler=refresh_scheduler,
        ),
    )


def _pending_child(state: DelegationState, child: DelegationChild, response: RunOutput) -> None:
    from mindroom.response_turn import paused_attempt_from_response  # noqa: PLC0415

    paused = paused_attempt_from_response(response, fallback_session_id=child.session_id, fallback_run_id=child.run_id)
    if paused is None:
        msg = "Delegated child paused without supported exact approval requirements"
        raise RuntimeError(msg)
    state.pending_child_id = child.delegation_id
    state.pending_agent_name = paused.approval_agent_name or child.child_agent_name
    for tool in paused.tools:
        projected = deepcopy(tool)
        projected.tool_call_id = f"{child.delegation_id}:{tool.tool_call_id}"
        state.pending_tools.append(projected.to_dict())
        requirement = RunRequirement(projected)
        state.pending_requirements.append(requirement.to_dict())


async def _resolved_child_tool(
    child: DelegationChild,
    response: RunOutput,
    call_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> ToolExecution | None:
    """Find an executed leaf by its complete delegation path, never its ID alone."""
    prefix = f"{child.delegation_id}:"
    if not call_id.startswith(prefix):
        return None
    local_id = call_id[len(prefix) :]
    for tool in response.tools or ():
        if tool.tool_call_id == local_id and not tool.is_paused:
            projected = deepcopy(tool)
            projected.tool_call_id = call_id
            return projected
    for descendant in DelegationState.from_metadata(response.metadata).children:
        if local_id.startswith(f"{descendant.delegation_id}:"):
            nested = await _read_child(descendant, config, runtime_paths)
            if nested is not None:
                tool = await _resolved_child_tool(
                    descendant,
                    nested,
                    local_id,
                    config=config,
                    runtime_paths=runtime_paths,
                )
                if tool is not None:
                    tool.tool_call_id = call_id
                return tool
    return None


def _settle_pending_child_tools(
    response: RunOutput | TeamRunOutput,
    pending_tools: list[dict[str, object]],
    on_event: Callable[[object], None],
    *,
    reason: str,
) -> None:
    """Close the current approval generation's visible tools after an interruption."""
    for pending_tool in pending_tools:
        cancelled_tool = ToolExecution.from_dict(pending_tool)
        cancelled_tool.requires_confirmation = False
        cancelled_tool.requires_user_input = False
        cancelled_tool.external_execution_required = False
        cancelled_tool.result = reason
        cancelled_tool.tool_call_error = True
        on_event(_child_completion_event(response, cancelled_tool))


def _child_completion_event(
    response: RunOutput | TeamRunOutput,
    tool: ToolExecution,
) -> ToolCallCompletedEvent | TeamToolCallCompletedEvent:
    """Complete a projected child call in the parent presentation's root scope."""
    event_type = TeamToolCallCompletedEvent if isinstance(response, TeamRunOutput) else ToolCallCompletedEvent
    return event_type(tool=tool, run_id=response.run_id, session_id=response.session_id)


async def drive_delegations(  # noqa: C901, PLR0912, PLR0915
    entity: Agent | Team,
    response: RunOutput | TeamRunOutput,
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    decisions: dict[str, bool] | None = None,
    denial_reasons: dict[str, str | None] | None = None,
    member_config_names: Mapping[str, str] | None = None,
    on_event: Callable[[object], None] | None = None,
) -> RunOutput | TeamRunOutput:
    """Advance one durable parent; sequential children preserve completed sibling results."""
    from mindroom.response_turn import apply_exact_approval_decisions  # noqa: PLC0415

    if not has_delegation_state(response):
        return response
    if execution_identity is None or execution_identity.channel != "matrix":
        msg = "Native delegation requires a Matrix execution owner"
        raise RuntimeError(msg)
    state = DelegationState.from_metadata(response.metadata)
    if not state.storage_bindings and isinstance(response, RunOutput):
        state.storage_bindings = freeze_delegation_storage(config, (agent_name,))
    pending_id = state.pending_child_id
    prior_pending_tools = state.pending_tools
    if decisions is not None:
        # Validate every presented identity before touching any child or gate.
        apply_exact_approval_decisions(
            [RunRequirement.from_dict(item) for item in state.pending_requirements],
            decisions=decisions,
            denial_reasons=denial_reasons or {},
        )
        if pending_id is None:
            ordinary = [item for item in response.requirements or () if item.needs_confirmation]
            ordinary_ids = {item.tool_execution.tool_call_id for item in ordinary if item.tool_execution}
            if set(decisions) == ordinary_ids:
                apply_exact_approval_decisions(ordinary, decisions=decisions, denial_reasons=denial_reasons or {})
            else:
                state.gates.update(decisions)
    state.clear_pending()
    while response.status == RunStatus.paused:
        external = _external_requirements(response)
        ordinary = [requirement for requirement in response.requirements or () if requirement.needs_confirmation]
        if ordinary:
            state.pending_requirements = [requirement.to_dict() for requirement in ordinary]
            state.pending_tools = [
                requirement.tool_execution.to_dict() for requirement in ordinary if requirement.tool_execution
            ]
            await _persist(entity, response, state)
            return response
        if not external and any(not item.is_resolved() for item in response.requirements or ()):
            return response
        for requirement in external:
            tool = requirement.tool_execution
            if tool is None or not tool.tool_call_id:
                msg = "Delegation requirement has no exact tool-call identity"
                raise RuntimeError(msg)
            requirement_key = (
                f"{requirement.member_agent_id}:{tool.tool_call_id}"
                if requirement.member_agent_id
                else tool.tool_call_id
            )
            if on_event is not None:
                on_event(
                    ToolCallStartedEvent(
                        tool=deepcopy(tool),
                        run_id=response.run_id,
                        session_id=response.session_id,
                        agent_id=requirement.member_agent_id or agent_name,
                    ),
                )
            caller = agent_name
            if requirement.member_agent_id is not None:
                caller = (member_config_names or {}).get(requirement.member_agent_id, "")
                if not caller:
                    msg = "Delegation requirement has no frozen member config identity"
                    raise RuntimeError(msg)
            toolkit = _toolkit(caller, config, runtime_paths, execution_identity, delegation_depth, refresh_scheduler)
            args = tool.tool_args or {}
            child_name, task = args.get("agent_name"), args.get("task")
            if not isinstance(child_name, str) or not isinstance(task, str):
                requirement.set_external_execution_result("Cannot delegate: agent_name and task must be strings.")
                continue
            authorization = toolkit.authorize(child_name, task)
            if isinstance(authorization, str):
                retained = next((item for item in state.children if item.parent_requirement_id == requirement.id), None)
                if retained is not None:
                    await _settle_interrupted_child(
                        retained,
                        config=config,
                        runtime_paths=runtime_paths,
                        reason=authorization,
                    )
                    if pending_id == retained.delegation_id and on_event is not None:
                        _settle_pending_child_tools(response, prior_pending_tools, on_event, reason=authorization)
                requirement.set_external_execution_result(authorization)
                if requirement.id in state.hooks:
                    await after_delegation(
                        state.hooks[requirement.id],
                        config=config,
                        runtime_paths=runtime_paths,
                        result=authorization,
                    )
                continue
            if tool_may_require_approval(config, "run_subagent") and requirement_key not in state.gates:
                projected = deepcopy(tool)
                projected.external_execution_required = False
                projected.requires_confirmation = True
                projected.approval_type = POLICY_CONFIRMATION_APPROVAL_TYPE
                projected.tool_call_id = requirement_key
                state.pending_tools = [projected.to_dict()]
                state.pending_requirements = [RunRequirement(projected).to_dict()]
                state.pending_agent_name = caller
                await _persist(entity, response, state)
                return response
            if state.gates.get(requirement_key) is False:
                requirement.set_external_execution_result("Delegation denied by requester; child was not executed.")
                continue
            if requirement.id not in state.hooks:
                state.hooks[requirement.id] = await before_delegation(
                    execution_identity=replace(execution_identity, agent_name=caller),
                    arguments=args,
                    config=config,
                    runtime_paths=runtime_paths,
                )
                await _persist(entity, response, state)
            hook_state = state.hooks[requirement.id]
            if hook_state.blocked_result is not None:
                requirement.set_external_execution_result(hook_state.blocked_result)
                await after_delegation(
                    hook_state,
                    config=config,
                    runtime_paths=runtime_paths,
                    result=hook_state.blocked_result,
                )
                await _persist(entity, response, state)
                continue
            child = next((item for item in state.children if item.parent_requirement_id == requirement.id), None)
            fresh = child is None
            if child is None:
                delegation_id = uuid4().hex
                session_id = f"delegate:{caller}:{child_name}:{delegation_id}"
                identity = replace(execution_identity, agent_name=child_name, session_id=session_id)
                model_name = config.resolve_runtime_model(
                    entity_name=child_name,
                    room_id=identity.room_id,
                    thread_id=identity.resolved_thread_id,
                    runtime_paths=runtime_paths,
                ).model_name
                child = DelegationChild(
                    delegation_id,
                    tool.tool_call_id,
                    caller,
                    child_name,
                    task,
                    session_id,
                    uuid4().hex,
                    model_name,
                    delegation_depth + 1,
                    serialize_tool_execution_identity(identity),
                    parent_requirement_id=requirement.id,
                    storage_bindings=freeze_delegation_storage(config, (caller, child_name)),
                )
                state.children.append(child)
                await start_child_record(
                    child,
                    parent_run_id=response.run_id,
                    parent_delegation_id=_RUNNING_CHILD_ID.get(),
                    config=config,
                    runtime_paths=runtime_paths,
                    caller_execution_identity=replace(execution_identity, agent_name=caller),
                )
                # The stable child ID lands before any child side effect.
                await _persist(entity, response, state)
            if child.result is None:
                if child.storage_bindings != freeze_delegation_storage(config, child.storage_bindings):
                    msg = "Delegation storage scope changed while awaiting approval"
                    raise RuntimeError(msg)
                if _child_identity(child) != replace(
                    execution_identity,
                    agent_name=child.child_agent_name,
                    session_id=child.session_id,
                ):
                    msg = "Delegation child scope no longer matches its parent execution identity"
                    raise RuntimeError(msg)
                if (child.caller_agent_name, child.child_agent_name, child.task, child.depth) != (
                    caller,
                    child_name,
                    task,
                    delegation_depth + 1,
                ):
                    msg = "Delegation child no longer matches its parent requirement"
                    raise RuntimeError(msg)
                child_decisions = None
                child_reasons = None
                if pending_id == child.delegation_id and decisions is not None:
                    prefix = f"{child.delegation_id}:"
                    child_decisions = {key.removeprefix(prefix): value for key, value in decisions.items()}
                    child_reasons = {key.removeprefix(prefix): value for key, value in (denial_reasons or {}).items()}
                    decisions = None
                    pending_id = None
                try:
                    child_response = await _run_child(
                        child,
                        toolkit=toolkit,
                        config=authorization,
                        runtime_paths=runtime_paths,
                        refresh_scheduler=refresh_scheduler,
                        decisions=child_decisions,
                        denial_reasons=child_reasons,
                        fresh=fresh,
                    )
                    if child_decisions is not None and on_event is not None:
                        for pending_tool in prior_pending_tools:
                            completed_tool = await _resolved_child_tool(
                                child,
                                child_response,
                                str(pending_tool["tool_call_id"]),
                                config=config,
                                runtime_paths=runtime_paths,
                            )
                            if completed_tool is not None:
                                on_event(_child_completion_event(response, completed_tool))
                    if child_response.status == RunStatus.paused:
                        child.status = "paused"
                        _pending_child(state, child, child_response)
                        await _persist(entity, response, state)
                        return response
                except asyncio.CancelledError:
                    await _settle_interrupted_child(
                        child,
                        config=config,
                        runtime_paths=runtime_paths,
                        reason="Delegation cancelled.",
                    )
                    await after_delegation(
                        hook_state,
                        config=config,
                        runtime_paths=runtime_paths,
                        result=child.result,
                        error=asyncio.CancelledError("Delegation cancelled."),
                    )
                    await _persist(entity, response, state)
                    raise
                except Exception as error:
                    await _settle_interrupted_child(
                        child,
                        config=config,
                        runtime_paths=runtime_paths,
                        reason=str(error),
                        status="failed",
                    )
                    if child_decisions is not None and on_event is not None:
                        _settle_pending_child_tools(response, prior_pending_tools, on_event, reason=str(error))
                await _persist(entity, response, state)
            receipt = await finish_child_record(child, config=config, runtime_paths=runtime_paths)
            result = child.result or "Agent completed the task but returned no content."
            if child.status != "completed":
                result = f"Delegation to '{child_name}' {child.status}: {result}"
            requirement.set_external_execution_result(f"{result}\n\n{receipt}")
            await after_delegation(
                hook_state,
                config=config,
                runtime_paths=runtime_paths,
                result=f"{result}\n\n{receipt}",
            )
            if on_event is not None:
                on_event(
                    ToolCallCompletedEvent(
                        tool=deepcopy(requirement.tool_execution),
                        run_id=response.run_id,
                        session_id=response.session_id,
                        agent_id=requirement.member_agent_id or agent_name,
                    ),
                )
        await _persist(entity, response, state)
        if isinstance(response, RunOutput):
            continuation_stream = cast("Agent", entity).acontinue_run(
                run_id=response.run_id,
                requirements=list(response.requirements or ()),
                session_id=response.session_id,
                user_id=execution_identity.requester_id,
                metadata=deepcopy(response.metadata),
                stream=True,
                stream_events=True,
                yield_run_output=True,
            )
        else:
            continuation_stream = cast("Team", entity).acontinue_run(
                run_response=response,
                requirements=list(response.requirements or ()),
                session_id=response.session_id,
                user_id=execution_identity.requester_id,
                metadata=deepcopy(response.metadata),
                stream=True,
                stream_events=True,
                yield_run_output=True,
            )
        continued = None
        async for event in continuation_stream:
            if isinstance(event, (RunOutput, TeamRunOutput)):
                continued = event
            elif not isinstance(event, (RunPausedEvent, TeamRunPausedEvent)) and on_event is not None:
                on_event(event)
        if continued is None:
            msg = "Delegation continuation did not yield its terminal run"
            raise RuntimeError(msg)
        response = continued
        state.clear_pending()
        await _persist(entity, response, state)
    return response


async def drive_delegation_stream(
    entity: Agent | Team,
    events: AsyncIterator[object],
    **kwargs: Unpack[_DelegationOptions],
) -> AsyncIterator[object]:
    """Drain native pauses before driving children, preserving Agno's stored status."""
    paused_event: RunPausedEvent | TeamRunPausedEvent | None = None
    response: RunOutput | TeamRunOutput | None = None
    async for event in events:
        if isinstance(event, (RunPausedEvent, TeamRunPausedEvent)):
            paused_event = event
        elif isinstance(event, (RunOutput, TeamRunOutput)):
            response = event
        else:
            yield event
    if response is None and paused_event is not None:
        response = await _load_paused_output(entity, paused_event)
    if response is None or response.status != RunStatus.paused or not has_delegation_state(response):
        for terminal in filter(None, (paused_event, response)):
            yield terminal
        return
    driven = response
    async for event in _stream_driven_run(entity, response, cast("_DelegationOptions", kwargs)):
        if isinstance(event, (RunOutput, TeamRunOutput)):
            driven = event
        else:
            yield event
    if driven.status == RunStatus.paused:
        yield _project_pause_event(paused_event, driven)
    yield driven


async def _load_paused_output(
    entity: Agent | Team,
    event: RunPausedEvent | TeamRunPausedEvent,
) -> RunOutput | TeamRunOutput | None:
    if event.run_id is None:
        msg = "Delegation pause has no persisted run identity"
        raise RuntimeError(msg)
    return await entity.aget_run_output(event.run_id, session_id=event.session_id)


def _project_pause_event(
    event: RunPausedEvent | TeamRunPausedEvent | None,
    response: RunOutput | TeamRunOutput,
) -> RunPausedEvent | TeamRunPausedEvent:
    state = DelegationState.from_metadata(response.metadata)
    if event is None:
        pause_type = TeamRunPausedEvent if isinstance(response, TeamRunOutput) else RunPausedEvent
        event = pause_type(run_id=response.run_id, session_id=response.session_id)
    event.tools = [ToolExecution.from_dict(item) for item in state.pending_tools]
    event.requirements = [RunRequirement.from_dict(item) for item in state.pending_requirements]
    return event


async def _stream_driven_run(
    entity: Agent | Team,
    response: RunOutput | TeamRunOutput,
    options: _DelegationOptions,
) -> AsyncIterator[object]:
    queue: asyncio.Queue[object] = asyncio.Queue()

    async def drive() -> RunOutput | TeamRunOutput:
        try:
            return await drive_delegations(entity, response, on_event=queue.put_nowait, **options)
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(drive())
    try:
        while (event := await queue.get()) is not None:
            yield event
        yield await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
