"""Shared Agno model-history run visibility policy."""

from typing import TypeGuard

from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput

NON_VISIBLE_RUN_STATUSES = frozenset({RunStatus.paused, RunStatus.cancelled, RunStatus.error})


def is_model_history_visible_run(run: object) -> TypeGuard[RunOutput | TeamRunOutput]:
    """Return whether one run is represented in Agno model history."""
    return (
        isinstance(run, (RunOutput, TeamRunOutput))
        and run.parent_run_id is None
        and run.status not in NON_VISIBLE_RUN_STATUSES
    )


# A queued-message notice, MindRoom's instruction to pause for a newer message, is stored as a user message marked so.
QUEUED_MESSAGE_NOTICE_MARKER_KEY = "mindroom_queued_message_notice"
QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER = "persisted"


def queued_notice_marker(message: Message) -> bool | str | None:
    """Return a queued-message notice's marker, True while live or persisted once stored, or None for other messages."""
    provider_data = message.provider_data
    marker = provider_data.get(QUEUED_MESSAGE_NOTICE_MARKER_KEY) if isinstance(provider_data, dict) else None
    return marker if marker in (True, QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER) else None


def is_queued_notice_message(message: Message) -> bool:
    """Return whether a message is a queued-message notice MindRoom wrote, live or persisted."""
    return queued_notice_marker(message) is not None
