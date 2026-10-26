"""The date and session context, which opens the turn part of an agent's system prompt (``agent_loop.prompt``)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from mindroom.prompt_templates import render_prompt_template


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
    """Return the session context that opens the turn part of the system prompt, which changes between days."""
    return f"<mindroom_session_context>\n{date_context.strip()}\n</mindroom_session_context>"
