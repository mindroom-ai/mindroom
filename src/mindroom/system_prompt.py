"""The date and session context, which opens the turn part of an agent's system prompt (``agent_loop.prompt``)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from mindroom.prompt_templates import render_prompt_template

if TYPE_CHECKING:
    from mindroom.config.main import Config


def render_session_context(config: Config, timezone: str) -> str:
    """Return the session context that opens the turn part of the system prompt.

    It names the current date in ``timezone`` and that timezone, never a clock time, so it changes only once a day.
    """
    now = datetime.now(ZoneInfo(timezone))
    date_context = render_prompt_template(
        config.get_prompt("DATETIME_CONTEXT_TEMPLATE"),
        date_str=now.strftime("%A, %B %d, %Y"),
        timezone_str=timezone,
        timezone_abbrev=now.tzname() or timezone,
    )
    return f"<mindroom_session_context>\n{date_context.strip()}\n</mindroom_session_context>"
