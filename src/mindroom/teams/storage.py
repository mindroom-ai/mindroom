"""Where a team's new runs and resumes store: the scope's store and session, and the records a run hands over.

``under_team_scope`` binds the team scope as the scope the running entity's tools address while the run's events
are pulled, and ``register_team_notice_storage`` hands the team's storage to the turn's queued-notice finalization.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom import ai_runtime
from mindroom.history.storage import new_scope_session
from mindroom.llm.events import RunFinished
from mindroom.llm.runs import TeamRunRecord
from mindroom.session_storage.store import persist_to
from mindroom.teams.runtime import MemberRunEnded
from mindroom.tool_system.context_bound_streams import context_bound_async_stream
from mindroom.tool_system.runtime_context import get_tool_runtime_context, tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractContextManager

    from mindroom.agent_loop.run import Persist
    from mindroom.config.main import Config
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.types import HistoryScope, ResolvedHistorySettings
    from mindroom.llm.runs import AnyRunRecord, RunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.teams.runtime import TeamRunEvent


@dataclass(frozen=True)
class TeamStorage:
    """Where one team run is stored: the scope's store and the session whose state its tools share."""

    store: ConversationStore
    session: ConversationSession
    scope: HistoryScope
    storage_factory: Callable[[], ConversationStore] | None


@dataclass
class TeamRunRecords:
    """The records one team run handed over: its terminal team record and each member run's latest record.

    ``record`` comes from the run's persist callback however the run ended, and from its ``RunFinished``.
    """

    record: TeamRunRecord | None = None
    members: list[RunRecord] = field(default_factory=list)

    def collect(self, event: TeamRunEvent) -> bool:
        """Keep the record ``event`` carries; return whether it carried one."""
        if isinstance(event, RunFinished):
            assert isinstance(event.record, TeamRunRecord)
            self.record = event.record
            return True
        if isinstance(event, MemberRunEnded):
            # A member resumed within this run ends twice; its later record replaces the paused one.
            self.members = [*(record for record in self.members if record.run_id != event.record.run_id), event.record]
            return True
        return False


def scope_storage(scope_context: ScopeSessionContext, *, session_id: str) -> TeamStorage:
    """Return where a team run in ``scope_context`` is stored, giving a scope without a stored session its first one."""
    session = scope_context.session or new_scope_session(
        session_id=session_id,
        scope_id=scope_context.scope.scope_id,
        is_team=True,
    )
    return TeamStorage(scope_context.storage, session, scope_context.scope, scope_context.storage_factory)


def persist_into(records: TeamRunRecords | None, storage: TeamStorage | None) -> Persist[AnyRunRecord]:
    """Return the persist callback of one team run: keep the team's terminal record and store every record.

    Storing a record updates the session the run loaded, member rows included.
    """
    store_persist = persist_to(storage.store, storage.session) if storage is not None else None

    async def persist(record: AnyRunRecord) -> None:
        if records is not None and isinstance(record, TeamRunRecord):
            records.record = record
        if store_persist is not None:
            await store_persist(record)

    return persist


def configured_team(config: Config, team_name: str | None) -> str | None:
    """Return the configured team ``team_name`` names, or None for an ad hoc team.

    A saved approval names the team as the entity whose account handles it, which for an ad hoc team is a member;
    an ad hoc team compacts and stores as no configured entity, under the defaults.
    """
    return team_name if team_name in config.teams else None


def team_history_settings(config: Config, team_name: str | None) -> ResolvedHistorySettings:
    """Return the history settings of the configured team, or the defaults for an ad hoc one."""
    return config.resolve_entity(configured_team(config, team_name)).history_settings


def register_team_notice_storage(
    *,
    scope_context: ScopeSessionContext | None,
    session_id: str | None,
    entity_name: str,
) -> None:
    """Register team storage for response-boundary notice finalization."""
    ai_runtime.register_queued_notice_storage(
        storage_factory=scope_context.storage_factory if scope_context is not None else None,
        session_id=session_id,
        session_kind="team",
        entity_name=entity_name,
    )


def under_team_scope(
    events: AsyncIterator[TeamRunEvent],
    scope: HistoryScope | None,
) -> AsyncIterator[TeamRunEvent]:
    """Pull the run's events with the team scope bound as the scope the running entity stores to.

    Tools that address history by scope, such as ``compact_context``, read it from the tool runtime context, so a
    member compacts the team's history, not its own agent's. The binding holds only while an event is pulled, so
    the consumer never runs under it, and it is read at each pull, so the bindings around it, such as the attempt
    model's, reach the run.
    """

    def with_scope() -> AbstractContextManager[None]:
        context = get_tool_runtime_context()
        bound = replace(context, history_scope=scope) if context is not None and scope is not None else context
        return tool_runtime_context(bound)

    return context_bound_async_stream(context_factory=with_scope, stream_factory=lambda: events)


__all__ = [
    "TeamRunRecords",
    "TeamStorage",
    "configured_team",
    "persist_into",
    "register_team_notice_storage",
    "scope_storage",
    "team_history_settings",
    "under_team_scope",
]
