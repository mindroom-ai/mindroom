"""Run the history cleanup a response performs after a redaction, outside a full response."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from mindroom.history.session_context import load_scope_session_from_storage, load_scrubbed_scope_session
from mindroom.history.types import HistoryScope

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
    await load_scrubbed_scope_session(
        partial(load_scope_session_from_storage, scope, target.session_id, lambda: storage),
        partial(store.redacted_history_events, target),
    )
