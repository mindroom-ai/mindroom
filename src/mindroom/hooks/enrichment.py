"""Enrichment rendering and persistence helpers."""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, cast

from agno.db.base import SessionType
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

if TYPE_CHECKING:
    from agno.db.sqlite import SqliteDb
    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput

    from .types import EnrichmentItem


_ENRICHMENT_BLOCK_PATTERN = re.compile(
    r"\n*<mindroom_message_context>.*?</mindroom_message_context>\n*",
    re.DOTALL,
)


def render_enrichment_block(items: list[EnrichmentItem]) -> str:
    """Render enrichment items into one model-facing XML-like block."""
    if not items:
        return ""

    rendered_items = [
        "\n".join(
            (
                (
                    f'<item key="{html.escape(item.key, quote=True)}" '
                    f'cache_policy="{html.escape(item.cache_policy, quote=True)}">'
                ),
                html.escape(item.text),
                "</item>",
            ),
        )
        for item in items
    ]
    return "<mindroom_message_context>\n" + "\n".join(rendered_items) + "\n</mindroom_message_context>"


def strip_enrichment_block(text: str) -> str:
    """Remove rendered enrichment blocks from persisted text."""
    return _ENRICHMENT_BLOCK_PATTERN.sub("\n", text).strip()


def _strip_session_message_content(message: object) -> bool:
    typed_message = cast("Message", message)
    changed = False
    if isinstance(typed_message.content, str):
        stripped = strip_enrichment_block(typed_message.content)
        if stripped != typed_message.content:
            typed_message.content = stripped
            changed = True

    if isinstance(typed_message.compressed_content, str):
        stripped = strip_enrichment_block(typed_message.compressed_content)
        if stripped != typed_message.compressed_content:
            typed_message.compressed_content = stripped
            changed = True

    return changed


def _session_runs(session: AgentSession | TeamSession) -> list[RunOutput | TeamRunOutput]:
    return list(session.runs or [])


def strip_enrichment_from_session_storage(
    storage: SqliteDb,
    session_id: str,
    *,
    session_type: SessionType = SessionType.AGENT,
) -> bool:
    """Remove enrichment blocks from persisted Agno session history for one session."""
    raw_session = storage.get_session(session_id, session_type)
    if raw_session is None:
        return False

    if isinstance(raw_session, dict):
        session = (
            TeamSession.from_dict(cast("dict[str, object]", raw_session))
            if session_type is SessionType.TEAM
            else AgentSession.from_dict(cast("dict[str, object]", raw_session))
        )
    else:
        session = cast("AgentSession | TeamSession", raw_session)
    if session is None:
        return False

    changed = False
    for run in _session_runs(session):
        messages = run.messages
        if messages is None:
            continue
        for message in messages:
            changed = _strip_session_message_content(message) or changed

    if changed:
        storage.upsert_session(session)
    return changed
