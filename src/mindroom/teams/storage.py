"""Where a team's new runs and resumes store: the scope's store and session, and the records a run hands over."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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
        """Keep the team's terminal record; the team runtime adds member records to its ``ended_member_runs``."""
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


__all__ = [
    "TeamRunRecords",
    "TeamStorage",
    "configured_team",
    "persist_into",
    "scope_storage",
    "team_history_settings",
]
