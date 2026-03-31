"""Tests for hook enrichment rendering, stripping, and caching."""

from __future__ import annotations

from dataclasses import dataclass

from agno.db.base import SessionType
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.ai import _build_cache_key
from mindroom.hooks import (
    EnrichmentItem,
    compute_enrichment_digest,
    render_enrichment_block,
    strip_enrichment_block,
    strip_enrichment_from_session_storage,
)


@dataclass
class _FakeStorage:
    session: AgentSession | TeamSession | None
    upserted_session: AgentSession | TeamSession | None = None

    def get_session(self, session_id: str, _session_type: object) -> AgentSession | TeamSession | None:
        if self.session is None or self.session.session_id != session_id:
            return None
        return self.session

    def upsert_session(self, session: AgentSession | TeamSession) -> None:
        self.upserted_session = session


def test_render_enrichment_block_and_digest_are_stable() -> None:
    """Rendering and digesting should be deterministic for one item set."""
    items = [
        EnrichmentItem(key="location", text="User is in Amsterdam", cache_policy="stable"),
        EnrichmentItem(key="weather", text="12C and windy", cache_policy="volatile"),
    ]

    rendered = render_enrichment_block(items)
    digest = compute_enrichment_digest(items)

    assert rendered == (
        "<mindroom_message_context>\n"
        '<item key="location" cache_policy="stable">\n'
        "User is in Amsterdam\n"
        "</item>\n"
        '<item key="weather" cache_policy="volatile">\n'
        "12C and windy\n"
        "</item>\n"
        "</mindroom_message_context>"
    )
    assert digest == compute_enrichment_digest(list(items))


def test_render_enrichment_block_escapes_xml_sensitive_content() -> None:
    """Rendered enrichment should escape keys and text so the block stays well-formed."""
    rendered = render_enrichment_block(
        [
            EnrichmentItem(
                key='weather"<now>',
                text='Use <rain & wind> "carefully"',
            ),
        ],
    )

    assert rendered == (
        "<mindroom_message_context>\n"
        '<item key="weather&quot;&lt;now&gt;" cache_policy="volatile">\n'
        "Use &lt;rain &amp; wind&gt; &quot;carefully&quot;\n"
        "</item>\n"
        "</mindroom_message_context>"
    )


def test_strip_enrichment_block_removes_rendered_context() -> None:
    """Persisted session text should not keep transient enrichment blocks."""
    text = (
        "User prompt\n\n"
        "<mindroom_message_context>\n"
        '<item key="calendar" cache_policy="volatile">\n'
        "Meeting at 3pm\n"
        "</item>\n"
        "</mindroom_message_context>\n\n"
        "Actual prompt"
    )

    assert strip_enrichment_block(text) == "User prompt\nActual prompt"


def test_strip_enrichment_from_session_storage_updates_agent_runs() -> None:
    """Stored agent session history should be scrubbed."""
    enriched = (
        "Question\n\n"
        "<mindroom_message_context>\n"
        '<item key="todos" cache_policy="stable">\n'
        "3 open todos\n"
        "</item>\n"
        "</mindroom_message_context>"
    )
    session = AgentSession(
        session_id="session-1",
        agent_id="agent-1",
        runs=[
            RunOutput(
                session_id="session-1",
                messages=[
                    Message(role="user", content=enriched, compressed_content=enriched),
                ],
            ),
        ],
    )
    storage = _FakeStorage(session)

    changed = strip_enrichment_from_session_storage(storage, "session-1")

    assert changed is True
    assert storage.upserted_session is session
    agent_message = session.runs[0].messages[0]
    assert agent_message.content == "Question"
    assert agent_message.compressed_content == "Question"


def test_strip_enrichment_from_session_storage_updates_team_runs() -> None:
    """Stored team session history should be scrubbed from the shared team DB."""
    enriched = (
        "Question\n\n"
        "<mindroom_message_context>\n"
        '<item key="todos" cache_policy="stable">\n'
        "3 open todos\n"
        "</item>\n"
        "</mindroom_message_context>"
    )
    session = TeamSession(
        session_id="session-1",
        team_id="team-1",
        runs=[
            TeamRunOutput(
                session_id="session-1",
                messages=[
                    Message(role="user", content=enriched, compressed_content=enriched),
                ],
            ),
        ],
    )
    storage = _FakeStorage(session)

    changed = strip_enrichment_from_session_storage(
        storage,
        "session-1",
        session_type=SessionType.TEAM,
    )

    assert changed is True
    assert storage.upserted_session is session
    team_message = session.runs[0].messages[0]
    assert team_message.content == "Question"
    assert team_message.compressed_content == "Question"


def test_enrichment_digest_changes_ai_cache_key() -> None:
    """The local AI cache key should vary when enrichment changes."""

    class _Model:
        id = "test-model"

    class _Agent:
        name = "calculator"
        model = _Model()

    base_key = _build_cache_key(_Agent(), "prompt", "session-1", show_tool_calls=True)
    enriched_key = _build_cache_key(
        _Agent(),
        "prompt",
        "session-1",
        show_tool_calls=True,
        enrichment_digest="abc123",
    )

    assert base_key != enriched_key
    assert enriched_key.endswith(":enrichment=abc123:tool_calls=show")
