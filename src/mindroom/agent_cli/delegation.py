"""Hidden external calls reuse the native child operation and approval snapshot."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from agno.run.base import RunStatus

from mindroom.agent_cli.approval import CliApprovalCall
from mindroom.delegation.execution import advance_delegation_call, persist_delegation_state, prepare_delegation_state
from mindroom.event_journal import ApprovalCall
from mindroom.response_turn import PausedAttempt, paused_attempt_from_response
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agno.models.response import ToolExecution
    from agno.run.requirement import RunRequirement

    from mindroom.delegation.state import ChildResponseRunner
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolBinding


def approval_calls_for_cli_pause(paused: PausedAttempt, agent_name: str) -> tuple[ApprovalCall, ...]:
    """Retain native ownership while the response coordinator owns claim/expiry."""
    invoking_agent = paused.approval_agent_name or agent_name
    return tuple(
        ApprovalCall(
            tool_call_id=str(tool.tool_call_id),
            tool_name=str(tool.tool_name),
            invoking_agent=invoking_agent,
            toolkit_name=paused.toolkit_owners.get((invoking_agent, str(tool.tool_name))),
            expires_at_ns=0,
        )
        for tool in paused.tools
    )


async def advance_cli_delegation(
    binding: PreparedAgentToolBinding,
    requirement: RunRequirement,
    *,
    parent_bash_call_id: str,
    delegation_depth: int,
    run_child: ChildResponseRunner | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    decisions: dict[str, bool] | None = None,
    denial_reasons: dict[str, str | None] | None = None,
    approval_calls: Sequence[ApprovalCall] = (),
    on_event: Callable[[object], None] | None = None,
) -> PausedAttempt | ToolExecution:
    """Advance one child, persisting state on the real parent without resuming it."""
    from mindroom.ai import run_delegated_child_response  # noqa: PLC0415 - envelope creation cycle

    catalog = binding.catalog
    context = catalog.runtime_context
    response = catalog.run_response
    tool = requirement.tool_execution
    if (
        tool is None
        or not requirement.needs_external_execution
        or tool.tool_name not in {"run_subagent", "continue_subagent"}
    ):
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
    state, previous = prepare_delegation_state(
        response,
        agent_name=context.agent_name,
        config=config,
        decisions=decisions,
        denial_reasons=denial_reasons,
    )
    persist = partial(persist_delegation_state, catalog.agent, response)

    # Same mutation/authorization owner as ordinary prepared dispatch. No parent
    # acontinue_run and no hidden requirements inserted into its native transcript.
    with catalog.execution_context():
        waiting = await advance_delegation_call(
            requirement,
            response=response,
            state=state,
            previous_state=previous,
            persist=persist,
            agent_name=context.agent_name,
            run_child=run_child or run_delegated_child_response,
            config=config,
            runtime_paths=context.runtime_paths,
            execution_identity=build_execution_identity_from_runtime_context(context),
            delegation_depth=delegation_depth,
            refresh_scheduler=refresh_scheduler,
            decisions=decisions,
            denial_reasons=denial_reasons,
            approval_calls=approval_calls,
            on_event=on_event,
        )
        await persist(state)
    if not waiting:
        return tool
    view = deepcopy(response)
    view.status = RunStatus.paused
    paused = paused_attempt_from_response(
        view,
        fallback_session_id=catalog.session.session_id,
        fallback_run_id=catalog.run_context.run_id,
        toolkit_owners={(context.agent_name, binding.key.function): binding.key.toolkit},
    )
    if paused is None:
        msg = "Delegated child paused without supported exact approval requirements"
        raise RuntimeError(msg)
    return replace(
        paused,
        runtime_model_name=context.active_model_name,
        cli_call=CliApprovalCall(
            toolkit=binding.key.toolkit,
            function=binding.key.function,
            arguments=deepcopy(tool.tool_args or {}),
            call_id=str(tool.tool_call_id),
            parent_bash_call_id=parent_bash_call_id,
            requirements=paused.requirements,
            external_requirement=requirement,
            delegation_depth=delegation_depth,
        ).to_dict(),
    )
