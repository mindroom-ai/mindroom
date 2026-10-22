"""Resume a persisted paused agent run on the owned loop, as an approval or a delegation continues it.

A resume rebuilds the agent, replays the stored history its scope's configured policy selects, restores the paused
request's native history settings, installs the hooks an agent turn installs, and continues the run with the
decisions on exactly the calls it waits for. The loop refuses anything but exact coverage of those calls before it
runs any of them.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.agent_loop.run import resume_agent
from mindroom.agent_loop.spec import RunContext
from mindroom.history.native import restore_native_history
from mindroom.history.replay import configured_replay_plan
from mindroom.history.request_messages import build_resume_request, replayed_history
from mindroom.llm.events import RunFinished
from mindroom.llm.runs import RunRecord
from mindroom.loop_hooks import agent_loop_hooks
from mindroom.response_turn import DENIED_WITHOUT_REASON
from mindroom.session_storage.agent_storage import runs_without
from mindroom.session_storage.store import persist_to
from mindroom.tool_system.context_bound_streams import closing_async_stream

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
    denied: Mapping[str, str | None] = field(default_factory=dict)
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
    if record is None:
        msg = "Resumed run ended without its terminal record"
        raise RuntimeError(msg)
    return record


@dataclass(frozen=True, slots=True, kw_only=True)
class ResumeRunner:
    """Resumes paused runs of one rebuilt agent against its store, with the hooks an agent turn installs.

    A resume replays the history the configured policy selects from ``session`` now, not a turn's narrowed plan; the
    run's native history settings come from the paused request's last response. Every resume streams its requests,
    as Agno's continuation did.
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

    def events(self, paused: RunRecord, decisions: ResumeDecisions) -> AsyncIterator[RunEvent]:
        """Resume ``paused`` with ``decisions``; the stream ends with ``RunFinished`` as ``resume_agent``'s does."""
        build = self.build
        plan = configured_replay_plan(history_settings=build.history_settings, estimated_tokens=0)
        native = restore_native_history(build.spec.model, persisted_run=paused, session=self.session)
        native_compaction = native.native_compaction if native is not None else None
        store_record = persist_to(self.store, self.session)

        async def persist(record: RunRecord) -> None:
            await store_record(record)
            self.session.runs = [*runs_without(self.session.runs, [record.run_id]), record]

        return resume_from_session(
            build.spec,
            paused,
            decisions,
            session=self.session,
            scope=self.scope,
            settings=build.history_settings,
            plan=plan,
            user_id=self.user_id,
            hooks=agent_loop_hooks(
                build,
                config=self.config,
                runtime_paths=self.runtime_paths,
                agent_name=self.agent_name,
                model_name=self.model_name,
                store=self.store,
                session=self.session,
                scope=self.scope,
                scoped=plan.add_history_to_context,
                native_compaction=native_compaction,
                prompt_id=None,
            ),
            persist=persist,
            request_template=replace(
                build.request_template,
                native_compaction=native_compaction,
                portable_replay=native.portable_replay if native is not None else False,
                stream=True,
            ),
        )

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
]
