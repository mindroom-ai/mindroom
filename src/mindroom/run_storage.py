"""Where one agent or team run stores, the terminal record it hands over, and the persist callback that keeps both.

A turn's attempt takes its ``RunStorage`` from the turn's scope context (``attempt_run_storage``), which keeps the
session the attempt saves into for the turn's later attempts; a team run outside a turn takes it from the scope
context it runs in (``scope_run_storage``). The hooks a run installs read the same value (``HookTurn``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.llm.events import RunFinished
from mindroom.llm.runs import TeamRunRecord
from mindroom.session_storage.store import persist_to

if TYPE_CHECKING:
    from mindroom.agent_loop.run import Persist
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.types import HistoryScope
    from mindroom.llm.runs import AnyRunRecord, RunRecord
    from mindroom.response_turn import TurnRunState
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession


@dataclass(frozen=True)
class RunStorage:
    """Where one run is stored: the scope's store, the session whose state its tools share, and the scope.

    An agent turn outside a scope has no ``scope``, and a run without a ``store`` stores nothing.
    """

    store: ConversationStore | None
    session: ConversationSession
    scope: HistoryScope | None


def scope_run_storage(scope_context: ScopeSessionContext) -> RunStorage:
    """Return where a run in ``scope_context`` is stored, giving a scope without a stored session its first one."""
    return RunStorage(scope_context.storage, scope_context.stored_or_first_session(), scope_context.scope)


def attempt_run_storage(run: TurnRunState) -> RunStorage | None:
    """Return where the turn's next attempt is stored, or None when the turn has no scope context.

    A scope without a stored session gets its first session, which stays on the turn's scope context, so a later
    attempt of the same turn replays this one's run and an empty-run discard finds it, as a reload of the scope would.
    """
    if run.scope_context is None:
        return None
    run.scope_context = run.scope_context.with_session()
    return scope_run_storage(run.scope_context)


@dataclass
class TerminalRecord[R: AnyRunRecord]:
    """The terminal ``kind`` record one run handed over.

    It comes from the run's persist callback however the run ended, and from the ``RunFinished`` that hands it over.
    A team run's persist callback also receives its members' records, which the team record's ``ended_member_runs``
    hold.
    """

    kind: type[R]
    record: R | None = None

    def collect(self, event: object) -> bool:
        """Keep the record a ``RunFinished`` carries; return whether ``event`` was one."""
        if isinstance(event, RunFinished):
            assert isinstance(event.record, self.kind)
            self.record = event.record
            return True
        return False

    def keep(self, record: AnyRunRecord) -> None:
        """Keep ``record`` when it is of this run's kind, which passes over a team's member records."""
        if isinstance(record, self.kind):
            self.record = record

    def terminal(self, name: str) -> R:
        """Return the record the run ended with, which a run that finished without raising always handed over.

        ``name`` names the run when it handed none over: an agent run by its run id, a team run by its team.
        """
        if self.record is None:
            raise without_terminal_record(self.kind, name)
        return self.record


def without_terminal_record(kind: type[RunRecord | TeamRunRecord], name: str) -> RuntimeError:
    """Return the error for a ``kind`` run named ``name`` that finished without handing over the record it ended with."""
    run = f"team {name!r} run" if kind is TeamRunRecord else f"agent run {name!r}"
    return RuntimeError(f"{run} ended without its terminal record")


def persist_into[R: AnyRunRecord](
    terminal: TerminalRecord[R] | None,
    storage: RunStorage | None,
) -> Persist[AnyRunRecord]:
    """Return the persist callback of one run: keep its terminal record in ``terminal`` when given, then store it.

    Every record the run hands over is stored where ``storage`` stores, which updates the session the run loaded,
    a team's member rows included; a run without a store stores nothing.
    """
    store_persist = (
        persist_to(storage.store, storage.session) if storage is not None and storage.store is not None else None
    )

    async def persist(record: AnyRunRecord) -> None:
        if terminal is not None:
            terminal.keep(record)
        if store_persist is not None:
            await store_persist(record)

    return persist


__all__ = [
    "RunStorage",
    "TerminalRecord",
    "attempt_run_storage",
    "persist_into",
    "scope_run_storage",
    "without_terminal_record",
]
