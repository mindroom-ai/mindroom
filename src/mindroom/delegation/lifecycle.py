"""Own child execution outcomes and publish their runtime and audit projections."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from mindroom.ai_run_metadata import prior_usage_of
from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.constants import runtime_matrix_homeserver
from mindroom.delegation.activity import ActivityFeed
from mindroom.delegation.audit import (
    ChildAudit,
    child_response_usage,
    finish_child_record,
    flush_child_audit,
    open_child_audit,
    record_child_event,
    record_child_response,
    start_child_record,
)
from mindroom.delegation.personas import caller_toolkit_names, self_only_refusal
from mindroom.delegation.records import DelegationRecordLimitError
from mindroom.delegation.sessions import reserve_subagent_turn, update_subagent_turn, update_subagent_turn_sync
from mindroom.delegation.state import TERMINAL_CHILD_STATUSES, DelegationChild, child_execution_identity
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.delegation.workers import child_placement
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.failure_text import failure_text
from mindroom.live_frames import subagents as subagent_frames
from mindroom.llm.events import RunFinished, TextDelta, ToolCompleted, ToolStarted
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text
from mindroom.requester_identity import is_access_checked_requester_id
from mindroom.tool_system.progress import cap_report, report_tool_progress
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import get_detached_requester_context, get_tool_runtime_context
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from mindroom.turn_model import ModelScope, TurnBudget, resolve_turn_model
from mindroom.workers.placements import dedicated_worker_placement

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence
    from typing import TypeGuard

    import nio

    from mindroom.agent_modes import AgentMode
    from mindroom.budgets.monitor import BudgetMonitor
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.activity import SubagentSink
    from mindroom.delegation.state import DelegationTerminalStatus, SubagentPersona
    from mindroom.event_journal import ApprovalCall
    from mindroom.llm.events import RunEvent
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.workers.models import ScriptResourceProfileName

logger = get_logger(__name__)

MAX_DELEGATION_DEPTH = 3

type _DelegationGrant = Literal["delegate", "dynamic_workflow"]


@dataclass
class ChildRunObservation:
    """Exact terminal evidence collected while one child response envelope runs, and the child's live audit.

    ``response`` is the terminal record of the child's latest run: the record its stream ended with when it ran in
    this context, completed, paused, or failed, else the outcome the envelope returned without running it. A child
    that is cancelled leaves no record here: the loop stores its terminal row, which the interruption reads, and the
    cancellation propagates to the driver.
    """

    child: DelegationChild
    audit: ChildAudit
    parent: ChildRunObservation | None = None
    response: RunRecord | None = None
    # The tail of what the child said and which tools it called, reported as its caller's progress.
    activity: str = ""
    # The ID the child's turn carries in its hooks and tool calls, also across a continuation that starts a new run.
    turn_id: str = field(default_factory=lambda: uuid4().hex)
    # What hands the child's run to its sink, None when nothing publishes it.
    feed: ActivityFeed | None = None
    # Whether the child's published tool rows carry previews: whether the turn in the room that started its chain shows
    # its tool calls.
    previews: bool = False

    def owns(self, record: AnyRunRecord) -> TypeGuard[RunRecord]:
        """Return whether a terminal record is of the child's current run and session, the run this context streams."""
        child = self.child
        return isinstance(record, RunRecord) and record.run_id == child.run_id and record.session_id == child.session_id

    def current_record(self) -> RunRecord | None:
        """Return the terminal record of the child's current run, None while that run has handed over none."""
        response = self.response
        return response if response is not None and response.run_id == self.child.run_id else None


_CHILD_RUN: ContextVar[ChildRunObservation | None] = ContextVar("delegation_child_run", default=None)


def running_child_id() -> str | None:
    """Return the delegation id of the child turn running in this context, or None outside one."""
    observation = _CHILD_RUN.get()
    return None if observation is None else observation.child.delegation_id


