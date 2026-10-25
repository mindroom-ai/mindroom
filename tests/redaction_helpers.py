"""Run the history cleanup a response performs after a redaction, outside a full response."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.response_turn import ResponseTurnContext, _remove_history_of_redacted_events

if TYPE_CHECKING:
    from mindroom.message_target import MessageTarget
    from mindroom.session_storage.store import ConversationStore
    from mindroom.turn_store import TurnStore

_AGENT_SCOPE = HistoryScope(kind="agent", scope_id="agent")


async def remove_redacted_history_like_next_response(
    store: TurnStore,
    target: MessageTarget,
    storage: ConversationStore,
    *,
    scope: HistoryScope = _AGENT_SCOPE,
) -> None:
    """Open one history scope the way the next response's turn driver does."""
    session = storage.load_session(target.session_id, scope.kind)
    await _remove_history_of_redacted_events(
        ResponseTurnContext(
            entity_label="agent",
            session_id=target.session_id,
            run_id=None,
            correlation_id="next-response",
            reply_to_event_id=None,
            room_id=target.room_id,
            thread_id=target.resolved_thread_id,
            requester_id="@user:example.org",
            matrix_run_metadata=None,
            redacted_history_events=partial(store.redacted_history_events, target),
        ),
        ScopeSessionContext(scope=scope, storage=storage, session=session, session_id=target.session_id),
    )
