"""Configure native provider replay under the existing history policy."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.history.replay import compaction_summary_message, current_summary_text
from mindroom.history.types import NativeHistory
from mindroom.native_compaction import recorded_native_settings

if TYPE_CHECKING:
    from mindroom.history.types import ResolvedHistoryExecutionPlan, ResolvedHistorySettings
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider, ModelRequest
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.types import ConversationSession


def configure_native_history(
    provider: ModelProvider,
    *,
    plan: ResolvedHistoryExecutionPlan,
    history_settings: ResolvedHistorySettings,
    session: ConversationSession | None,
    allowed: bool,
) -> NativeHistory | None:
    """Return the native settings of a run's requests, with compaction only when it keeps the scope's authored semantics.

    None means the provider route manages no history of its own, so the requests carry no native settings.
    """
    history_route = provider.native_history_route
    if history_route is None:
        return None
    threshold = plan.trigger_threshold_tokens
    static_tokens = plan.static_prompt_tokens
    hard_budget = plan.hard_replay_budget_tokens
    enabled = (
        allowed
        and plan.authored_compaction_enabled
        and not plan.explicit_compaction_model
        and history_settings.replays_whole_history
        and threshold is not None
        and static_tokens is not None
        and hard_budget is not None
        and static_tokens < threshold < static_tokens + hard_budget
    )
    return NativeHistory(
        native_compaction=history_route.native_compaction_settings(
            threshold=threshold if enabled else None,
            history_generation=_history_generation(session),
            allow_authored=enabled,
        ),
        portable_replay=hard_budget is not None,
    )


def restore_native_history(
    provider: ModelProvider,
    *,
    persisted_run: AnyRunRecord,
    session: ConversationSession | None,
) -> NativeHistory | None:
    """Return the native settings a resume of ``persisted_run`` sends: the paused request's policy when its route holds."""
    history_route = provider.native_history_route
    if history_route is None:
        return None
    latest = next((message for message in reversed(persisted_run.messages) if message.role == "assistant"), None)
    saved = recorded_native_settings(latest) if latest is not None else None
    native = history_route.native_compaction_settings(
        threshold=saved.threshold if saved is not None else None,
        history_generation=_history_generation(session),
        allow_authored=saved is not None and saved.threshold is None,
    )
    if native is not None and saved is not None and native.route != saved.route:
        native = None
    return NativeHistory(native_compaction=native, portable_replay=_restored_portable_replay(latest))


def with_native_history(template: ModelRequest, native: NativeHistory | None) -> ModelRequest:
    """Return ``template`` carrying ``native``'s settings for provider-managed history; None carries none."""
    return replace(
        template,
        native_compaction=native.native_compaction if native is not None else None,
        portable_replay=native.portable_replay if native is not None else False,
    )


def _restored_portable_replay(message: Message | None) -> bool:
    """Return the replay policy the paused request ran with, from the provenance its last response recorded.

    Legacy responses cannot always reveal the original budget, so stored continuation is kept unless canonical
    replay has its own provenance.
    """
    data = (message.provider_data or {}) if message is not None else {}
    saved = data.get("mindroom_portable_replay")
    if isinstance(saved, bool):
        return saved
    items = data.get("mindroom_response_output")
    has_ordered_reasoning = (
        isinstance(items, list)
        and all(isinstance(item, dict) for item in items)
        and any(
            item.get("type") == "reasoning"
            and isinstance(encrypted := item.get("encrypted_content"), str)
            and bool(encrypted.strip())
            for item in items
        )
    )
    return (
        data.get("mindroom_response_stored") is False
        or (message is not None and recorded_native_settings(message) is not None)
        or has_ordered_reasoning
    )


def _history_generation(session: ConversationSession | None) -> str:
    """Bind checkpoints to the exact summary message their requests carried."""
    summary = current_summary_text(session) if session is not None else None
    return str(compaction_summary_message(summary, run_local=False).content) if summary is not None else ""


def native_history_route(native: NativeHistory | None) -> str | None:
    """Return the active projection identity, or canonical history when disabled."""
    return native.native_compaction.route if native is not None and native.native_compaction is not None else None


__all__ = [
    "configure_native_history",
    "native_history_route",
    "restore_native_history",
    "with_native_history",
]
