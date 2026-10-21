"""Resume a persisted paused agent run on the owned loop, as an approval or a delegation continues it.

A resume rebuilds the agent, replays its scope's stored history as the paused request did, restores the paused
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
from mindroom.history.request_messages import build_resume_request, scope_history_messages
from mindroom.llm.events import RunFinished
from mindroom.llm.runs import RunRecord
from mindroom.loop_hooks import agent_loop_hooks
from mindroom.session_storage.agent_storage import runs_without
from mindroom.session_storage.store import persist_to
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalCall
    from mindroom.history.types import HistoryScope, ResolvedReplayPlan, ScopeSummary
    from mindroom.llm.events import RunEvent
    from mindroom.llm.messages import Message
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession

# The note MindRoom's continuation has always given the model for a call denied without a reason.
DENIED_WITHOUT_REASON = "Not approved by requester"

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


def resume_history(
    session: ConversationSession | None,
    scope: HistoryScope | None,
    build: AgentBuild,
    plan: ResolvedReplayPlan | None,
) -> tuple[list[Message], ScopeSummary | None]:
    """Return the stored history a run of ``build`` replays under ``plan`` and the scope summary it follows.

    A run without a scope, or whose plan replays nothing, sends its own messages only.
    """
    if session is None or scope is None or plan is None or not plan.add_history_to_context:
        return [], None
    settings = build.history_settings
    history = scope_history_messages(
        session.runs,
        scope=scope,
        num_history_runs=plan.num_history_runs,
        num_history_messages=plan.num_history_messages,
        max_tool_calls_from_history=settings.max_tool_calls_from_history,
        system_message_role=settings.system_message_role,
    )
    return history, session.summary


async def forward_resume(events: AsyncIterator[RunEvent], on_event: ResumeEvents | None) -> RunRecord:
    """Drain a resume, handing every event but the last to ``on_event``, and return its terminal record."""
    record: RunRecord | None = None
    async with closing_async_stream(events):
        async for event in events:
            if isinstance(event, RunFinished):
                assert isinstance(event.record, RunRecord)
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

    The history a resume replays is the configured policy's selection from ``session``, as the paused request's
    was; the run's native history settings come from the paused request's last response.
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
    stream: bool = True

    def events(self, paused: RunRecord, decisions: ResumeDecisions) -> AsyncIterator[RunEvent]:
        """Resume ``paused`` with ``decisions``; the stream ends with ``RunFinished`` as ``resume_agent``'s does."""
        build = self.build
        plan = configured_replay_plan(history_settings=build.history_settings, estimated_tokens=0)
        history, summary = resume_history(self.session, self.scope, build, plan)
        native = restore_native_history(build.spec.model, persisted_run=paused, session=self.session)
        native_compaction = native.native_compaction if native is not None else None
        store_record = persist_to(self.store, self.session)

        async def persist(record: RunRecord) -> None:
            await store_record(record)
            self.session.runs = [*runs_without(self.session.runs, [record.run_id]), record]

        return resume_agent(
            build.spec,
            paused,
            build_resume_request(paused=paused.messages, summary=summary, history=history),
            approved=decisions.approved,
            denied=decisions.denied,
            external_results=decisions.external_results,
            ctx=RunContext(
                run_id=paused.run_id,
                session_id=self.session.session_id,
                user_id=self.user_id,
                metadata=deepcopy(paused.metadata),
                session_state=self.session.session_state,
            ),
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
                stream=self.stream,
            ),
        )

    async def continue_run(
        self,
        paused: RunRecord,
        decisions: ResumeDecisions,
        on_event: ResumeEvents | None,
    ) -> RunRecord:
        """Resume ``paused`` with ``decisions``, forwarding its events, and return its terminal record."""
        return await forward_resume(self.events(paused, decisions), on_event)


__all__ = [
    "DENIED_WITHOUT_REASON",
    "ContinueRun",
    "ResumeDecisions",
    "ResumeEvents",
    "ResumeRunner",
    "approval_decisions",
    "forward_resume",
    "resume_history",
]
