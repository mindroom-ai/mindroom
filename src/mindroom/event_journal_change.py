"""The single rule that an event-journal move cannot be adopted by a live process.

The store behind ``event_journal`` is opened once at startup and shared by every
bot, so nothing at runtime can replace it. Applying a change would leave the
runtime committing turns and terminal records to the old database while adopted
state named the new one, and the next process start would open the new database
without any of that history -- replay and delivery dedupe would both begin from
a past that never happened. Reopening it safely means stopping every bot,
closing the store, and reopening before anything reads it, which is a process
restart by another name.

Three authorities over ``config.yaml`` live in the same process: the
orchestrator's reload lifecycle, which applies a change to the running bots; the
API's config publisher, which publishes it to everything keyed to the snapshot
generation; and the config writers, which persist it to the file. They must
reach the same verdict from the same rule. One refusing alone is worse than
neither refusing: the runtime keeps the journal it opened while the published
generation advances past it, and every consumer pinned to the generation it was
bound at -- external trigger delivery -- starts rejecting work against a runtime
that never changed. A writer that does not refuse is worse still, because it
moves published state onto the new journal and every later comparison then
agrees with it, retiring the rule for the rest of the process.

What counts as a move is only what changes the database actually opened. The
field carries settings that ``open_event_journal_store`` never reads for the
backend in force, and refusing over those would stop unrelated reloads for a
store that did not move -- for good, since a refusal never advances the adopted
config that later reloads are compared against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.matrix import EventJournalConfig
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

EVENT_JOURNAL_CHANGE_REASON = "the event journal database cannot change without restarting the process"
EVENT_JOURNAL_CHANGE_MESSAGE = (
    "event_journal cannot change while MindRoom is running. "
    "Stop the process, change event_journal, then start it again."
)


def _opened_database(
    journal_config: EventJournalConfig,
    runtime_paths: RuntimePaths,
) -> tuple[str, str | None]:
    """Return which database ``open_event_journal_store`` would open for this config.

    Only what that function reads can make two configs name different stores.
    It branches on the backend and, for PostgreSQL, on the resolved DSN; the
    SQLite path comes from the runtime storage root and reads no
    ``event_journal`` field at all. So editing ``database_url`` while the
    backend stays ``sqlite`` opens the very same file and is not a move.

    An unresolvable PostgreSQL DSN gets its own identity: no database can be
    opened from it, so it is not the one currently open, and it is the same
    non-database as any other config that cannot resolve one either.
    """
    if journal_config.backend != "postgres":
        return ("sqlite", None)
    try:
        return ("postgres", journal_config.resolve_postgres_database_url(runtime_paths))
    except ValueError:
        return ("postgres", None)


def refuses_event_journal_change(
    current_config: Config | None,
    new_config: Config,
    *,
    runtime_paths: RuntimePaths,
    refused_by: str,
) -> bool:
    """Return whether ``new_config`` moves a journal this process already opened.

    ``current_config`` is ``None`` when nothing has been adopted yet -- the
    first load of a process -- where the journal ``new_config`` names is the one
    about to be opened and there is nothing to refuse.

    Logs the refusal here so one decision produces one message, whichever caller
    reached it first. The resolved DSN never reaches the log: it can carry
    credentials, and whether it changed is the whole of what a reader needs.
    """
    if current_config is None:
        return False
    opened = _opened_database(current_config.event_journal, runtime_paths)
    requested = _opened_database(new_config.event_journal, runtime_paths)
    if opened == requested:
        return False
    logger.error(
        "config_reload_refused_event_journal_change",
        reason=EVENT_JOURNAL_CHANGE_REASON,
        refused_by=refused_by,
        current_backend=opened[0],
        requested_backend=requested[0],
        database_url_changed=opened[1] != requested[1],
    )
    return True


__all__ = [
    "EVENT_JOURNAL_CHANGE_MESSAGE",
    "EVENT_JOURNAL_CHANGE_REASON",
    "refuses_event_journal_change",
]
