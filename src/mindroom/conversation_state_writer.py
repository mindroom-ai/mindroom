"""Conversation-state persistence helpers for bot flows."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from mindroom.constants import MATRIX_RESPONSE_EVENT_ID_METADATA_KEY
from mindroom.history.session_context import create_scope_session_storage, resolve_bound_team_scope
from mindroom.history.types import HistoryScope
from mindroom.hooks import EVENT_SESSION_STARTED, SessionHookContext, emit
from mindroom.runtime_protocols import SupportsConfig  # noqa: TC001
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.session_storage.store import watch_session_row
from mindroom.tool_system.runtime_context import runtime_context_from_dispatch_context, scope_hook_context_kwargs

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    import structlog

    from mindroom.constants import RuntimePaths
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_system.runtime_context import ToolDispatchContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class ConversationStateWriterDeps:
    """Static collaborators for conversation-state persistence and cache writes."""

    runtime: SupportsConfig
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str


@dataclass(frozen=True)
class ReplySession:
    """The stored session one reply writes: its history scope, and the dispatch the reply's tools run under."""

    scope: HistoryScope
    tool_dispatch: ToolDispatchContext


@asynccontextmanager
async def session_started_on_creation(
    reply: ReplySession | None,
    *,
    correlation_id: str,
) -> AsyncIterator[None]:
    """Run one reply, then emit ``session:started`` once if a write the reply made created its session's row.

    A write counts once it commits, in this block or in a task or thread the block starts, whichever writer made it:
    the run's save, an interrupted turn's record, a fold, or a helper's usage. The store is never read for this, and
    nothing is watched outside a live Matrix context or while no ``session:started`` hook is registered. A block that
    raises emits nothing.
    """
    context = runtime_context_from_dispatch_context(reply.tool_dispatch) if reply is not None else None
    if reply is None or context is None or not context.hooks.registry.has_hooks(EVENT_SESSION_STARTED):
        yield
        return
    with watch_session_row(reply.scope, context.session_id) as watch:
        yield
    if not watch.created:
        return
    hook_context = SessionHookContext(
        **scope_hook_context_kwargs(
            context,
            event_name=EVENT_SESSION_STARTED,
            correlation_id=correlation_id,
            scope=reply.scope,
            session_id=context.session_id,
        ),
    )
    await emit(context.hooks.registry, EVENT_SESSION_STARTED, hook_context)


@dataclass
class ConversationStateWriter:
    """Own the persisted conversation state for one bot."""

    deps: ConversationStateWriterDeps

    def history_scope(self) -> HistoryScope:
        """Return the persisted history scope backing this bot's runs."""
        if self.deps.agent_name in self.deps.runtime.config.teams:
            return HistoryScope(kind="team", scope_id=self.deps.agent_name)
        return HistoryScope(kind="agent", scope_id=self.deps.agent_name)

    def team_history_scope(
        self,
        member_names: Sequence[str],
        *,
        requester_user_id: str | None,
    ) -> HistoryScope:
        """Return the persisted history scope of one team response: this bot's configured team's, else its members'."""
        return resolve_bound_team_scope(
            member_names=member_names,
            config=self.deps.runtime.config,
            team_name=self.deps.agent_name,
            requester_user_id=requester_user_id,
        )

    def create_storage(
        self,
        execution_identity: ToolExecutionIdentity | None,
        *,
        scope: HistoryScope | None = None,
    ) -> ConversationStore:
        """Open the conversation store of one exact persisted history scope."""
        return create_scope_session_storage(
            scope=self.history_scope() if scope is None else scope,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            execution_identity=execution_identity,
        )

    async def apersist_response_event_id_in_session_run(
        self,
        *,
        execution_identity: ToolExecutionIdentity | None,
        scope: HistoryScope,
        session_id: str,
        run_id: str,
        response_event_id: str,
    ) -> None:
        """Persist Matrix response linkage onto the run that produced it, off-loop in the session target's FIFO lane."""
        link = {MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: response_event_id}
        await run_session_storage_operation(
            partial(self.create_storage, execution_identity, scope=scope),
            lambda storage: storage.patch_run_metadata(session_id, run_id, link),
        )