def active_delegation_edges(owner: ToolExecutionIdentity) -> tuple[tuple[str, str, _DelegationGrant], ...]:
    """Return the exact active native ancestry for a retained leaf execution owner, with each edge's grant."""
    observation = _CHILD_RUN.get()
    if observation is None or child_execution_identity(observation.child) != owner:
        return ()
    edges = []
    while observation is not None:
        child = observation.child
        edges.append((child.caller_agent_name, child.child_agent_name, delegation_grant(child)))
        observation = observation.parent
    return tuple(reversed(edges))


def child_tool_runtime_context(context: ToolRuntimeContext, child: DelegationChild) -> ToolRuntimeContext:
    """Rebind a caller's tool context to the child turn running here, carrying its persona as the cap on copies it authors.

    The child's turn carries the ID of its child run and names the caller's turn as its parent. It hides its tool calls
    whatever its caller's reply shows, so they never read as shown.
    """
    observation = _CHILD_RUN.get()
    assert observation is not None, "A child's tool context is built inside the child's run"
    return replace(
        context,
        agent_name=child.child_agent_name,
        active_model_name=child.model_name,
        target=replace(context.target, session_id=child.session_id),
        persona=child.persona,
        turn_id=observation.turn_id,
        parent_turn_id=context.turn_id,
        tool_calls_shown=False,
    )


async def reserve_child_turn(
    child: DelegationChild,
    *,
    owner: ToolExecutionIdentity,
    runtime_paths: RuntimePaths,
) -> None:
    """Claim a turn and adopt an exact retained attempt if its parent snapshot is stale."""
    retained = await reserve_subagent_turn(child, owner=owner, runtime_paths=runtime_paths)
    if retained is not None:
        child.run_id = retained.run_id
        child.model_name = retained.model_name


def note_child_run_id(
    child: DelegationChild,
    run_id: str,
    runtime_paths: RuntimePaths,
    *,
    model_name: str,
) -> None:
    """Publish the exact attempt and the model it runs on before it can execute tools."""
    child.run_id = run_id
    if model_name != child.model_name:
        # A model switch during the run is what the child's follow-ups ask for next.
        child.requested_model_name = model_name
        child.model_name = model_name
    update_subagent_turn_sync(child, runtime_paths)


async def start_child_turn(
    child: DelegationChild,
    *,
    parent_run_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
    caller_execution_identity: ToolExecutionIdentity | None,
    parent_delegation_id: str | None = None,
) -> None:
    """Create and bind the audit record before startup cancellation can propagate."""

    async def create_record() -> None:
        locator = await start_child_record(
            child,
            parent_run_id=parent_run_id,
            config=config,
            runtime_paths=runtime_paths,
            caller_execution_identity=caller_execution_identity,
            parent_delegation_id=parent_delegation_id,
        )
        child.record_locator = locator.to_dict()

    await run_coroutine_until_complete(create_record())


async def settle_child_response(
    child: DelegationChild,
    response: RunRecord,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    approval_calls: Sequence[ApprovalCall] = (),
) -> None:
    """Derive operational state from one exact run, then publish its audit view.

    A finished run's result is what it hands over (``handover_content``), so a failed run's is why it failed, a run
    recovery cancelled hands over why, and a resumed run's is what it wrote before its pause and since, else the
    completion note. A finished turn's usage is the run's with that of the turn's earlier runs, which its record
    carries (``prior_usage_of``), however many pauses and restarts lay between them.
    """
    if response.run_id != child.run_id or response.session_id != child.session_id:
        msg = "Delegation child response does not match its retained run"
        raise ValueError(msg)
    content = response.handover_content
    if response.status == RunStatus.completed:
        child.status = "completed"
        child.result = content or "Agent completed the task but returned no content."
    elif response.status == RunStatus.cancelled:
        child.status = "cancelled"
        child.result = content or "Delegation cancelled."
    elif response.status == RunStatus.error:
        child.status = "failed"
        child.result = content or "Delegation failed."
    else:
        child.status = "paused" if response.status == RunStatus.paused else "running"
        child.result = None
    await update_subagent_turn(child, runtime_paths)
    try:
        await record_child_response(
            child,
            response,
            config=config,
            runtime_paths=runtime_paths,
            approval_calls=approval_calls,
        )
    except DelegationRecordLimitError:
        # A full record keeps room for its terminal event, so a terminal child is settled without the remaining
        # ordinary events.
        if child.status not in TERMINAL_CHILD_STATUSES:
            raise
    if child.status in TERMINAL_CHILD_STATUSES:
        await finish_child_record(
            child,
            config=config,
            runtime_paths=runtime_paths,
            usage=child_response_usage(response, prior_usage_of(response)),
        )


