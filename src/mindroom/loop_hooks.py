"""The hooks an agent or team-leader run or resume installs on the agent loop, in the order the turn applies them.

An agent turn, an approval resume, a delegated child's resume, a team member's run or resume, and a team leader's
turn or resume install the hooks ``_loop_hooks`` composes once: the wait budget of managed tool-job calls, the
participation gate, mid-turn compaction, the approval receipt, the output-limit answer, portable tool search, the
tool-call middleware, the queued-message and tool-job notices, and the skill-review capture. Which of those a run
has is an input: ``agent_loop_hooks`` and ``leader_loop_hooks`` name what an agent's and a leader's run differ by,
and ``HookTurn`` holds what every run takes from its turn. Every run has the approval receipt hook; the receipt
reaches only the runs the bound approval continuation names (``mindroom.approval.receipt``).

The loop lets a hook's exception fail the run, so a mistake in a hook the run depends on surfaces. A hook whose
work a reply can do without catches and logs that work's failures itself: mid-turn compaction sends the request
uncompacted when its summary or storage fails, and the queued-message and job notices carry on without a judgment,
a Matrix reaction, or a job acknowledgement that failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom import ai_runtime
from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.output_limit import output_limit_answers
from mindroom.approval.receipt import approval_receipt_hook
from mindroom.history.mid_turn_compaction import mid_turn_compaction_hook
from mindroom.llm.provider import DeferredTools
from mindroom.llm.tool_search import resolve_call_tool_calls
from mindroom.participation_hook import participation_hook
from mindroom.skill_learning.capture import capture_final_request
from mindroom.tool_jobs.execution import agent_tool_middleware, job_definitions_hook
from mindroom.tool_jobs.notices import job_notice_hook

if TYPE_CHECKING:
    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.model_loop import BeforeRequestHook
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.history.types import CompactionLifecycle, ResolvedHistorySettings, ResolvedReplayPlan
    from mindroom.llm.provider import ModelProvider
    from mindroom.participation import ParticipationGate
    from mindroom.run_storage import RunStorage
    from mindroom.skill_learning.capture import SkillReviewCapture
    from mindroom.tool_jobs.execution import JobActor
    from mindroom.tool_system.invocation import ToolMiddleware


@dataclass(frozen=True, slots=True, kw_only=True)
class HookTurn:
    """What one run or resume takes from its turn for its hooks, the same for an agent's and a leader's.

    ``storage`` is where the run stores, and ``replay_plan`` is the plan the run's requests replay the scope's stored
    history under, so a fold lands in the scope when they replay some; a run without a scope or a plan compacts within
    itself. ``model_name`` is the configured name of the model the run resolved, and ``prompt_id`` the turn's prompt;
    ``lifecycle`` and ``turn_recorder`` are the turn's compaction lifecycle and recorder, which a resume and a team
    member lack.
    """

    config: Config
    runtime_paths: RuntimePaths
    model_name: str
    storage: RunStorage | None
    replay_plan: ResolvedReplayPlan | None
    prompt_id: str | None
    lifecycle: CompactionLifecycle | None = None
    turn_recorder: TurnRecorder | None = None


def agent_loop_hooks(
    build: AgentBuild,
    turn: HookTurn,
    *,
    agent_name: str,
    participation: ParticipationGate | None = None,
    skill_review_capture: SkillReviewCapture | None = None,
    run_id: str | None = None,
    queued_notices: bool = True,
) -> LoopHooks:
    """Return the hooks of one agent run or resume, from the build's model, history settings, tools, and jobs.

    The run compacts as ``agent_name``, in the turn's scope when one is given and under its own name otherwise, and
    a top-level agent hears of its ended jobs. ``queued_notices`` is off for a team member, whose leader owns the
    turn's notices; ``skill_review_capture`` records the final request of run ``run_id``.
    """
    jobs = build.tool_jobs
    scope = turn.storage.scope if turn.storage is not None else None
    return _loop_hooks(
        turn,
        model=build.spec.model,
        entity_name=agent_name,
        scope_id=scope.scope_id if scope is not None else agent_name,
        is_team=False,
        history_settings=build.history_settings,
        deferred_tools=build.request_template.deferred_tools,
        tool_middleware=agent_tool_middleware(build),
        participation=participation,
        job_definitions=jobs,
        # A delegated child cannot collect jobs; its caller hears when they end. A team member hears of its
        # own jobs too: the notice reaches only the member whose account posts an ad hoc team's replies.
        job_notices_for=jobs.name if jobs is not None and jobs.depth == 0 else None,
        queued_notices=queued_notices,
        skill_review_capture=skill_review_capture,
        run_id=run_id,
    )


def leader_loop_hooks(
    turn: HookTurn,
    *,
    entity_name: str | None,
    scope_id: str,
    model: ModelProvider,
    history_settings: ResolvedHistorySettings,
    tool_middleware: tuple[ToolMiddleware, ...],
    jobs: JobActor | None,
) -> LoopHooks:
    """Return the hooks of one team leader's turn or resume.

    The leader compacts as the team: as the configured team ``entity_name`` names, or as no entity when it is None
    for an ad hoc team, under the team's ``history_settings``. It has no participation gate, deferred tools, job
    definitions, or skill-review capture; it owns the turn's queued-message notices and hears of its team's ended
    ``jobs``.
    """
    return _loop_hooks(
        turn,
        model=model,
        entity_name=entity_name,
        scope_id=scope_id,
        is_team=True,
        history_settings=history_settings,
        deferred_tools=DeferredTools(),
        tool_middleware=tool_middleware,
        participation=None,
        job_definitions=None,
        job_notices_for=jobs.name if jobs is not None else None,
        queued_notices=True,
        skill_review_capture=None,
        run_id=None,
    )


def _loop_hooks(
    turn: HookTurn,
    *,
    model: ModelProvider,
    entity_name: str | None,
    scope_id: str,
    is_team: bool,
    history_settings: ResolvedHistorySettings,
    deferred_tools: DeferredTools,
    tool_middleware: tuple[ToolMiddleware, ...],
    participation: ParticipationGate | None,
    job_definitions: JobActor | None,
    job_notices_for: str | None,
    queued_notices: bool,
    skill_review_capture: SkillReviewCapture | None,
    run_id: str | None,
) -> LoopHooks:
    """Return the hooks of one run or resume, in the order the loop applies them before each request.

    The request cap is the loop's own and refuses a request before any hook runs; the wait budget of
    ``job_definitions``' managed calls joins their definitions first, so the participation decision and the
    compaction's sizing see the definitions the request sends; the participation gate decides next, so a reply that
    stays silent never compacts or requests; mid-turn compaction rewrites the live messages, and folds into the
    scope only when the run replays it; the bound approval receipt then projects into the first request of a run
    that reads it. Queued-message notices follow every tool batch and a resume's entry when ``queued_notices`` is
    on; the run hears of the ended jobs of ``job_notices_for`` after each batch; ``skill_review_capture`` records the
    final request of run ``run_id``.
    """
    capabilities = model.capabilities
    before_request: list[BeforeRequestHook] = []
    if job_definitions is not None:
        before_request.append(job_definitions_hook(job_definitions))
    if participation is not None:
        before_request.append(participation_hook(participation, model))
    before_request.append(
        mid_turn_compaction_hook(
            config=turn.config,
            runtime_paths=turn.runtime_paths,
            entity_name=entity_name,
            scope_id=scope_id,
            is_team=is_team,
            model_name=turn.model_name,
            model=model,
            history_settings=history_settings,
            storage=turn.storage,
            replay_plan=turn.replay_plan,
            deferred_tool_names=deferred_tools.names,
            prompt_id=turn.prompt_id,
            lifecycle=turn.lifecycle,
            turn_recorder=turn.turn_recorder,
        ),
    )
    before_request.append(approval_receipt_hook(capabilities))
    notice_text = turn.config.get_prompt("QUEUED_MESSAGE_NOTICE_TEXT")
    return LoopHooks(
        before_request=tuple(before_request),
        answer_tool_calls=output_limit_answers(capabilities),
        rewrite_tool_calls=resolve_call_tool_calls if deferred_tools.portable else None,
        tool_middleware=tool_middleware,
        after_tool_batch=(
            *((ai_runtime.queued_notice_after_tool_batch(notice_text),) if queued_notices else ()),
            *((job_notice_hook(job_notices_for),) if job_notices_for is not None else ()),
        ),
        on_resume_entry=(ai_runtime.queued_notice_on_resume_entry(notice_text),) if queued_notices else (),
        on_loop_end=(
            capture_final_request(skill_review_capture, model, run_id=run_id or "", model_name=turn.model_name)
            if skill_review_capture is not None
            else None
        ),
    )


__all__ = ["HookTurn", "agent_loop_hooks", "leader_loop_hooks"]
