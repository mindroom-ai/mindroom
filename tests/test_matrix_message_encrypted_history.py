"""Room-history reads retain encrypted wire events for the owned nio client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import nio
import pytest
import pytest_asyncio
from nio.crypto import InboundGroupSession, OlmAccount, OutboundGroupSession
from nio.durable import DurableSyncConfig

from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.event_journal import EventJournalStore
from mindroom.matrix._owned_session import MatrixCredentials, OwnedMatrixSession, open_owned_matrix_session
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import make_visible_message, serve_conversation_reader
from tests.test_matrix_message_tool import _make_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@dataclass
class _HttpResponse:
    payload: dict[str, Any]
    status: int = 200
    content_type: str = "application/json"
    content_disposition: None = None

    async def json(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.payload))


@dataclass
class _RoomHistory:
    context: ToolRuntimeContext
    owned: OwnedMatrixSession
    wire: list[dict[str, Any]] = field(default_factory=list)
    delivered: list[list[str]] = field(default_factory=list)

    def encrypted(
        self,
        event_id: str,
        timestamp: int,
        content: dict[str, Any],
        *,
        with_key: bool = True,
    ) -> dict[str, Any]:
        """Build real Megolm ciphertext and optionally retain its decryption key."""
        peer = OlmAccount()
        outbound = OutboundGroupSession()
        inbound = InboundGroupSession(
            outbound.session_key,
            peer.identity_keys["ed25519"],
            peer.identity_keys["curve25519"],
            self.context.room_id,
        )
        if with_key:
            assert self.owned.client.olm is not None
            with self.owned.session._outbound.transaction():
                self.owned.client.olm.inbound_group_store.add(inbound)
                self.owned.client.olm.save_inbound_group_session(inbound)
        outbound.mark_as_shared()
        ciphertext = outbound.encrypt(
            json.dumps({"room_id": self.context.room_id, "type": "m.room.message", "content": content}),
        )
        return {
            "type": "m.room.encrypted",
            "room_id": self.context.room_id,
            "event_id": event_id,
            "sender": "@alice:localhost",
            "origin_server_ts": timestamp,
            "content": {
                "algorithm": "m.megolm.v1.aes-sha2",
                "device_id": "PEER",
                "sender_key": peer.identity_keys["curve25519"],
                "session_id": outbound.id,
                "ciphertext": ciphertext,
            },
        }

    async def read(self, thread_id: str = "room") -> dict[str, Any]:
        with tool_runtime_context(self.context):
            return json.loads(await MatrixMessageTools().matrix_message(action="read", thread_id=thread_id, limit=5))


@pytest_asyncio.fixture
async def history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_RoomHistory]:
    """Keep owned setup, URL encoding, parsing, and crypto real; replace HTTP only."""
    context = _make_context(thread_id=None, storage_path=tmp_path)
    journal = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    owned = await open_owned_matrix_session(
        "https://localhost",
        MatrixCredentials(context.client.user_id, "DEVICE", "test-token"),
        context.runtime_paths,
        consumer_store=journal.principal(context.client.user_id),
        new_consumer_generation=uuid4(),
        config=DurableSyncConfig(),
    )
    owned.client.rooms.update(context.client.rooms)
    state = _RoomHistory(replace(context, client=owned.client), owned)

    async def transport(_client: nio.AsyncClient, method: str, path: str, *_args: object) -> _HttpResponse:
        parsed = urlparse(path)
        assert method == "GET"
        assert parsed.path.endswith("/messages")
        query = parse_qs(parsed.query)
        assert query["dir"] == ["b"]
        event_filter = json.loads(query.get("filter", ["{}"])[0])
        types = event_filter.get("types")
        # Apply the emitted wire filter before nio parses or decrypts any event.
        chunk = [event for event in state.wire if types is None or event["type"] in types]
        chunk = chunk[: int(query["limit"][0])]
        state.delivered.append([event["event_id"] for event in chunk])
        return _HttpResponse({"start": "start", "chunk": chunk})

    monkeypatch.setattr(nio.AsyncClient, "send", transport)
    MatrixMessageTools._recent_actions.clear()
    try:
        yield state
    finally:
        await owned.session.close()
        await owned.client.close()
        await journal.close()


def _plaintext() -> dict[str, Any]:
    return {
        "type": "m.room.message",
        "event_id": "$plain",
        "sender": "@alice:localhost",
        "origin_server_ts": 1,
        "content": {"msgtype": "m.text", "body": "older plaintext"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["encrypted-only", "mixed", "encrypted-edit"])
async def test_owned_room_read_decrypts_wire_history(history: _RoomHistory, scenario: str) -> None:
    """A plaintext-only server filter must not hide decryptable messages or edits."""
    history.wire = [history.encrypted("$encrypted", 2, {"msgtype": "m.text", "body": "encrypted original"})]
    expected = [("$encrypted", "encrypted original")]
    if scenario == "mixed":
        history.wire.append(_plaintext())
        expected.insert(0, ("$plain", "older plaintext"))
    elif scenario == "encrypted-edit":
        history.wire.insert(
            0,
            history.encrypted(
                "$edit",
                3,
                {
                    "msgtype": "m.text",
                    "body": "* corrected encrypted text",
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$encrypted"},
                    "m.new_content": {"msgtype": "m.text", "body": "corrected encrypted text"},
                },
            ),
        )
        expected = [("$encrypted", "corrected encrypted text")]

    # Establish valid wire envelopes and keys before exercising the tool's filter.
    control = await history.owned.client.room_messages(history.context.room_id, limit=5)
    assert isinstance(control, nio.RoomMessagesResponse)
    assert len(control.chunk) == len(history.wire)
    assert all(isinstance(event, nio.RoomMessageText) for event in control.chunk)

    payload = await history.read()

    assert payload["status"] == "ok"
    assert [(message["event_id"], message["body"]) for message in payload["messages"]] == expected
    if scenario == "encrypted-edit":
        assert payload["messages"][0]["timestamp"] == 2
        assert payload["messages"][0]["latest_event_id"] == "$edit"
    history.context.conversation_reader.read_strict.assert_not_called()


@pytest.mark.asyncio
async def test_owned_room_read_skips_ciphertext_without_keys(history: _RoomHistory) -> None:
    """An admitted encrypted event without its key stays invisible beside readable history."""
    history.wire = [
        history.encrypted("$missing-key", 2, {"msgtype": "m.text", "body": "unreadable"}, with_key=False),
        _plaintext(),
    ]

    payload = await history.read()

    assert payload["status"] == "ok"
    assert [(message["event_id"], message["body"]) for message in payload["messages"]] == [
        ("$plain", "older plaintext"),
    ]
    assert history.delivered == [["$missing-key", "$plain"]]


@pytest.mark.asyncio
async def test_owned_thread_read_uses_projection_without_room_history(history: _RoomHistory) -> None:
    """A selected thread still reads the journal projection, without direct room pagination."""
    message = make_visible_message(event_id="$projected", timestamp=1, sender="@alice:localhost", body="projected")
    serve_conversation_reader(history.context.conversation_reader, [message])
    history.wire = [history.encrypted("$unrelated", 2, {"msgtype": "m.text", "body": "room history"})]

    payload = await history.read(thread_id="$thread")

    assert payload["status"] == "ok"
    assert [(row["event_id"], row["body"]) for row in payload["messages"]] == [("$projected", "projected")]
    assert history.delivered == []