async def finish_child_turn(
    child: DelegationChild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    status: DelegationTerminalStatus | None = None,
    reason: str | None = None,
) -> str:
    """Settle an interruption without overwriting an already retained terminal outcome.

    A child interrupted outside its run, such as one whose job was cancelled while it waited for an approval, is
    published with this outcome; one whose run already published its end has no feed left.
    """
    if status is not None and child.status not in TERMINAL_CHILD_STATUSES:
        child.status = status
        child.result = reason
    if child.status not in TERMINAL_CHILD_STATUSES:
        msg = f"Cannot finish active delegation: {child.delegation_id}"
        raise ValueError(msg)
    _publish(ActivityFeed.find(child.delegation_id), lambda feed: feed.status(child.status, result=child.result))
    await update_subagent_turn(child, runtime_paths)
    if not child.record_locator:
        return ""
    return await finish_child_record(child, config=config, runtime_paths=runtime_paths)


def _publish(feed: ActivityFeed | None, update: Callable[[ActivityFeed], object]) -> None:
    """Apply one update to a child's feed; a failure is logged and dropped, so it never reaches the child."""
    if feed is None:
        return
    try:
        update(feed)
    except Exception:
        logger.exception("Sub-agent activity update failed", delegation_id=feed.delegation_id)


def _subagent_sink(context: ToolRuntimeContext, observation: ChildRunObservation) -> SubagentSink | None:
    """Return the sink that streams the child's snapshots as frames to its caller's room.

    None outside a Matrix room or a managed runtime, in a silent schedule's turn, which shows nothing in the room while
    it runs, or when the operator has live frames off as the feed opens; switching them off later does not stop a feed
    already open. The account answering the caller's turn sends the frames, through its bot's client at each send: a
    bot that logs in again has a new client, and a stopped bot none.
    """
    orchestrator = context.orchestrator
    if (
        orchestrator is None
        or not context.room_id
        or context.source_kind == SILENT_SCHEDULE_SOURCE_KIND
        or not context.current_config.defaults.streaming.live_frames
    ):
        return None
    account, room_id, previews = context.recipient, context.room_id, observation.previews
    homeserver_url = runtime_matrix_homeserver(context.runtime_paths)

    def client() -> nio.AsyncClient | None:
        bot = orchestrator.agent_bots.get(account)
        return None if bot is None else bot.client

    return lambda snapshot: subagent_frames.publish(
        account,
        room_id,
        snapshot,
        previews=previews,
        client=client,
        homeserver_url=homeserver_url,
    )


def _open_feed(observation: ChildRunObservation) -> ActivityFeed | None:
    """Open the child's feed when its caller's turn has a sink for it; None outside a turn or without one.

    An attempt that opens no feed also ends the one a paused attempt of the child left, whose sink would otherwise take
    the child's later updates, as when live frames were switched off before it resumed.
    """
    context = get_tool_runtime_context()
    child, parent = observation.child, observation.parent
    # The outermost child of the chain was delegated by the agent in the room whose turn started it.
    root = parent
    while root is not None and root.parent is not None:
        root = root.parent
    try:
        sink = None if context is None else _subagent_sink(context, observation)
        if sink is not None:
            return ActivityFeed.open(
                child,
                parent_delegation_id=None if parent is None else parent.child.delegation_id,
                root_caller_agent_name=(child if root is None else root.child).caller_agent_name,
                sink=sink,
            )
    except Exception:
        logger.exception("Sub-agent activity feed failed to open", delegation_id=child.delegation_id)
    ActivityFeed.forget(child.delegation_id)
    return None


