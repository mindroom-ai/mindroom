"""Direct tests for Matrix thread resolution and outbound mutation membership helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.attachment_helpers import resolve_canonical_tool_thread_target
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.relation_lookup import RelationLookup
from mindroom.matrix.thread_diagnostics import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_DEGRADED,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
)
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    ThreadResolution,
    ThreadResolutionState,
    resolve_related_event_thread_membership,
    thread_messages_thread_membership_access,
)
from mindroom.matrix.thread_mutation_impact import (
    event_thread_membership_known,
    redaction_thread_membership_known,
)
from mindroom.message_target import MessageTarget
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    FakeRelationStore,
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.event_journal import RelationView
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_THREAD_REPLY_CONTENT = {
    "body": "thread reply",
    "msgtype": "m.text",
    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-root:localhost"},
}


def _message_event_info(content: dict[str, object]) -> EventInfo:
    return EventInfo.from_event(
        {
            "type": "m.room.message",
            "content": content,
        },
    )


def _serving_client(
    event_id: str,
    *,
    content: dict[str, object],
    event_type: str = "m.room.message",
) -> MagicMock:
    """Return a homeserver that serves one event for every point lookup."""
    client = MagicMock()
    client.room_get_event = AsyncMock(
        return_value=nio.RoomGetEventResponse.from_dict(
            {
                "event_id": event_id,
                "sender": "@user:localhost",
                "origin_server_ts": 1,
                "room_id": "!room:localhost",
                "type": event_type,
                "content": content,
            },
        ),
    )
    return client


def _tool_context(
    runtime_root: Path,
    *,
    thread_id: str | None,
    relations: RelationLookup,
) -> ToolRuntimeContext:
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(runtime_root),
    )
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id="!room:localhost",
            thread_id=thread_id,
            reply_to_event_id=None,
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=relations,
        conversation_reader=make_conversation_reader_mock(),
        room=None,
        storage_path=runtime_root,
    )


@pytest.mark.asyncio
async def test_resolve_related_event_thread_membership_terminates_on_relation_cycle() -> None:
    """A reply cycle must terminate as room-level instead of walking relations forever."""
    event_infos = {
        "$cycle-a:localhost": _message_event_info(
            {
                "body": "a",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$cycle-b:localhost"}},
            },
        ),
        "$cycle-b:localhost": _message_event_info(
            {
                "body": "b",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$cycle-a:localhost"}},
            },
        ),
    }
    fetched_event_ids: list[str] = []

    async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
        return None

    async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
        fetched_event_ids.append(event_id)
        return event_infos[event_id]

    async def prove_thread_root(_room_id: str, _thread_root_id: str) -> ThreadResolution:
        msg = "events with relations can never become thread roots"
        raise AssertionError(msg)

    resolution = await resolve_related_event_thread_membership(
        "!room:localhost",
        "$cycle-a:localhost",
        access=ThreadMembershipAccess(
            lookup_thread_id=lookup_thread_id,
            fetch_event_info=fetch_event_info,
            prove_thread_root=prove_thread_root,
        ),
    )

    assert (resolution.state, resolution.thread_id) == (ThreadResolutionState.ROOM_LEVEL, None)
    assert fetched_event_ids == ["$cycle-a:localhost", "$cycle-b:localhost"]


@pytest.mark.asyncio
async def test_thread_root_proof_from_degraded_history_is_indeterminate() -> None:
    """An empty degraded thread read must yield an indeterminate result, never room-level demotion."""
    thread_root_id = "$thread-root:localhost"
    degraded_history = ThreadHistoryResult(
        [],
        is_full_history=False,
        diagnostics={
            THREAD_HISTORY_SOURCE_DIAGNOSTIC: THREAD_HISTORY_SOURCE_DEGRADED,
            THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True,
        },
    )

    async def lookup_thread_id(_room_id: str, _event_id: str) -> str | None:
        return None

    async def fetch_event_info(_room_id: str, _event_id: str) -> EventInfo | None:
        return _message_event_info({"body": "root", "msgtype": "m.text"})

    async def fetch_thread_messages(_room_id: str, _thread_id: str) -> ThreadHistoryResult:
        return degraded_history

    resolution = await resolve_related_event_thread_membership(
        "!room:localhost",
        thread_root_id,
        access=thread_messages_thread_membership_access(
            lookup_thread_id=lookup_thread_id,
            fetch_event_info=fetch_event_info,
            fetch_thread_messages=fetch_thread_messages,
            absence_needs_full_history=True,
        ),
    )

    assert resolution.state is ThreadResolutionState.INDETERMINATE
    assert resolution.thread_id is None


@pytest.mark.asyncio
async def test_event_thread_membership_known_for_a_reply_into_a_thread() -> None:
    """A reply whose target is in a thread has known membership."""
    relations = make_relation_lookup(threads={"$thread-reply:localhost": "$thread-root:localhost"})
    known = await event_thread_membership_known(
        make_conversation_reader_mock(),
        relations,
        "!room:localhost",
        event_type="m.room.message",
        content={
            "body": "bridged reply",
            "msgtype": "m.text",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
        },
    )

    assert known


@pytest.mark.parametrize(
    "relation",
    [
        {"m.in_reply_to": {"event_id": "$sticker:localhost"}},
        {"event_id": "$sticker:localhost", "rel_type": "m.reference"},
    ],
)
@pytest.mark.asyncio
async def test_event_thread_membership_unknown_through_non_message_ancestors(
    relation: dict[str, object],
) -> None:
    """Reply and reference walks must enforce the conversation-family boundary."""
    relations = make_relation_lookup(
        client=_serving_client(
            "$sticker:localhost",
            event_type="m.sticker",
            content={
                "body": "sticker",
                "m.relates_to": {
                    "event_id": "$thread-root:localhost",
                    "rel_type": "m.thread",
                },
                "url": "mxc://localhost/sticker",
            },
        ),
    )

    known = await event_thread_membership_known(
        make_conversation_reader_mock(),
        relations,
        "!room:localhost",
        event_type="m.room.message",
        content={
            "body": "bridged relation",
            "msgtype": "m.text",
            "m.relates_to": relation,
        },
    )

    assert not known


def _failing_relations(failure: str) -> RelationLookup:
    """Return a relation lookup whose homeserver or journal raises ``boom``."""
    relations_client = MagicMock()
    relations_client.room_get_event = AsyncMock(side_effect=RuntimeError("boom"))
    if failure == "homeserver":
        return make_relation_lookup(client=relations_client)
    relations = make_relation_lookup(client=_serving_client("$thread-reply:localhost", content=_THREAD_REPLY_CONTENT))
    cast("FakeRelationStore", relations.store).failure = RuntimeError("boom")
    return relations


@pytest.mark.parametrize("failure", ["homeserver", "journal"])
@pytest.mark.asyncio
async def test_event_thread_membership_unknown_on_lookup_failure(failure: str) -> None:
    """A failed lookup leaves membership unknown instead of room level, and logs why."""
    with capture_logs() as logs:
        known = await event_thread_membership_known(
            make_conversation_reader_mock(),
            _failing_relations(failure),
            "!room:localhost",
            event_type="m.room.message",
            content={
                "body": "bridged reply",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
            },
        )

    assert not known
    [refusal] = [log for log in logs if log["event"] == "Refusing a Matrix mutation whose thread membership is unknown"]
    assert refusal["log_level"] == "warning"
    assert (refusal["room_id"], refusal["mutation"], refusal["target_event_id"]) == (
        "!room:localhost",
        "send_event",
        None,
    )
    assert str(refusal["exc_info"]) == "boom"


@pytest.mark.parametrize("failure", ["homeserver", "journal"])
@pytest.mark.asyncio
async def test_redaction_thread_membership_unknown_on_lookup_failure(failure: str) -> None:
    """A redaction whose target lookup fails is unknown instead of room level, and logs why."""
    with capture_logs() as logs:
        known = await redaction_thread_membership_known(
            make_conversation_reader_mock(),
            _failing_relations(failure),
            "!room:localhost",
            event_id="$thread-reply:localhost",
        )

    assert not known
    [refusal] = [log for log in logs if log["event"] == "Refusing a Matrix mutation whose thread membership is unknown"]
    assert refusal["log_level"] == "warning"
    assert (refusal["room_id"], refusal["mutation"], refusal["target_event_id"]) == (
        "!room:localhost",
        "redact",
        "$thread-reply:localhost",
    )
    assert str(refusal["exc_info"]) == "boom"


@pytest.mark.asyncio
async def test_redaction_thread_membership_known_for_reactions() -> None:
    """Redacting a reaction cannot affect thread membership, so it is known."""
    relations = make_relation_lookup(
        client=_serving_client(
            "$reaction:localhost",
            event_type="m.reaction",
            content={
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$thread-reply:localhost",
                    "key": "👍",
                },
            },
        ),
    )

    known = await redaction_thread_membership_known(
        make_conversation_reader_mock(),
        relations,
        "!room:localhost",
        event_id="$reaction:localhost",
    )

    assert known


@pytest.mark.asyncio
async def test_resolve_canonical_tool_thread_target_uses_context_thread(tmp_path: Path) -> None:
    """Tool-facing normalization applies the context thread before canonicalizing it."""
    relations_client = _serving_client("$ctx-thread:localhost", content=_THREAD_REPLY_CONTENT)
    context = _tool_context(
        tmp_path,
        thread_id="$ctx-thread:localhost",
        relations=make_relation_lookup(client=relations_client),
    )

    target = await resolve_canonical_tool_thread_target(
        context,
        room_id=context.room_id,
        thread_id=None,
    )

    assert target.requested_thread_id == "$ctx-thread:localhost"
    assert target.canonical_thread_id == "$thread-root:localhost"
    assert target.error is None
    relations_client.room_get_event.assert_awaited_once_with(context.room_id, "$ctx-thread:localhost")


@pytest.mark.asyncio
async def test_resolve_canonical_tool_thread_target_requires_thread_context(tmp_path: Path) -> None:
    """Tool-facing normalization returns the shared missing-thread error when no target is available."""
    relations_client = _serving_client("$ctx-thread:localhost", content=_THREAD_REPLY_CONTENT)
    context = _tool_context(tmp_path, thread_id=None, relations=make_relation_lookup(client=relations_client))

    target = await resolve_canonical_tool_thread_target(
        context,
        room_id=context.room_id,
        thread_id=None,
    )

    assert target.requested_thread_id is None
    assert target.canonical_thread_id is None
    assert target.error == "thread_id is required when no active thread context is available for the target room."
    relations_client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_canonical_tool_thread_target_fail_closed_preserves_requested_thread_id(tmp_path: Path) -> None:
    """Fail-closed tool normalization keeps the resolved request target for error reporting."""
    relations_client = _serving_client(
        "$ctx-thread:localhost",
        content={
            "body": "bridged reply",
            "msgtype": "m.text",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
        },
    )
    # The walk asks the journal which thread the reply target is in, and a
    # journal that cannot answer fails the normalization rather than guessing.
    relations = RelationLookup(
        store=cast("RelationView", FakeRelationStore(failure=TimeoutError("timed out"))),
        runtime=SimpleNamespace(client=relations_client),  # type: ignore[arg-type]
    )
    context = _tool_context(tmp_path, thread_id="$ctx-thread:localhost", relations=relations)

    target = await resolve_canonical_tool_thread_target(
        context,
        room_id=context.room_id,
        thread_id=None,
        fail_closed_on_normalization_error=True,
    )

    assert target.requested_thread_id == "$ctx-thread:localhost"
    assert target.canonical_thread_id is None
    assert target.error == "Failed to resolve a canonical thread root for the target event."
