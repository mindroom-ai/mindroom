"""Drive durable external delegation requirements without retaining Python waits."""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, NotRequired, TypedDict, Unpack, cast
from uuid import uuid4

from agno.db.base import BaseDb
from agno.models.response import ToolExecution
from agno.run.agent import RunErrorEvent, RunOutput, RunPausedEvent, ToolCallCompletedEvent, ToolCallStartedEvent
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import RunPausedEvent as TeamRunPausedEvent
from agno.run.team import TeamRunOutput
from agno.run.team import ToolCallCompletedEvent as TeamToolCallCompletedEvent

from mindroom.agent_storage import create_session_storage
from mindroom.approval_receipt import install_approval_receipt_hooks
from mindroom.approval_tools import (
    approval_denial_context,
    required_approval_tool_names,
    toolkit_owners_for_agents,
    validate_approval_tool_owners,
)
from mindroom.background_tasks import wait_for_future_until_complete
from mindroom.delegation.hooks import after_delegation, before_delegation
from mindroom.delegation.lifecycle import (
    authorize_delegation,
    child_execution_identity,
    child_run_context,
    finish_child_turn,
    note_child_run_id,
    observe_child_event,
    prepare_child_turn,
    reserve_child_turn,
    settle_child_response,
    start_child_turn,
)
from mindroom.delegation.recovery import interrupt_child, read_child_run, resolve_subagent
from mindroom.delegation.sessions import (
    SubagentSessionError,
    subagent_liveness,
)
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationChild, DelegationPendingTool, DelegationState
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.dynamic_tool_continuation import continuation_decision_from_tools
from mindroom.error_handling import run_error_event_text
from mindroom.history.native import restore_native_history
from mindroom.history.session_context import close_agent_runtime_state_dbs
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_approval import POLICY_CONFIRMATION_APPROVAL_TYPE, tool_may_require_approval
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.output_files import (
    OUTPUT_PATH_ARGUMENT,
    ToolOutputFilePolicy,
    ToolOutputFileRequest,
    finalize_tool_output_file,
    normalize_output_path_argument,
    prepare_tool_output_file,
)
from mindroom.tool_system.runtime_context import get_tool_runtime_context, tool_runtime_context
from mindroom.tool_system.worker_routing import (
    run_with_tool_execution_identity,
)
from mindroom.workspaces import resolve_agent_workspace_from_state_path

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from agno.agent import Agent
    from agno.session.agent import AgentSession
    from agno.team import Team

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import ChildResponseRunner
    from mindroom.event_journal import ApprovalCall
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


class _DelegationOptions(TypedDict):
    """Explicit dependencies shared by blocking and streaming delegation drivers."""

    agent_name: str
    run_child: ChildResponseRunner
    config: Config
    runtime_paths: RuntimePaths
    execution_identity: ToolExecutionIdentity | None
    delegation_depth: NotRequired[int]
    refresh_scheduler: NotRequired[KnowledgeRefreshScheduler | None]
    decisions: NotRequired[dict[str, bool] | None]
    denial_reasons: NotRequired[dict[str, str | None] | None]
    approval_calls: NotRequired[Sequence[ApprovalCall]]
    member_config_names: NotRequired[Mapping[str, str] | None]


_RUNNING_CHILD_ID: ContextVar[str | None] = ContextVar("running_delegated_child", default=None)


@dataclass(frozen=True)
class _ChildOutcome:
    """Retained outcome and executable ownership observed by its actual envelope."""

    response: RunOutput
    toolkit_owners: dict[tuple[str, str], str | None]


