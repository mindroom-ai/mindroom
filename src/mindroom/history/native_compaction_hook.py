"""Post-turn loop hook emitting ``compaction:after`` for provider-native compaction checkpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.history.compaction import (
    compaction_hooks_registered,
    emit_compaction_hook,
    iso_utc_now,
    projected_messages_transcript,
)
from mindroom.history.request_size import RequestSizer, request_sizer
from mindroom.hooks import EVENT_COMPACTION_AFTER
from mindroom.logging_config import get_logger
from mindroom.model_usage import context_input_tokens_from_counts, message_context_tokens
from mindroom.native_compaction import ENCRYPTED_REPLAY_RESET_KEY, extract_native_checkpoint, native_replay

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.agent_loop.model_loop import AfterTurnHook, LoopState
    from mindroom.config.main import Config
    from mindroom.history.types import HistoryScope
    from mindroom.hooks import TranscriptLine
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider, ModelRequest
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.native_compaction import NativeCheckpoint

logger = get_logger(__name__)


def native_compaction_after_turn_hook(
    *,
    config: Config,
    scope: HistoryScope,
    model_name: str,
    model: ModelProvider,
    session_runs: Sequence[AnyRunRecord] | Callable[[], Sequence[AnyRunRecord]] = (),
) -> AfterTurnHook:
    """Return a post-turn loop hook that emits ``compaction:after`` when the provider compacted natively."""

    async def after_turn(state: LoopState, sent_request: ModelRequest, message: Message) -> None:
        checkpoint: NativeCheckpoint | None = extract_native_checkpoint(message)
        if checkpoint is None:
            return
        if not compaction_hooks_registered(EVENT_COMPACTION_AFTER):
            return
        try:
            stripped = sent_request.strip_encrypted_replay or bool(
                (message.provider_data or {}).get(ENCRYPTED_REPLAY_RESET_KEY),
            )
            reset_scope = model.capabilities.replay_reset_scope
            effective_request = replace(sent_request, strip_encrypted_replay=True) if stripped else sent_request
            sizer = request_sizer(model, config, model_name)
            token_count_before = (await asyncio.to_thread(sizer.size, effective_request)).tokens
            token_count_after = await _token_count_after(
                state=state,
                message=message,
                model=model,
                sizer=sizer,
            )
            runs = session_runs if isinstance(session_runs, Sequence) else session_runs()
            transcript = _native_transcript(
                sent_request,
                state.record,
                checkpoint.route,
                reset_scope=reset_scope,
                stripped=stripped,
                session_runs=runs,
            )
            await emit_compaction_hook(
                event_name=EVENT_COMPACTION_AFTER,
                scope=scope,
                transcript=transcript,
                session_id=state.record.session_id or "",
                token_count_before=token_count_before,
                token_count_after=token_count_after,
                compaction_summary=checkpoint.summary,
                kind="native",
                mode="auto",
                summary_model=model_name or state.record.model or model.identity.id,
                compacted_at=iso_utc_now(),
                compacted_run_count=None,
            )
        except Exception:
            logger.exception(
                "Failed to emit native compaction hook",
                scope=scope.key,
                message_id=checkpoint.message_id,
                encrypted=checkpoint.encrypted,
            )

    return after_turn


async def _token_count_after(
    *,
    state: LoopState,
    message: Message,
    model: ModelProvider,
    sizer: RequestSizer,
) -> int:
    """Return post-compaction token count from Anthropic split-iteration usage or re-sized post-checkpoint request."""
    if message.metrics.context is not None:
        identity = model.identity
        after_ctx = message_context_tokens(
            message,
            provider=identity.provider,
            configured_provider=sizer.configured_provider,
            model_id=identity.id,
        )
        billed_total = context_input_tokens_from_counts(
            input_tokens=message.metrics.tokens.input_tokens,
            cache_read_tokens=message.metrics.tokens.cache_read_tokens,
            cache_write_tokens=message.metrics.tokens.cache_write_tokens,
            provider=identity.provider,
            configured_provider=sizer.configured_provider,
            model_id=identity.id,
        )
        if after_ctx is not None and billed_total is not None and billed_total > after_ctx.tokens:
            return after_ctx.tokens
    return await asyncio.to_thread(model.request_tokens, state.request())


def _native_transcript(
    sent_request: ModelRequest,
    record: AnyRunRecord,
    route: str,
    *,
    reset_scope: str | None = None,
    stripped: bool = False,
    session_runs: Sequence[AnyRunRecord] = (),
) -> tuple[TranscriptLine, ...]:
    """Project the pre-compaction messages covered by this native checkpoint into compaction transcript lines."""
    replayed = native_replay(
        sent_request.messages,
        route,
        reset_scope=reset_scope,
        stripped=stripped,
    ).messages
    history = [msg for msg in replayed if msg.from_history]
    current = [msg for msg in replayed if not msg.from_history]
    return projected_messages_transcript(history, current, record=record, session_runs=session_runs)


__all__ = ["native_compaction_after_turn_hook"]
