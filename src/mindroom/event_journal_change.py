"""The single rule that an event-journal move cannot be adopted by a live process.

The store behind ``event_journal`` is opened once at startup and shared by every
bot, so nothing at runtime can replace it. Applying a change would leave the
runtime committing turns and terminal records to the old database while adopted
state named the new one, and the next process start would open the new database
without any of that history -- replay and delivery dedupe would both begin from
a past that never happened. Reopening it safely means stopping every bot,
closing the store, and reopening before anything reads it, which is a process
restart by another name.

Two independent readers of ``config.yaml`` live in the same process: the
orchestrator's reload lifecycle, which applies a change to the running bots, and
the API's config publisher, which publishes it to everything keyed to the
snapshot generation. They must reach the same verdict from the same rule.
One refusing alone is worse than neither refusing: the runtime keeps the journal
it opened while the published generation advances past it, and every consumer
pinned to the generation it was bound at -- external trigger delivery -- starts
rejecting work against a runtime that never changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config

logger = get_logger(__name__)

EVENT_JOURNAL_CHANGE_REASON = "the event journal backend cannot change without restarting the process"


def refuses_event_journal_change(
    current_config: Config | None,
    new_config: Config,
    *,
    refused_by: str,
) -> bool:
    """Return whether ``new_config`` moves a journal this process already opened.

    ``current_config`` is ``None`` when nothing has been adopted yet -- the
    first load of a process -- where the journal ``new_config`` names is the one
    about to be opened and there is nothing to refuse.

    Logs the refusal here so one decision produces one message, whichever caller
    reached it first.
    """
    if current_config is None or new_config.event_journal == current_config.event_journal:
        return False
    logger.error(
        "config_reload_refused_event_journal_change",
        reason=EVENT_JOURNAL_CHANGE_REASON,
        refused_by=refused_by,
        current=current_config.event_journal.backend,
        requested=new_config.event_journal.backend,
    )
    return True


__all__ = ["EVENT_JOURNAL_CHANGE_REASON", "refuses_event_journal_change"]