def _external_requirements(response: RunOutput | TeamRunOutput) -> list[RunRequirement]:
    return [
        requirement
        for requirement in response.requirements or ()
        if requirement.needs_external_execution
        and requirement.tool_execution is not None
        and requirement.tool_execution.tool_name in {"run_subagent", "continue_subagent"}
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


async def _run_child(
    child: DelegationChild,
    *,
    run_child: ChildResponseRunner,
    config: Config,
    runtime_paths: RuntimePaths,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    decisions: dict[str, bool] | None,
    denial_reasons: dict[str, str | None] | None,
    approval_calls: Sequence[ApprovalCall],
    fresh: bool,
) -> _ChildOutcome:
    token = _RUNNING_CHILD_ID.set(child.delegation_id)
    try:
        async with child_run_context(child, config=config, runtime_paths=runtime_paths) as observation:
            response = await run_with_tool_execution_identity(
                child_execution_identity(child),
                operation=lambda: _execute_child(
                    child,
                    run_child=run_child,
                    config=config,
                    runtime_paths=runtime_paths,
                    refresh_scheduler=refresh_scheduler,
                    decisions=decisions,
                    denial_reasons=denial_reasons,
                    approval_calls=approval_calls,
                    fresh=fresh,
                ),
            )
            observation.response = response.response
            observation.terminal = None
            return response
    finally:
        _RUNNING_CHILD_ID.reset(token)


async def _execute_child(
    child: DelegationChild,
    *,
    run_child: ChildResponseRunner,
    config: Config,
    runtime_paths: RuntimePaths,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    decisions: dict[str, bool] | None,
    denial_reasons: dict[str, str | None] | None,
    approval_calls: Sequence[ApprovalCall],
    fresh: bool,
) -> _ChildOutcome:
    """Start a fresh envelope or resume the exact stored child under its own scope."""
    # These imports cross the creation/envelope cycle only during execution.
    from mindroom.agents import create_agent  # noqa: PLC0415
    from mindroom.knowledge.utils import resolve_agent_knowledge_access_async  # noqa: PLC0415
    from mindroom.response_turn import apply_local_approval_decisions  # noqa: PLC0415

    identity = child_execution_identity(child)
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
        return await _start_child_envelope(
            child,
            run_child,
            config,
            runtime_paths,
            prompt=child.task,
            refresh_scheduler=refresh_scheduler,
        )
    persisted = await read_child_run(child, config, runtime_paths)
    if persisted is None:
        msg = "Delegated run was interrupted before retaining an outcome"
        raise RuntimeError(msg)
    if persisted.status != RunStatus.paused or decisions is None:
        # A retained pause must never acquire ownership from a later reconstruction.
        return _ChildOutcome(persisted, {})
    child_state = DelegationState.from_metadata(persisted.metadata)
    local_calls = () if child_state.pending_child_id is not None else approval_calls
    approved_calls = tuple(call for call in local_calls if decisions.get(call.tool_call_id))
    required_tool_names = await required_approval_tool_names(
        child.child_agent_name,
        approved_calls,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )
    if decisions is not None:
        await settle_child_response(
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
            required_tool_names=required_tool_names,
        )
    except BaseException:
        storage.close()
        raise
    try:
        if agent.model is not None:
            install_approval_receipt_hooks(agent.model, agent.fallback_config)
        session = await agent.aget_session(session_id=child.session_id, user_id=identity.requester_id)
        restore_native_history(agent.model, persisted_run=persisted, session=cast("AgentSession | None", session))
        requirements = apply_local_approval_decisions(
            persisted,
            decisions=decisions,
            denial_reasons=denial_reasons or {},
        )
        validate_approval_tool_owners([agent], approved_calls, requirements)
        with (
            tool_runtime_context(child_context),
            approval_denial_context(
                agent,
                {child.run_id: tuple(call for call in local_calls if not decisions.get(call.tool_call_id))},
            ),
        ):
            response = await _continue_child(
                agent,
                run_child,
                child,
                persisted,
                config=config,
                runtime_paths=runtime_paths,
                identity=identity,
                refresh_scheduler=refresh_scheduler,
                decisions=decisions,
                denial_reasons=denial_reasons,
                approval_calls=approval_calls,
            )
        toolkit_owners = toolkit_owners_for_agents([agent])
    finally:
        close_agent_runtime_state_dbs(agent, shared_scope_storage=storage)
        storage.close()

    if response.status == RunStatus.completed:
        decision = continuation_decision_from_tools(response.tools, original_prompt=child.task, continuation_count=0)
        if decision.next_prompt is not None:
            model_name = (
                decision.model_switch_name
                if decision.model_switch_when == "after-toolcall" and decision.model_switch_name is not None
                else child.model_name
            )
            note_child_run_id(child, uuid4().hex, runtime_paths, model_name=model_name)
            return await _start_child_envelope(
                child,
                run_child,
                config,
                runtime_paths,
                prompt=decision.next_prompt,
                refresh_scheduler=refresh_scheduler,
            )
    return _ChildOutcome(response, toolkit_owners)


async def _start_child_envelope(
    child: DelegationChild,
    run_child: ChildResponseRunner,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    prompt: str,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
) -> _ChildOutcome:
    """Run the normal response loop in the child's retained conversation scope."""
    from mindroom.response_turn import ResponsePausedForApproval  # noqa: PLC0415

    result = None
    toolkit_owners = {}
    try:
        result = await run_child(
            child,
            prompt=prompt,
            config=config,
            runtime_paths=runtime_paths,
            refresh_scheduler=refresh_scheduler,
            supports_native_tool_approval=True,
        )
    except ResponsePausedForApproval as suspension:
        toolkit_owners = suspension.paused.toolkit_owners
        if suspension.paused.runtime_model_name is not None:
            note_child_run_id(child, child.run_id, runtime_paths, model_name=suspension.paused.runtime_model_name)
    response = await read_child_run(child, config, runtime_paths)
    if response is None:
        msg = result or "Delegated execution did not retain its exact run outcome"
        raise RuntimeError(msg)
    return _ChildOutcome(response, toolkit_owners)


async def _continue_child(
    agent: Agent,
    run_child: ChildResponseRunner,
    child: DelegationChild,
    persisted: RunOutput,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    decisions: dict[str, bool] | None,
    denial_reasons: dict[str, str | None] | None,
    approval_calls: Sequence[ApprovalCall],
) -> RunOutput:
    """Apply child decisions and drive any further nested delegations."""
    from mindroom.response_turn import apply_exact_approval_decisions  # noqa: PLC0415

    if not has_delegation_state(persisted):
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
        error_event: RunErrorEvent | None = None
        async for event in events:
            await observe_child_event(event)
            if isinstance(event, RunOutput):
                continued = event
            elif isinstance(event, RunErrorEvent):
                error_event = event
        if error_event is not None and (continued is None or continued.status == RunStatus.error):
            raise RuntimeError(run_error_event_text(error_event))
        if continued is None:
            msg = "Delegated continuation did not yield its retained outcome"
            raise RuntimeError(msg)
        persisted = continued
        decisions = None
        denial_reasons = None
        approval_calls = ()
    return cast(
        "RunOutput",
        await drive_delegations(
            agent,
            persisted,
            agent_name=child.child_agent_name,
            run_child=run_child,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=identity,
            delegation_depth=child.depth,
            refresh_scheduler=refresh_scheduler,
            decisions=decisions,
            denial_reasons=denial_reasons,
            approval_calls=approval_calls,
        ),
    )


def _pending_child(state: DelegationState, child: DelegationChild, outcome: _ChildOutcome) -> None:
    from mindroom.response_turn import paused_attempt_from_response  # noqa: PLC0415

    response = outcome.response
    paused = paused_attempt_from_response(
        response,
        fallback_session_id=child.session_id,
        fallback_run_id=child.run_id,
        toolkit_owners=outcome.toolkit_owners,
    )
    if paused is None:
        msg = "Delegated child paused without supported exact approval requirements"
        raise RuntimeError(msg)
    state.pending_child_id = child.delegation_id
    state.pending_agent_name = paused.approval_agent_name or child.child_agent_name
    child_state = DelegationState.from_metadata(response.metadata)
    for tool in paused.tools:
        projected = deepcopy(tool)
        projected.tool_call_id = f"{child.delegation_id}:{tool.tool_call_id}"
        state.pending_tools.append(projected.to_dict())
        state.pending_tool_sources[projected.tool_call_id] = deepcopy(
            child_state.pending_tool_sources.get(str(tool.tool_call_id))
            or DelegationPendingTool(
                child=child,
                tool_call_id=str(tool.tool_call_id),
                toolkit_name=paused.toolkit_owners.get((child.child_agent_name, str(tool.tool_name))),
            ),
        )
        requirement = RunRequirement(projected)
        state.pending_requirements.append(requirement.to_dict())


async def _resolved_child_tool(
    source: DelegationPendingTool,
    call_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> ToolExecution | None:
    """Read the exact approved attempt, even after the child starts another run."""
    response = await read_child_run(source.child, config, runtime_paths)
    if response is None:
        return None
    for tool in response.tools or ():
        if tool.tool_call_id == source.tool_call_id and not tool.is_paused:
            projected = deepcopy(tool)
            projected.tool_call_id = call_id
            return projected
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


def _prepare_delegation_output(
    caller: str,
    config: Config,
    runtime_paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    raw_path: object,
    tool_name: str = "run_subagent",
) -> ToolOutputFileRequest | dict[str, object] | None:
    """Resolve the caller's output policy without scaffolding or reconciling its workspace."""
    storage = resolve_agent_storage(caller, config, runtime_paths, execution_identity=identity)
    workspace = resolve_agent_workspace_from_state_path(
        caller,
        config,
        runtime_paths=runtime_paths,
        state_storage_path=storage.state_root,
        use_state_storage_path=storage.execution.policy.private_workspace_enabled,
    )
    if workspace is None:
        if normalize_output_path_argument(raw_path) is not None:
            return {
                "mindroom_tool_output": {"status": "error", "error": "Output redirection requires an agent workspace."},
            }
        return None
    policy = ToolOutputFilePolicy.from_runtime(
        workspace.root,
        runtime_paths,
        auto_save_threshold_bytes=config.defaults.tool_output_auto_save_threshold_bytes,
    )
    return prepare_tool_output_file(policy, tool_name=tool_name, output_path=raw_path)


def _resolve_delegation_requirement(
    requirement: RunRequirement,
    result: str,
    response: RunOutput | TeamRunOutput,
    agent_name: str,
    on_event: Callable[[object], None] | None,
    *,
    output_request: ToolOutputFileRequest | None = None,
) -> str:
    """Resolve one external call and close its live tool trace, including rejections."""
    if output_request is not None:
        formatted = finalize_tool_output_file(output_request, result)
        result = formatted if isinstance(formatted, str) else json.dumps(formatted)
    requirement.set_external_execution_result(result)
    if on_event is not None:
        on_event(
            ToolCallCompletedEvent(
                tool=deepcopy(requirement.tool_execution),
                run_id=response.run_id,
                session_id=response.session_id,
                agent_id=requirement.member_agent_id or agent_name,
            ),
        )

    return result


async def drive_delegations(  # noqa: C901, PLR0912, PLR0915
    entity: Agent | Team,
    response: RunOutput | TeamRunOutput,
    *,
    agent_name: str,
    run_child: ChildResponseRunner,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    decisions: dict[str, bool] | None = None,
    denial_reasons: dict[str, str | None] | None = None,
    approval_calls: Sequence[ApprovalCall] = (),
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
    prior_tool_sources = state.pending_tool_sources
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
            caller_identity = replace(execution_identity, agent_name=caller)
            resolve_result = partial(
                _resolve_delegation_requirement,
                requirement,
                response=response,
                agent_name=agent_name,
                on_event=on_event,
            )
            args = tool.tool_args or {}
            retained = next((item for item in state.children if item.parent_requirement_id == requirement.id), None)
            previous_child = None
            if tool.tool_name == "continue_subagent":
                subagent_id, task = args.get("subagent_id"), args.get("message")
                if not isinstance(subagent_id, str) or not isinstance(task, str):
                    resolve_result("Cannot continue: subagent_id and message must be strings.")
                    continue
                try:
                    previous_child = retained or await resolve_subagent(
                        subagent_id,
                        owner=caller_identity,
                        config=config,
                        runtime_paths=runtime_paths,
                        depth=delegation_depth,
                    )
                except SubagentSessionError as error:
                    resolve_result(str(error))
                    continue
                if previous_child.subagent_id != subagent_id:
                    msg = "Subagent ID no longer matches its retained requirement"
                    raise RuntimeError(msg)
                child_name = previous_child.child_agent_name
            else:
                child_name, task = args.get("agent_name"), args.get("task")
                if child_name is None:
                    child_name = caller
            if not isinstance(child_name, str) or not isinstance(task, str):
                resolve_result("Cannot delegate: task must be a string and agent_name must be a string or null.")
                continue
            authorization = authorize_delegation(
                caller,
                child_name,
                task,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=caller_identity,
                depth=delegation_depth,
            )
            output_request = None
            if not isinstance(authorization, str):
                prepared_output = _prepare_delegation_output(
                    caller,
                    config,
                    runtime_paths,
                    caller_identity,
                    args.get(OUTPUT_PATH_ARGUMENT),
                    tool_name=tool.tool_name or "run_subagent",
                )
                if isinstance(prepared_output, dict):
                    authorization = json.dumps(prepared_output)
                else:
                    output_request = prepared_output
            if isinstance(authorization, str):
                if retained is not None:
                    await interrupt_child(
                        retained,
                        config=config,
                        runtime_paths=runtime_paths,
                        reason=authorization,
                    )
                    if pending_id == retained.delegation_id and on_event is not None:
                        _settle_pending_child_tools(response, prior_pending_tools, on_event, reason=authorization)
                resolve_result(authorization)
                if requirement.id in state.hooks:
                    await after_delegation(
                        state.hooks[requirement.id],
                        config=config,
                        runtime_paths=runtime_paths,
                        result=authorization,
                    )
                continue
            if output_request is not None:
                resolve_result = partial(resolve_result, output_request=output_request)
            if (
                tool_may_require_approval(config, tool.tool_name or "run_subagent")
                and requirement_key not in state.gates
            ):
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
                resolve_result("Delegation denied by requester; child was not executed.")
                continue
            if requirement.id not in state.hooks:
                state.hooks[requirement.id] = await before_delegation(
                    execution_identity=caller_identity,
                    arguments=args,
                    tool_name=tool.tool_name or "run_subagent",
                    config=config,
                    runtime_paths=runtime_paths,
                )
                await _persist(entity, response, state)
            hook_state = state.hooks[requirement.id]
            if hook_state.blocked_result is not None:
                blocked_result = resolve_result(hook_state.blocked_result)
                await after_delegation(
                    hook_state,
                    config=config,
                    runtime_paths=runtime_paths,
                    result=blocked_result,
                )
                await _persist(entity, response, state)
                continue
            child = retained
            fresh = child is None
            if child is None:
                child = prepare_child_turn(
                    caller,
                    child_name,
                    task,
                    owner=execution_identity,
                    config=config,
                    runtime_paths=runtime_paths,
                    depth=delegation_depth,
                    previous=previous_child,
                    parent_tool_call_id=tool.tool_call_id,
                    parent_requirement_id=requirement.id,
                )
                state.children.append(child)
            if child.result is None:
                if child.storage_bindings != freeze_delegation_storage(config, child.storage_bindings):
                    msg = "Delegation storage scope changed while awaiting approval"
                    raise RuntimeError(msg)
                if replace(child_execution_identity(child), thread_id=execution_identity.resolved_thread_id) != replace(
                    execution_identity,
                    thread_id=execution_identity.resolved_thread_id,
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
                child_calls = ()
                if pending_id == child.delegation_id and decisions is not None:
                    prefix = f"{child.delegation_id}:"
                    child_decisions = {key.removeprefix(prefix): value for key, value in decisions.items()}
                    child_reasons = {key.removeprefix(prefix): value for key, value in (denial_reasons or {}).items()}
                    child_calls = tuple(
                        replace(call, tool_call_id=call.tool_call_id.removeprefix(prefix)) for call in approval_calls
                    )
                    decisions = None
                    pending_id = None
                try:
                    if fresh:
                        await start_child_turn(
                            child,
                            parent_run_id=response.run_id,
                            parent_delegation_id=_RUNNING_CHILD_ID.get(),
                            config=config,
                            runtime_paths=runtime_paths,
                            caller_execution_identity=caller_identity,
                        )
                        # Persist the child before execution, with startup covered by cleanup.
                        await _persist(entity, response, state)
                    async with subagent_liveness(child, runtime_paths):
                        await reserve_child_turn(
                            child,
                            owner=caller_identity,
                            runtime_paths=runtime_paths,
                        )
                        child_outcome = await _run_child(
                            child,
                            run_child=run_child,
                            config=authorization,
                            runtime_paths=runtime_paths,
                            refresh_scheduler=refresh_scheduler,
                            decisions=child_decisions,
                            denial_reasons=child_reasons,
                            approval_calls=child_calls,
                            fresh=fresh,
                        )
                    if child_decisions is not None and on_event is not None:
                        for pending_tool in prior_pending_tools:
                            completed_tool = await _resolved_child_tool(
                                prior_tool_sources[str(pending_tool["tool_call_id"])],
                                str(pending_tool["tool_call_id"]),
                                config=config,
                                runtime_paths=runtime_paths,
                            )
                            if completed_tool is not None:
                                on_event(_child_completion_event(response, completed_tool))
                    if child_outcome.response.status == RunStatus.paused:
                        _pending_child(state, child, child_outcome)
                        await _persist(entity, response, state)
                        return response
                except asyncio.CancelledError:
                    await interrupt_child(
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
                    await interrupt_child(
                        child,
                        config=config,
                        runtime_paths=runtime_paths,
                        reason=str(error),
                        status="failed",
                    )
                    if child_decisions is not None and on_event is not None:
                        _settle_pending_child_tools(response, prior_pending_tools, on_event, reason=str(error))
                await _persist(entity, response, state)
            receipt = await finish_child_turn(child, config=config, runtime_paths=runtime_paths)
            result = child.result or "Agent completed the task but returned no content."
            if child.status != "completed":
                result = f"Delegation to '{child_name}' {child.status}: {result}"
            result = resolve_result(f"{result}\n\n{receipt}")
            await after_delegation(
                hook_state,
                config=config,
                runtime_paths=runtime_paths,
                result=result,
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
        error_event: RunErrorEvent | TeamRunErrorEvent | None = None
        async with closing_async_stream(continuation_stream):
            async for event in continuation_stream:
                if isinstance(event, (RunErrorEvent, TeamRunErrorEvent)):
                    error_event = event
                if isinstance(event, (RunOutput, TeamRunOutput)):
                    continued = event
                elif not isinstance(event, (RunPausedEvent, TeamRunPausedEvent)) and on_event is not None:
                    on_event(event)
        entity_label = "Team" if isinstance(response, TeamRunOutput) else "Agent"
        if continued is None:
            if error_event is not None:
                raise RuntimeError(run_error_event_text(error_event, entity_label=entity_label))
            msg = "Delegation continuation did not yield its terminal run"
            raise RuntimeError(msg)
        response = continued
        state.clear_pending()
        await _persist(entity, response, state)
        if error_event is not None and response.status == RunStatus.error:
            raise RuntimeError(run_error_event_text(error_event, entity_label=entity_label))
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
    driven_stream = _stream_driven_run(entity, response, cast("_DelegationOptions", kwargs))
    async with closing_async_stream(driven_stream):
        async for event in driven_stream:
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
            await wait_for_future_until_complete(asyncio.gather(task, return_exceptions=True))
        elif not task.cancelled():
            # An early-closing consumer may never reach the await above.
            # Observe failure without replacing its error or cancellation.
            task.exception()
