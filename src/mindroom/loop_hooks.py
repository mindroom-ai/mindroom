"""The hooks an agent run or resume installs on the owned loop, in the order the turn applies them.

An agent turn, an approval resume, and a delegated child's resume all run the same policy around the loop: the
participation gate, mid-turn compaction, the approval receipt, the output-limit answer, portable tool search, the
tool-call middleware, the queued-message notices, and the skill-review capture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom import ai_runtime
from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.output_limit import output_limit_policy
from mindroom.agent_loop.tool_search import resolve_call_tool_calls
from mindroom.approval.receipt import approval_receipt_hook
from mindroom.history.mid_turn_compaction import mid_turn_compaction_hook
from mindroom.participation_hook import participation_hook
from mindroom.skill_learning.capture import capture_final_request

if TYPE_CHECKING:
    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.model_loop import BeforeRequestHook
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.history.types import CompactionLifecycle, HistoryScope
    from mindroom.llm.provider import NativeCompactionReplay
    from mindroom.participation import ParticipationGate
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.skill_learning.capture import SkillReviewCapture


def agent_loop_hooks(
    build: AgentBuild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    model_name: str,
    store: ConversationStore | None,
    session: ConversationSession,
    scope: HistoryScope | None,
    scoped: bool,
    native_compaction: NativeCompactionReplay | None,
    prompt_id: str | None,
    participation: ParticipationGate | None = None,
    lifecycle: CompactionLifecycle | None = None,
    turn_recorder: TurnRecorder | None = None,
    skill_review_capture: SkillReviewCapture | None = None,
    run_id: str | None = None,
) -> LoopHooks:
    """Return the hooks of one agent run or resume, in the order the loop applies them before each request.

    The participation gate decides first, so a reply that stays silent never compacts or requests; the request
    cap is the loop's own and refuses a request before any hook runs; mid-turn compaction rewrites the live
    messages, and folds into the scope only when ``scoped`` says the run replays the scope's stored history; the
    approval receipt projects into the first request of a resumed run only. Queued-message notices follow every
    tool batch and a resume's entry; ``skill_review_capture`` records the final request of run ``run_id``.
    """
    model = build.spec.model
    capabilities = model.capabilities
    before_request: list[BeforeRequestHook] = []
    if participation is not None:
        before_request.append(participation_hook(participation, model))
    before_request.append(
        mid_turn_compaction_hook(
            config=config,
            runtime_paths=runtime_paths,
            entity_name=agent_name,
            scope_id=scope.scope_id if scope is not None else agent_name,
            is_team=False,
            model_name=model_name,
            model=model,
            history_settings=build.history_settings,
            store=store,
            session=session if scoped else None,
            scope=scope if scoped else None,
            native_compaction=native_compaction,
            deferred_tool_names=build.request_template.deferred_tool_names,
            prompt_id=prompt_id,
            lifecycle=lifecycle,
            turn_recorder=turn_recorder,
        ),
    )
    before_request.append(approval_receipt_hook(capabilities))
    notice_text = config.get_prompt("QUEUED_MESSAGE_NOTICE_TEXT")
    deferred = build.request_template.deferred_tool_names
    return LoopHooks(
        before_request=tuple(before_request),
        answer_tool_calls=output_limit_policy() if capabilities.claude_family else None,
        rewrite_tool_calls=resolve_call_tool_calls if deferred and not capabilities.native_tool_search else None,
        tool_middleware=build.tool_middleware,
        after_tool_batch=(ai_runtime.queued_notice_after_tool_batch(notice_text),),
        on_resume_entry=(ai_runtime.queued_notice_on_resume_entry(notice_text),),
        on_loop_end=(
            capture_final_request(skill_review_capture, model, run_id=run_id or "", model_name=model_name)
            if skill_review_capture is not None
            else None
        ),
    )


__all__ = ["agent_loop_hooks"]
