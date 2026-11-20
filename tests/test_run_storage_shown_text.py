"""A stored reply replays as Matrix showed it, so the agent learns an image it made up was not sent."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.run_storage import RunStorage, TerminalRecord, persist_into
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.session_storage.types import ConversationSession
from tests.conftest import seed_session_row

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_stored_reply_carries_the_note_for_a_replaced_image(tmp_path: Path) -> None:
    """The stored assistant text holds the note Matrix showed; the run's handed-over record keeps the model's text."""
    store = create_agent_state_storage("code", tmp_path)
    session = ConversationSession(kind="agent", session_id="session", owner_id="code")
    seed_session_row(store, session)
    reply = "Logo: ![logo](mxc://placeholder)"
    record = RunRecord(
        run_id="run-1",
        session_id="session",
        agent_id="code",
        status=RunStatus.completed,
        created_at=1_700_000_000,
        content=reply,
        messages=[Message(role="user", content="show the logo"), Message(role="assistant", content=reply)],
    )
    terminal = TerminalRecord(RunRecord)

    await persist_into(terminal, RunStorage(store, session, None))(record)

    shown = "Logo: \u26a0\ufe0f Image not sent: logo (no uploaded file)"
    stored = store.load_session("session")
    assert stored is not None
    assert [message.content for message in stored.runs[0].messages] == ["show the logo", shown]
    assert stored.runs[0].content == shown
    assert terminal.record is record
    assert record.content == reply
