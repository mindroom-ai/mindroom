"""Explicit boundary between shared agent instructions and session context."""

from datetime import datetime
from zoneinfo import ZoneInfo

from mindroom.prompt_templates import render_prompt_template

SESSION_CONTEXT_BOUNDARY = "<mindroom_session_context>"


def render_date_context(timezone_str: str, *, datetime_context_template: str) -> str:
    """Render the current date and timezone without a changing clock time."""
    now = datetime.now(ZoneInfo(timezone_str))
    return render_prompt_template(
        datetime_context_template,
        date_str=now.strftime("%A, %B %d, %Y"),
        timezone_str=timezone_str,
        timezone_abbrev=now.tzname() or timezone_str,
    )


def render_session_context(date_context: str) -> str:
    """Start the system suffix that may change between conversations or turns.

    Agno appends skills, summaries, and learning after additional_context.
    Keep those sections behind this conservative boundary as well, without
    parsing Agno's generated prose or changing their system-message role.
    """
    return f"{SESSION_CONTEXT_BOUNDARY}\n{date_context}\n</mindroom_session_context>"
