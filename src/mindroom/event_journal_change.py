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

Which journal is open is owned here, by the module that opens it, and not by any
of those three. Each of them only holds a view of the config file, and a view
can be missing: ``mindroom run --no-api`` starts the orchestrator with no API
snapshot at all, and the chat ``!config`` command and the config tools still
write through the same persist path, so an authority that asks "is an API
snapshot published?" answers "no journal is open" for a process that has one
open and is writing to it.

What counts as a move is only what changes the database actually opened. The
field carries settings that ``open_event_journal_store`` never reads for the
backend in force, and refusing over those would stop unrelated reloads for a
store that did not move -- for good, since a refusal never advances the adopted
config that later reloads are compared against.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.config.matrix import EventJournalConfig
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

# Keyed by config path rather than by the whole runtime context: the runtime
# context carries the resolution environment, which is not part of which
# database got opened, and two authorities resolving the same config file
# independently must land on the same key.
_OPENED_DATABASES: dict[Path, tuple[str, str | None]] = {}
_OPENED_DATABASES_LOCK = threading.Lock()

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


def record_opened_event_journal(
    journal_config: EventJournalConfig,
    *,
    runtime_paths: RuntimePaths,
) -> None:
    """Record the database this process just opened for ``runtime_paths``.

    Called from the one function that opens the store, so the fact is written
    where it becomes true rather than inferred from whichever authority happens
    to hold a config view.
    """
    with _OPENED_DATABASES_LOCK:
        _OPENED_DATABASES[runtime_paths.config_path] = _opened_database(journal_config, runtime_paths)


def _committed_database(
    current_config: Config | None,
    runtime_paths: RuntimePaths,
) -> tuple[str, str | None] | None:
    """Return the database this process is committed to, or ``None`` if none yet.

    A recorded open wins over the caller's adopted config. The two agree once a
    journal is open, because the config it was opened from is the adopted one
    and no move past that point is accepted. Where they can disagree is the
    startup window before the store opens, in which the API may have adopted a
    file the orchestrator has not read yet; there the record names the database
    that actually got opened, which is the one that cannot move.
    """
    with _OPENED_DATABASES_LOCK:
        opened = _OPENED_DATABASES.get(runtime_paths.config_path)
    if opened is not None:
        return opened
    if current_config is None:
        return None
    return _opened_database(current_config.event_journal, runtime_paths)


def refuses_event_journal_change(
    current_config: Config | None,
    new_config: Config,
    *,
    runtime_paths: RuntimePaths,
    refused_by: str,
) -> bool:
    """Return whether ``new_config`` moves a journal this process already opened.

    ``current_config`` is the caller's adopted config, consulted only until the
    store is actually opened; passing ``None`` means the caller has adopted
    nothing and is content to be answered from the opened-journal record alone.
    Both being absent -- the first load of a process, before anything is open --
    means the journal ``new_config`` names is the one about to be opened and
    there is nothing to refuse.

    Logs the refusal here so one decision produces one message, whichever caller
    reached it first. The resolved DSN never reaches the log: it can carry
    credentials, and whether it changed is the whole of what a reader needs.
    """
    opened = _committed_database(current_config, runtime_paths)
    if opened is None:
        return False
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
    "record_opened_event_journal",
    "refuses_event_journal_change",
]
