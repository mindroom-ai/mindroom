"""Configure native provider replay under the existing history policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.native_compaction import NativeCompactionModel, recorded_native_settings

if TYPE_CHECKING:
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession

    from mindroom.history.types import ResolvedHistoryExecutionPlan, ResolvedHistorySettings


def configure_native_history(
    model: object,
    *,
    plan: ResolvedHistoryExecutionPlan,
    history_settings: ResolvedHistorySettings,
    session: AgentSession | TeamSession | None,
    allowed: bool,
) -> NativeCompactionModel | None:
    """Enable native requests only when they preserve the scope's authored semantics."""
    if not isinstance(model, NativeCompactionModel):
        return None
    if plan.hard_replay_budget_tokens is not None:
        model.configure_portable_replay()
    threshold = plan.trigger_threshold_tokens
    static_tokens = plan.static_prompt_tokens
    hard_budget = plan.hard_replay_budget_tokens
    enabled = (
        allowed
        and plan.authored_compaction_enabled
        and not plan.explicit_compaction_model
        and history_settings.policy.mode == "all"
        and history_settings.max_tool_calls_from_history is None
        and threshold is not None
        and static_tokens is not None
        and hard_budget is not None
        and static_tokens < threshold < static_tokens + hard_budget
    )
    summary = session.summary.summary.strip() if session is not None and session.summary is not None else ""
    model.configure_native_compaction(
        threshold=threshold if enabled else None,
        history_generation=summary,
        allow_authored=enabled,
    )
    return model


def restore_native_history(
    model: object,
    *,
    persisted_run: RunOutput | TeamRunOutput,
    session: AgentSession | TeamSession | None,
) -> None:
    """Restore the paused request's policy only when its rebuilt route still matches."""
    if not isinstance(model, NativeCompactionModel):
        return
    model.configure_portable_replay()
    latest = next((message for message in reversed(persisted_run.messages or []) if message.role == "assistant"), None)
    saved = recorded_native_settings(latest) if latest is not None else None
    summary = session.summary.summary.strip() if session is not None and session.summary is not None else ""
    model.configure_native_compaction(
        threshold=saved.threshold if saved is not None else None,
        history_generation=summary,
        allow_authored=saved is not None and saved.threshold is None,
    )
    if model.native_compaction is not None and saved is not None and model.native_compaction.route != saved.route:
        model.configure_native_compaction(threshold=None)


def native_history_route(model: NativeCompactionModel | None) -> str | None:
    """Return the active projection identity, or canonical history when disabled."""
    return model.native_compaction.route if model is not None and model.native_compaction is not None else None