def _publish_exit(feed: ActivityFeed | None, child: DelegationChild, *, cancelled: bool, reason: str) -> None:
    """Publish how an attempt ended: its settled pause or outcome, else cancelled or failed with ``reason``."""
    if child.status == "paused" or child.status in TERMINAL_CHILD_STATUSES:
        _publish(feed, lambda feed: feed.status(child.status, result=child.result))
    else:
        _publish(feed, lambda feed: feed.status("cancelled" if cancelled else "failed", result=reason))


@asynccontextmanager
async def child_run_context(
    child: DelegationChild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> AsyncIterator[ChildRunObservation]:
    """Observe and audit one child attempt, then settle it even when its last buffered output fails to record.

    The attempt is published to its sub-agent sink as it starts and with how it ended, after its settlement.
    """
    audit = await open_child_audit(child, config=config, runtime_paths=runtime_paths)
    parent = _CHILD_RUN.get()
    # The caller's context is current until the child's run rebinds it. A nested child's caller is a child, whose context
    # always hides its calls, so the nested child takes its parent's previews instead.
    context = get_tool_runtime_context()
    previews = parent.previews if parent is not None else context is not None and context.tool_calls_shown
    observation = ChildRunObservation(child, audit, parent=parent, previews=previews)
    observation.feed = _open_feed(observation)
    token = _CHILD_RUN.set(observation)
    # What an attempt that settles no outcome is published with; the text its caller is handed for the same exit.
    cancelled, reason = False, "Delegated run ended without a retained terminal outcome."
    try:
        # The operator's current setting decides, not the snapshot the parent's response started with.
        placement = child_placement(child, live_delegation_config(config), runtime_paths)
        with dedicated_worker_placement(child.session_id, placement):
            yield observation
    except BaseException as error:
        cancelled = isinstance(error, asyncio.CancelledError)
        reason = "Delegation cancelled." if cancelled else redact_sensitive_text(failure_text(error))
        raise
    finally:
        try:
            await flush_child_audit(audit)
        finally:
            try:
                response = observation.current_record()
                if response is not None:
                    await settle_child_response(child, response, config=config, runtime_paths=runtime_paths)
            finally:
                _CHILD_RUN.reset(token)
                _publish_exit(observation.feed, child, cancelled=cancelled, reason=reason)


def report_child_approval_wait(child: DelegationChild, *, awaiting: bool) -> None:
    """Show a paused child as awaiting the approvals its job asks for, or running again once they are decided."""
    _publish(
        ActivityFeed.find(child.delegation_id),
        lambda feed: feed.status("awaiting_approval" if awaiting else "running"),
    )


def _report_child_activity(observation: ChildRunObservation, event: TextDelta | ToolStarted) -> None:
    text = event.text if isinstance(event, TextDelta) else f"\nCalling {event.call.tool_name}\n"
    if text:
        observation.activity = cap_report(observation.activity + text)
        report_tool_progress(observation.activity)


async def observe_child_event(event: RunEvent) -> None:
    """Retain execution evidence and report the child's activity, to its caller and its feed, before auditing the event.

    The loop's events carry no run identity; they are the events of the child turn running in this context, whose
    terminal record, which ends the stream of every run that is not cancelled, names the run it ended. A terminal
    record the child does not own is neither its evidence nor in its audit.
    """
    observation = _CHILD_RUN.get()
    if observation is None:
        return
    if isinstance(event, TextDelta):
        _report_child_activity(observation, event)
        _publish(observation.feed, lambda feed: feed.text(event.text))
    elif isinstance(event, ToolStarted):
        _report_child_activity(observation, event)
        _publish(observation.feed, lambda feed: feed.tool_started(event))
    elif isinstance(event, RunFinished):
        if not observation.owns(record := event.record):
            return
        observation.response = record
    elif isinstance(event, ToolCompleted):
        _publish(observation.feed, lambda feed: feed.tool_completed(event))
    else:
        return
    await record_child_event(observation.audit, event)


def delegation_grant(child: DelegationChild) -> _DelegationGrant:
    """Name the caller permission that authorizes this child: its delegate allowlist or its workflow tool."""
    return "dynamic_workflow" if child.persona is not None and child.persona.source_kind == "workflow" else "delegate"


def caller_grants_child(caller_name: str, agent_name: str, config: Config, grant: _DelegationGrant) -> bool:
    """Return whether the caller's current config still grants this child, by delegation or its workflow tool."""
    caller = config.agents.get(caller_name)
    if caller is None:
        return False
    if grant == "delegate":
        return agent_name in caller.delegate_to
    # The workflow tool is built in, so the built-in tools decide whether the caller is offered it.
    return agent_name == caller_name and "dynamic_workflow" in caller_toolkit_names(
        caller_name,
        config,
        builtin_tool_registry(),
        delegation_depth=0,
    )


def _target_refusal(
    caller_name: str,
    agent_name: str,
    task: str,
    *,
    config: Config,
    grant: _DelegationGrant,
) -> str | None:
    """Return why this caller cannot start this target at all, before any requester check."""
    if not task.strip():
        return "Cannot delegate an empty task. Please provide a task description."
    if grant == "dynamic_workflow":
        return None if agent_name == caller_name else self_only_refusal(agent_name)
    caller = config.agents.get(caller_name)
    allowed_targets = caller.delegate_to if caller is not None else []
    if agent_name in allowed_targets:
        return None
    return f"Cannot delegate to '{agent_name}'. Allowed subagents: {', '.join(allowed_targets)}."


def live_delegation_config(config: Config) -> Config:
    """Return the live config that authorization and execution use, not a toolkit's or response's snapshot."""
    context = get_tool_runtime_context()
    if context is not None:
        return context.current_config
    detached = get_detached_requester_context()
    live = detached.config_provider() if detached is not None else None
    return live if live is not None else config


def authorize_delegation(  # noqa: PLR0911
    caller_name: str,
    agent_name: str,
    task: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    depth: int,
    model: str | None = None,
    grant: _DelegationGrant = "delegate",
) -> Config | str:
    """Recheck the caller's current grant and requester authority, returning the live config."""
    refusal = _target_refusal(
        caller_name,
        agent_name,
        task,
        config=config,
        grant=grant,
    )
    if refusal is not None:
        return refusal

    runtime_context = get_tool_runtime_context()
    detached_context = get_detached_requester_context()
    if runtime_context is not None:
        active_config = runtime_context.current_config
        requester_id = runtime_context.requester_id
        authorization_room_id = runtime_context.room_id
        membership_index = runtime_context.require_agent_reply_memberships()
    elif (
        detached_context is not None
        and execution_identity is not None
        and execution_identity.channel == "openai_compat"
        and execution_identity.requester_id == detached_context.requester_id
        and runtime_paths == detached_context.runtime_paths
    ):
        active_config = detached_context.config_provider()
        requester_id = detached_context.requester_id
        authorization_room_id = None
        membership_index = detached_context.agent_reply_memberships
    else:
        return f"Cannot delegate to '{agent_name}': requester authorization is unavailable."
    if active_config is None or agent_name not in active_config.agents:
        return f"Cannot delegate to '{agent_name}': that agent is not allowed to reply to you."
    if not caller_grants_child(caller_name, agent_name, active_config, grant):
        return f"Cannot delegate to '{agent_name}': it is no longer an allowed target."
    # A workflow participant is the caller's own copy inside the caller's tool call, so the requester
    # the caller already serves, possibly only through a team's access, keeps that access.
    if grant == "delegate" and not is_sender_allowed_for_responder(
        requester_id,
        agent_name,
        authorization_room_id,
        active_config,
        runtime_paths,
        membership_index,
    ):
        return f"Cannot delegate to '{agent_name}': that agent is not allowed to reply to you."
    if active_config.agents[agent_name].private is not None and not is_access_checked_requester_id(
        requester_id,
        active_config,
        runtime_paths,
    ):
        return f"Cannot delegate to '{agent_name}': a private agent runs only for a person or bot account."

    if depth >= MAX_DELEGATION_DEPTH:
        return "Cannot delegate: the maximum delegation depth was reached."
    if model is not None and (not isinstance(model, str) or model not in active_config.models):
        available_models = ", ".join(sorted(active_config.models))
        return f"Cannot delegate: Unknown model '{model}'. Available models: {available_models}."
    return active_config


def prepare_child_turn(
    caller_name: str,
    agent_name: str,
    task: str,
    *,
    owner: ToolExecutionIdentity,
    config: Config,
    runtime_paths: RuntimePaths,
    depth: int,
    budget_monitor: BudgetMonitor | None,
    model: str | None,
    agent_mode: AgentMode,
    previous: DelegationChild | None,
    persona: SubagentPersona | None,
    dedicated_worker: bool = False,
    worker_resource_profile: ScriptResourceProfileName | None = None,
    parent_tool_call_id: str = "",
    parent_requirement_id: str = "",
) -> DelegationChild:
    """Prepare the same scoped fresh/follow-up turn for direct and native callers.

    A follow-up keeps the mode, persona, and worker choice of the child it continues, and its requested model, checked
    again against the owner's budget.
    """
    delegation_id = uuid4().hex
    if previous is None:
        session_id = f"delegate:{caller_name}:{agent_name}:{delegation_id}"
        identity = replace(owner, agent_name=agent_name, session_id=session_id)
        subagent_id, previous_delegation_id = delegation_id, None
    else:
        session_id = previous.session_id
        identity = child_execution_identity(previous)
        subagent_id, previous_delegation_id = previous.subagent_id, previous.delegation_id
        model, agent_mode, persona = previous.requested_model_name, previous.agent_mode, previous.persona
        dedicated_worker, worker_resource_profile = previous.dedicated_worker, previous.worker_resource_profile
    # Each turn checks the budget against the requested model, so a follow-up recovers once spend is under the cap.
    turn_model = resolve_turn_model(
        config,
        runtime_paths,
        ModelScope(
            entity=agent_name,
            room_id=identity.room_id,
            thread_id=identity.resolved_thread_id,
            explicit=model,
        ),
        TurnBudget(monitor=budget_monitor, requester_id=owner.requester_id),
    )
    return DelegationChild(
        delegation_id=delegation_id,
        parent_tool_call_id=parent_tool_call_id,
        caller_agent_name=caller_name,
        child_agent_name=agent_name,
        task=task,
        session_id=session_id,
        run_id=uuid4().hex,
        model_name=turn_model.model_name,
        requested_model_name=turn_model.requested_model_name,
        depth=depth + 1,
        execution_identity=serialize_tool_execution_identity(identity),
        subagent_id=subagent_id,
        previous_delegation_id=previous_delegation_id,
        parent_requirement_id=parent_requirement_id,
        storage_bindings=freeze_delegation_storage(config, (caller_name, agent_name)),
        agent_mode=agent_mode,
        persona=persona,
        dedicated_worker=dedicated_worker,
        worker_resource_profile=worker_resource_profile,
    )
