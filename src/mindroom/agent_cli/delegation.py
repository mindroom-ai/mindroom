"""Hidden external calls reuse the native child operation and approval snapshot."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from mindroom.agent_cli.approval import CliApprovalCall
from mindroom.delegation.execution import advance_delegation_call, persist_delegation_state, prepare_delegation_state
from mindroom.delegation.state import DELEGATION_FUNCTIONS
from mindroom.event_journal import ApprovalCall, ApprovalDecision, approval_arguments_digest
from mindroom.llm.runs import RunStatus
from mindroom.response_turn import PausedAttempt, paused_attempt_from_response
from mindroom.tool_system.runtime_context import tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.llm.runs import RunRequirement, ToolExecution
    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolBinding


def approval_calls_for_cli_pause(
    paused: PausedAttempt,
    agent_name: str,
    resolved: Sequence[RunRequirement],
) -> tuple[ApprovalCall, ...]:
    """Return the calls of ``paused`` that a CLI approval decided, each with its ``resolved`` requirement's decision.

    Retain native ownership while the response coordinator owns claim/expiry.
    """
    invoking_agent = paused.approval_agent_name or agent_name
    calls: list[ApprovalCall] = []
    for requirement in resolved:
        tool = requirement.tool_execution
        calls.append(
            ApprovalCall(
                tool_call_id=tool.tool_call_id,
                tool_name=tool.tool_name,
                invoking_agent=invoking_agent,
                toolkit_name=paused.toolkit_owners.get((invoking_agent, tool.tool_name)),
                arguments_digest=approval_arguments_digest(tool.tool_args),
                expires_at_ns=0,
                decision=ApprovalDecision.APPROVED if requirement.confirmation else ApprovalDecision.DENIED,
                reason=requirement.confirmation_note,
            ),
        )
    return tuple(calls)


async def advance_cli_delegation(
    binding: PreparedAgentToolBinding,
    requirement: RunRequirement,
    *,
    parent_bash_call_id: str,
    delegation_depth: int,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    approval_calls: Sequence[ApprovalCall] | None,
    on_event: Callable[[object], None],
) -> PausedAttempt | ToolExecution:
    """Advance one child, persisting state on the real parent run without resuming it."""
    from mindroom.ai import run_delegated_child_response  # noqa: PLC0415 - envelope creation cycle

    catalog = binding.catalog
    context = catalog.runtime_context
    response = catalog.record
    tool = requirement.tool_execution
    if response is None or not requirement.needs_external_execution or tool.tool_name not in DELEGATION_FUNCTIONS:
        msg = "CLI delegation requires its exact external call"
        raise ValueError(msg)
    if (
        tool.tool_name != binding.key.function
        or binding.key.toolkit != "delegate"
        or not binding.function.external_execution
    ):
        msg = "CLI external call is not owned by the delegate toolkit"
        raise ValueError(msg)
    config = context.current_config
    state, previous, _parent_decisions = prepare_delegation_state(
        response,
        agent_name=context.agent_name,
        config=config,
        approval_calls=approval_calls,
    )
    if approval_calls is None:
        # A CLI requirement has no member agent, so its gate key is the bare call ID, which the CLI caller
        # chooses; a fresh call therefore never inherits an approval saved under the same ID.
        state.gates.pop(tool.tool_call_id, None)
    persist = partial(persist_delegation_state, catalog.store, catalog.session, response)

    # Same mutation/authorization owner as ordinary prepared dispatch. The parent never resumes, and no hidden
    # requirement enters its provider history.
    with tool_runtime_context(context):
        result = await advance_delegation_call(
            requirement,
            response=response,
            state=state,
            previous_state=previous,
            persist=persist,
            agent_name=context.agent_name,
            run_child=run_delegated_child_response,
            config=config,
            runtime_paths=context.runtime_paths,
            execution_identity=context.execution_identity,
            delegation_depth=delegation_depth,
            refresh_scheduler=refresh_scheduler,
            approval_calls=approval_calls,
            on_event=on_event,
        )
        await persist(state)
    if result is not None:
        return replace(tool, result=result, external_execution_required=False)
    view = deepcopy(response)
    view.status = RunStatus.paused
    paused = paused_attempt_from_response(
        view,
        toolkit_owners={(context.agent_name, binding.key.function): binding.approval_toolkit},
    )
    if paused is None:
        msg = "Delegated child paused without supported exact approval requirements"
        raise RuntimeError(msg)
    return replace(
        paused,
        runtime_model_name=catalog.run.model_name,
        cli_call=CliApprovalCall(
            toolkit=binding.approval_toolkit,
            function=binding.key.function,
            arguments=deepcopy(tool.tool_args or {}),
            call_id=tool.tool_call_id,
            parent_bash_call_id=parent_bash_call_id,
            requirements=paused.requirements,
            external_requirement=requirement,
            delegation_depth=delegation_depth,
        ).to_dict(),
    )
