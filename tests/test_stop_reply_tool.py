"""Tests for the tool that stops another agent's reply."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.custom_tools.stop_reply import StopReplyTools
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.message_target import MessageTarget
from mindroom.response_sources import ResponseSources
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids, persist_entity_accounts
from tests.reply_span_helpers import reply_span, seed_finished_reply

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore

_ROOM = "!room:localhost"
_THREAD = "$thread:localhost"


class _Orchestrator:
    """Name each running entity's unfinished reply per conversation, as the live orchestrator does."""

    def __init__(self, replies: dict[tuple[str, str, str | None], str]) -> None:
        self.replies = replies

    async def open_reply_event_id(self, entity_name: str, room_id: str, thread_id: str | None) -> str | None:
        return self.replies.get((entity_name, room_id, thread_id))


def _context(tmp_path: Path) -> ToolRuntimeContext:
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General"), "helper": AgentConfig(display_name="Helper")}),
        test_runtime_paths(tmp_path),
    )
    persist_entity_accounts(config, runtime_paths_for(config))
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = entity_ids(config, runtime_paths_for(config))["general"].full_id
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(room_id=_ROOM, thread_id=_THREAD, reply_to_event_id=None),
        requester_id="@user:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        orchestrator=_Orchestrator({("helper", _ROOM, _THREAD): "$helper-reply:localhost"}),
    )


@pytest.mark.asyncio
async def test_stop_agent_reply_sends_a_stop_for_the_requester_on_the_agents_reply(tmp_path: Path) -> None:
    """The stop is a 🛑 on the agent's reply in this conversation that relays the user the caller acts for."""
    send = AsyncMock(return_value=nio.RoomSendResponse("$stop:localhost", _ROOM))
    with (
        patch("mindroom.custom_tools.stop_reply.send_room_event_result", new=send),
        tool_runtime_context(_context(tmp_path)),
    ):
        payload = json.loads(await StopReplyTools().stop_agent_reply("helper"))

    assert payload["status"] == "ok"
    assert payload["reply_event_id"] == "$helper-reply:localhost"
    room_id, event_type, content = send.await_args.args[1:]
    assert (room_id, event_type) == (_ROOM, "m.reaction")
    assert content == {
        "m.relates_to": {"rel_type": "m.annotation", "event_id": "$helper-reply:localhost", "key": "🛑"},
        ORIGINAL_SENDER_KEY: "@user:localhost",
        SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "acts_for_user", "message"),
    [
        ("general", True, "own reply"),
        ("router", True, "no reply in progress"),
        ("helper", False, "on behalf of"),
    ],
)
async def test_stop_agent_reply_refuses_what_it_cannot_stop(
    tmp_path: Path,
    agent: str,
    acts_for_user: bool,
    message: str,
) -> None:
    """Its own reply, an entity with no reply here, and a call with no user to act for send nothing."""
    context = _context(tmp_path)
    if not acts_for_user:
        # An agent acting for no user requests in its own name.
        context = replace(context, requester_id=context.client.user_id)
    send = AsyncMock()
    with patch("mindroom.custom_tools.stop_reply.send_room_event_result", new=send), tool_runtime_context(context):
        payload = json.loads(await StopReplyTools().stop_agent_reply(agent))

    assert payload["status"] == "error"
    assert message in payload["message"]
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_event_id_names_the_unfinished_reply_of_one_conversation(journal_store: EventJournalStore) -> None:
    """Only a reply still being written, in exactly this room and thread, can be the one to stop."""
    principal = journal_store.principal("agent@localhost")
    await seed_finished_reply(
        principal,
        "$finished:localhost",
        sources=ResponseSources(pending_event_ids=("$old:localhost",), logical_source_event_ids=("$old:localhost",)),
        room_id=_ROOM,
        thread_id=_THREAD,
    )
    assert await principal.replies.open_event_id(_ROOM, _THREAD) is None

    async with reply_span(
        principal,
        source_event_id="$source:localhost",
        room_id=_ROOM,
        thread_id=_THREAD,
        placeholder_event_id="$running:localhost",
    ):
        assert await principal.replies.open_event_id(_ROOM, _THREAD) == "$running:localhost"
        assert await principal.replies.open_event_id(_ROOM, None) is None
        assert await principal.replies.open_event_id(_ROOM, "$other-thread:localhost") is None
