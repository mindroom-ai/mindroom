"""Where a team's new runs and resumes store: the scope's store and session, and the records a run hands over.

``register_team_notice_storage`` hands the team's storage to the turn's queued-notice finalization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom import ai_runtime
from mindroom.llm.events import RunFinished
from mindroom.llm.runs import TeamRunRecord
from mindroom.session_storage.store import persist_to

if TYPE_CHECKING:
    from mindroom.agent_loop.run import Persist
    from mindroom.config.main import Config
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.types import HistoryScope, ResolvedHistorySettings
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.teams.runtime import TeamRunEvent


@dataclass(frozen=True)
class TeamStorage:
    """Where one team run is stored: the scope's store and the session whose state its tools share."""

    store: ConversationStore
    session: ConversationSession
    scope: HistoryScope


@dataclass
class TeamRunRecords:
    """The terminal team record one team run handed over, whose ``ended_member_runs`` hold its members' latest records.

    It comes from the run's persist callback however the run ended, and from the ``RunFinished`` that hands it over.
    """

    record: TeamRunRecord | None = None

    def collect(self, event: TeamRunEvent) -> bool:
        """Keep the team record a ``RunFinished`` carries; return whether ``event`` was one."""
        if isinstance(event, RunFinished):
            assert isinstance(event.record, TeamRunRecord)
            self.record = event.record
            return True
        return False

    def keep(self, record: AnyRunRecord) -> None:
        """Keep the team's terminal record.

        Member records reach that record's ``ended_member_runs`` through the team runtime's member persist instead.
        """
        if isinstance(record, TeamRunRecord):
            self.record = record

    def terminal(self, team_name: str) -> TeamRunRecord:
        """Return the team record the run ended with, which a run that finished without raising always handed over."""
        if self.record is None:
            msg = f"team {team_name!r} run ended without its terminal record"
            raise RuntimeError(msg)
        return self.record


def scope_storage(scope_context: ScopeSessionContext) -> TeamStorage:
    """Return where a team run in ``scope_context`` is stored, giving a scope without a stored session its first one."""
    return TeamStorage(scope_context.storage, scope_context.stored_or_first_session(), scope_context.scope)


def persist_into(records: TeamRunRecords | None, storage: TeamStorage | None) -> Persist[AnyRunRecord]:
    """Return the persist callback of one team run: store every record, and keep the team's in ``records`` when given.

    Storing a record updates the session the run loaded, member rows included.
    """
    store_persist = persist_to(storage.store, storage.session) if storage is not None else None

    async def persist(record: AnyRunRecord) -> None:
        if records is not None:
            records.keep(record)
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


def register_team_notice_storage(*, scope_context: ScopeSessionContext | None, entity_name: str) -> None:
    """Register the scope's storage for response-boundary notice finalization; a team without a bound scope has none."""
    if scope_context is None:
        return
    ai_runtime.register_queued_notice_storage(
        storage_factory=scope_context.storage_factory,
        session_id=scope_context.session_id,
        session_kind="team",
        entity_name=entity_name,
    )


__all__ = [
    "TeamRunRecords",
    "TeamStorage",
    "configured_team",
    "persist_into",
    "register_team_notice_storage",
    "scope_storage",
    "team_history_settings",
]
