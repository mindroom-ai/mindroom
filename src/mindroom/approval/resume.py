"""Resume a persisted paused agent run on the owned loop, as an approval or a delegation continues it.

A resume replays the stored history a replay plan selects and continues the paused request with the decisions on
exactly the calls it waits for; the loop refuses anything but exact coverage of those calls before it runs any of
them. ``ResumeRunner`` resumes a rebuilt agent, for an approval or a delegated child, and a live turn continuing
after its delegations calls ``resume_from_session`` with its own plan.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from functools import cached_property
from typing import TYPE_CHECKING

from mindroom.agent_loop.run import resume_agent
from mindroom.agent_loop.spec import RunContext
from mindroom.approval.receipt import add_approval_receipt_reader
from mindroom.history.native import restore_native_history
from mindroom.history.replay import configured_replay_plan
from mindroom.history.request_messages import build_resume_request, replayed_history
from mindroom.llm.events import RunFinished
from mindroom.llm.request_context import RequestAttribution
from mindroom.llm.runs import DENIED_WITHOUT_REASON, RunRecord
from mindroom.loop_hooks import HookTurn, agent_loop_hooks
from mindroom.response_turn import stream_with_llm_request_log_context
from mindroom.session_storage.store import persist_to
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.model_loop import LoopHooks
    from mindroom.agent_loop.run import Persist
    from mindroom.agent_loop.spec import AgentSpec
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalCall
    from mindroom.history.types import HistoryScope, ResolvedHistorySettings, ResolvedReplayPlan
    from mindroom.llm.events import RunEvent
    from mindroom.llm.provider import ModelRequest
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession

type ResumeEvents = Callable[[RunEvent], None]
type ContinueRun[R: AnyRunRecord] = Callable[[R, ResumeDecisions, ResumeEvents | None], Awaitable[R]]


@dataclass(frozen=True, slots=True)
class ResumeDecisions:
    """What one resume answers: the approved calls, the denied calls with their notes, and the external results.

    ``external_results`` holds, by call id, what an externally executed call (a delegation) returned.
    """

    approved: tuple[ApprovalCall, ...] = ()
    denied: Mapping[str, str] = field(default_factory=dict)
    external_results: Mapping[str, str] = field(default_factory=dict)


def approval_decisions(
    calls: Sequence[ApprovalCall],
    decisions: Mapping[str, bool],
    denial_reasons: Mapping[str, str | None],
) -> ResumeDecisions:
    """Return the decisions one approval generation took on ``calls``, by the call ids ``decisions`` names."""
    return ResumeDecisions(
        approved=tuple(call for call in calls if decisions.get(call.tool_call_id)),
        denied={
            call_id: denial_reasons.get(call_id) or DENIED_WITHOUT_REASON
            for call_id, approved in decisions.items()
            if not approved
        },
    )


def resume_from_session(
    spec: AgentSpec,
    paused: RunRecord,
    decisions: ResumeDecisions,
    *,
    session: ConversationSession,
    scope: HistoryScope | None,
    settings: ResolvedHistorySettings,
    plan: ResolvedReplayPlan | None,
    user_id: str | None,
    hooks: LoopHooks,
    persist: Persist[RunRecord],
    request_template: ModelRequest,
) -> AsyncIterator[RunEvent]:
    """Resume ``paused`` in ``session`` with ``decisions``: the history ``plan`` replays, then the paused request.

    The resume reuses the paused run's stored system message and shares the session's state with its tools; the
    stream ends with ``RunFinished`` as ``resume_agent``'s does.
    """
    history, summary = replayed_history(session, scope, settings, plan)
    return resume_agent(
        spec,
        paused,
        build_resume_request(paused=paused.messages, summary=summary, history=history),
        approved=decisions.approved,
        denied=decisions.denied,
        external_results=decisions.external_results,
        ctx=RunContext(
            run_id=paused.run_id,
            session_id=session.session_id,
            user_id=user_id,
            metadata=deepcopy(paused.metadata),
            session_state=session.session_state,
        ),
        hooks=hooks,
        persist=persist,
        request_template=request_template,
    )


def with_resumed_turn_log_context[EventT](
    events: AsyncIterator[EventT],
    *,
    entity_name: str | None = None,
) -> AsyncIterator[EventT]:
    """Return ``events`` with the request-log attribution of the turn it resumes bound around every pull.

    A resume runs under the tool runtime context of the turn it continues, which names the entity, session, room,
    thread, source event, requester, and correlation id a fresh turn of it binds; without one ``events`` is returned
    as is. ``entity_name`` replaces the context's agent when the turn logged another entity: an ad hoc team's
    context names the member whose account handles it, while its requests carry the team's label.
    """
    context = get_tool_runtime_context()
    if context is None:
        return events
    target = context.target
    attribution = asdict(
        RequestAttribution(
            agent_id=context.agent_name if entity_name is None else entity_name,
            session_id=target.session_id,
            room_id=target.room_id,
            thread_id=target.resolved_thread_id,
            reply_to_event_id=target.reply_to_event_id,
            requester_id=context.requester_id,
            correlation_id=context.correlation_id,
        ),
    )
    return stream_with_llm_request_log_context(events, request_context=attribution)


async def forward_resume[R: AnyRunRecord](
    events: AsyncIterator[RunEvent],
    on_event: ResumeEvents | None,
    *,
    record_type: type[R],
) -> R:
    """Drain a resume, handing every event but the last to ``on_event``, and return its terminal ``record_type`` record."""
    record: R | None = None
    async with closing_async_stream(events):
        async for event in events:
            if isinstance(event, RunFinished):
                assert isinstance(event.record, record_type)
                record = event.record
            elif on_event is not None:
                on_event(event)
    # A run ends with ``RunFinished`` or raises.
    assert record is not None
    return record


@dataclass(frozen=True, kw_only=True)
class ResumeRunner:
    """Resumes one paused run of a rebuilt agent against its store, with the hooks an agent turn installs.

    A resume replays the history the configured policy selects from ``session`` now, not a turn's narrowed plan; the
    run's native history settings come from the paused request's last response. Every resume streams its requests,
    as Agno's continuation did. A resume that answers the run's own paused calls with an approval's decisions makes
    the run a reader of that approval's receipt. The run keeps one set of hooks for its whole life, so per-run hook
    state, such as the output-limit count or a failed compaction's stop, carries across its delegation
    continuations.
    """

    build: AgentBuild
    config: Config
    runtime_paths: RuntimePaths
    agent_name: str
    model_name: str
    store: ConversationStore
    session: ConversationSession
    scope: HistoryScope
    user_id: str | None

    @cached_property
    def _replay_plan(self) -> ResolvedReplayPlan:
        return configured_replay_plan(history_settings=self.build.history_settings, estimated_tokens=0)

    @cached_property
    def _hooks(self) -> LoopHooks:
        return agent_loop_hooks(
            self.build,
            HookTurn(
                config=self.config,
                runtime_paths=self.runtime_paths,
                model_name=self.model_name,
                store=self.store,
                session=self.session,
                scope=self.scope,
                replay_plan=self._replay_plan,
                prompt_id=None,
            ),
            agent_name=self.agent_name,
        )

    def events(self, paused: RunRecord, decisions: ResumeDecisions) -> AsyncIterator[RunEvent]:
        """Resume ``paused`` with ``decisions``; the stream ends with ``RunFinished`` as ``resume_agent``'s does."""
        build = self.build
        native = restore_native_history(build.spec.model, persisted_run=paused, session=self.session)
        native_compaction = native.native_compaction if native is not None else None
        if decisions.approved or decisions.denied:
            add_approval_receipt_reader(paused.run_id)
        events = resume_from_session(
            build.spec,
            paused,
            decisions,
            session=self.session,
            scope=self.scope,
            settings=build.history_settings,
            plan=self._replay_plan,
            user_id=self.user_id,
            hooks=self._hooks,
            persist=persist_to(self.store, self.session),
            request_template=replace(
                build.request_template,
                native_compaction=native_compaction,
                portable_replay=native.portable_replay if native is not None else False,
                stream=True,
            ),
        )
        return with_resumed_turn_log_context(events)

    async def continue_run(
        self,
        paused: RunRecord,
        decisions: ResumeDecisions,
        on_event: ResumeEvents | None,
    ) -> RunRecord:
        """Resume ``paused`` with ``decisions``, forwarding its events, and return its terminal record."""
        return await forward_resume(self.events(paused, decisions), on_event, record_type=RunRecord)


__all__ = [
    "ContinueRun",
    "ResumeDecisions",
    "ResumeEvents",
    "ResumeRunner",
    "approval_decisions",
    "forward_resume",
    "resume_from_session",
    "with_resumed_turn_log_context",
]
