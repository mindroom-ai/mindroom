"""Behavior tests for governed external event delivery."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import nio
import pytest

from mindroom import external_events
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.main import Config
from mindroom.constants import ORIGINAL_SENDER_KEY, PER_FIRE_THREAD_ROOT_KEY, resolve_primary_runtime_paths
from mindroom.conversation_resolver import MessageContext
from mindroom.custom_tools.external_events import ExternalEventsTools
from mindroom.entity_resolution import current_entity_id
from mindroom.ingress_validation import IngressValidator, IngressValidatorDeps
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.script_runs.policy import resolve_script_launch_grants
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import tool_stays_local
from mindroom.turn_policy import PreparedDispatch, TurnPolicy
from tests.authorization_helpers import make_test_tool_runtime_context, make_test_turn_policy_deps
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_relation_lookup, request_envelope

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("enforce_turn_authorization")]

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


class _Runtime:
    @asynccontextmanager
    async def external_event_delivery_scope(self, _agent_name: str, _client: object) -> AsyncIterator[None]:
        yield


class _Client:
    user_id = "@mindroom_watcher:example.org"
    device_id = "DEVICE"
    olm = None

    def __init__(self) -> None:
        self.rooms = {"!room:example.org": nio.MatrixRoom("!room:example.org", self.user_id)}
        self.events: dict[str, dict[str, Any]] = {}
        self.fail_after_send = False

    async def room_send(self, *, room_id: str, content: dict[str, Any], tx_id: str, **_kwargs: object) -> object:
        self.events.setdefault(tx_id, json.loads(json.dumps(content)))
        if self.fail_after_send:
            self.fail_after_send = False
            message = "connection lost after acceptance"
            raise RuntimeError(message)
        return nio.RoomSendResponse(f"${tx_id}", room_id)


@pytest.fixture
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolRuntimeContext:
    """Build a real authorization context and idempotent Matrix transport."""
    config = Config.model_validate(
        {
            "agents": {
                "watcher": {
                    "display_name": "Watcher",
                    "role": "Watch events",
                    "rooms": ["!room:example.org"],
                    "access": {"users": ["@owner:example.org", "@second:example.org"]},
                },
            },
        },
    )
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={"MATRIX_HOMESERVER": "https://example.org"},
    )
    config = bind_runtime_paths(config, runtime_paths)

    async def members(*_args: object) -> list[str]:
        return ["@owner:example.org", "@second:example.org", _Client.user_id]

    monkeypatch.setattr(external_events, "get_room_members", members)
    return make_test_tool_runtime_context(
        agent_name="watcher",
        requester_id="@owner:example.org",
        client=cast("Any", _Client()),
        target=MessageTarget.resolve(room_id="!room:example.org", thread_id="$launch", reply_to_event_id=None),
        config=config,
        runtime_paths=runtime_paths,
        orchestrator=cast("Any", _Runtime()),
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(),
    )


async def test_delivery_preserves_owner_and_freezes_duplicate(context: ToolRuntimeContext) -> None:
    """Actor metadata never replaces owner authority; delivered content is immutable."""
    first = await external_events.deliver_event(
        context,
        source="provider:subscription",
        event_id="one",
        message="Original",
        conversation_key="topic",
        actor_id="@administrator:example.org",
        data={"requester": "untrusted"},
    )
    duplicate = await external_events.deliver_event(
        replace(context, correlation_id="another-run"),
        source="provider:subscription",
        event_id="one",
        message="Edited",
        conversation_key="other-topic",
    )
    assert first["accepted"] is True
    assert duplicate == {**first, "duplicate": True}
    client = cast("_Client", context.client)
    assert len(client.events) == 1
    content = next(iter(client.events.values()))
    assert "Original" in content["body"]
    assert "Edited" not in content["body"]
    assert content[ORIGINAL_SENDER_KEY] == context.requester_id
    assert content["io.mindroom.external_event.actor_id"] == "@administrator:example.org"
    assert content[PER_FIRE_THREAD_ROOT_KEY] is True


async def test_crash_after_send_resumes_frozen_intent(context: ToolRuntimeContext) -> None:
    """Ambiguous transport outcomes reuse the exact transaction and first payload."""
    client = cast("_Client", context.client)
    client.fail_after_send = True
    with pytest.raises(external_events.ExternalEventDeliveryError):
        await external_events.deliver_event(
            context,
            source="feed",
            event_id="one",
            message="Original",
            conversation_key="topic",
        )
    receipt = await external_events.deliver_event(
        context,
        source="feed",
        event_id="one",
        message="Edited",
        conversation_key="elsewhere",
    )
    assert receipt["accepted"] is True
    assert len(client.events) == 1
    assert "Original" in next(iter(client.events.values()))["body"]


async def test_concurrent_deliveries_share_one_thread(context: ToolRuntimeContext) -> None:
    """Concurrent first arrivals serialize around one durable root."""
    results = await asyncio.gather(
        *(
            external_events.deliver_event(
                context,
                source="feed",
                event_id=str(i),
                message=f"Message {i}",
                conversation_key="topic",
            )
            for i in range(3)
        ),
    )
    client = cast("_Client", context.client)
    roots = [content for content in client.events.values() if content.get(PER_FIRE_THREAD_ROOT_KEY)]
    assert len(roots) == 1
    root_id = next(
        f"${transaction}" for transaction, content in client.events.items() if content.get(PER_FIRE_THREAD_ROOT_KEY)
    )
    assert root_id in {result["matrix_event_id"] for result in results}
    replies = [content for content in client.events.values() if not content.get(PER_FIRE_THREAD_ROOT_KEY)]
    assert len(replies) == 2
    assert all(content["m.relates_to"]["event_id"] == root_id for content in replies)


async def test_source_and_requester_scopes_are_isolated(context: ToolRuntimeContext) -> None:
    """Source and human ownership isolate event and conversation identities."""
    receipts = []
    for owner, source in [(context.requester_id, "a"), (context.requester_id, "b"), ("@second:example.org", "a")]:
        receipts.append(
            await external_events.deliver_event(
                replace(context, requester_id=owner),
                source=source,
                event_id="same",
                message="Hello",
                conversation_key="same",
            ),
        )
    assert len({receipt["matrix_event_id"] for receipt in receipts}) == 3


async def test_revocation_blocks_duplicate(context: ToolRuntimeContext) -> None:
    """Duplicate receipts cannot bypass current requester authorization."""
    await external_events.deliver_event(context, source="feed", event_id="one", message="Hello")
    denied = context.config.model_copy(deep=True)
    denied.agents["watcher"].access.users = ["@nobody:example.org"]
    denied = Config.model_validate(denied.authored_model_dump())
    with pytest.raises(external_events.ExternalEventDeliveryError, match="authorized"):
        await external_events.deliver_event(
            replace(context, config_provider=lambda: denied),
            source="feed",
            event_id="one",
            message="Hello",
        )


async def test_membership_failure_blocks_delivery(context: ToolRuntimeContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unavailable membership fails closed before any delivery."""

    async def unavailable(*_args: object) -> None:
        return None

    monkeypatch.setattr(external_events, "get_room_members", unavailable)
    with pytest.raises(external_events.ExternalEventDeliveryError, match="joined"):
        await external_events.deliver_event(context, source="feed", event_id="one", message="Hello")
    assert not cast("_Client", context.client).events


async def test_retry_ignores_changed_payload_validation(context: ToolRuntimeContext) -> None:
    """A known identity resumes its original payload even if edited text is now empty."""
    receipt = await external_events.deliver_event(context, source="feed", event_id="one", message="Original")
    retried = await external_events.deliver_event(
        context,
        source="feed",
        event_id="one",
        message="",
        conversation_key="x" * 300,
    )
    assert retried == {**receipt, "duplicate": True}


async def test_receipt_write_failure_never_returns_success(
    context: ToolRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disk failure after Matrix acceptance leaves a retryable frozen intent."""
    original = external_events._write

    def fail_receipt(path: Path, state: external_events._State) -> None:
        if any(intent.matrix_event_id is not None for intent in state.events.values()):
            message = "receipt unavailable"
            raise OSError(message)
        original(path, state)

    with monkeypatch.context() as patch:
        patch.setattr(external_events, "_write", fail_receipt)
        with pytest.raises(external_events.ExternalEventDeliveryError):
            await external_events.deliver_event(context, source="feed", event_id="one", message="Original")
    result = await external_events.deliver_event(context, source="feed", event_id="one", message="Edited")
    assert result["accepted"] is True
    assert len(cast("_Client", context.client).events) == 1


async def test_later_event_resumes_pending_root(context: ToolRuntimeContext) -> None:
    """A later message finishes the original conversation root after a lost acknowledgement."""
    client = cast("_Client", context.client)
    client.fail_after_send = True
    with pytest.raises(external_events.ExternalEventDeliveryError):
        await external_events.deliver_event(
            context,
            source="feed",
            event_id="one",
            message="First",
            conversation_key="topic",
        )
    first_transaction = next(iter(client.events))
    receipt = await external_events.deliver_event(
        context,
        source="feed",
        event_id="two",
        message="Second",
        conversation_key="topic",
    )
    assert len(client.events) == 2
    content = client.events[str(receipt["matrix_event_id"])[1:]]
    assert content["m.relates_to"]["event_id"] == f"${first_transaction}"
    assert PER_FIRE_THREAD_ROOT_KEY not in content


async def test_changed_device_cannot_retry_pending_send(context: ToolRuntimeContext) -> None:
    """An ambiguous send cannot be repeated through a different Matrix deduplication scope."""
    client = cast("_Client", context.client)
    client.fail_after_send = True
    with pytest.raises(external_events.ExternalEventDeliveryError):
        await external_events.deliver_event(context, source="feed", event_id="one", message="First")
    client.device_id = "NEW_DEVICE"
    with pytest.raises(external_events.ExternalEventDeliveryError, match="another Matrix device"):
        await external_events.deliver_event(context, source="feed", event_id="one", message="First")
    assert len(client.events) == 1


async def test_toolkit_returns_confirmed_receipt(context: ToolRuntimeContext) -> None:
    """The public tool wraps the same governed delivery service."""
    with tool_runtime_context(context):
        receipt = json.loads(await ExternalEventsTools().deliver_event("feed", "one", "Hello"))
    assert receipt["status"] == "ok"
    assert receipt["accepted"] is True
    assert receipt["matrix_event_id"].startswith("$")


async def test_background_tool_surface_includes_primary_delivery(context: ToolRuntimeContext) -> None:
    """Ordinary script grants can invoke the delivery toolkit on the primary runtime."""
    config_data = context.config.authored_model_dump()
    config_data["agents"]["watcher"]["tools"] = ["script", "external_events"]
    config = bind_runtime_paths(Config.model_validate(config_data), context.runtime_paths)
    grants = resolve_script_launch_grants(replace(context, config=config))
    assert any(grant.toolkit_name == "external_events" and grant.function_name == "deliver_event" for grant in grants)
    assert tool_stays_local("external_events")


async def test_actor_metadata_cannot_authorize_another_requester(context: ToolRuntimeContext) -> None:
    """An allowed external actor cannot grant the runtime requester any access."""
    with pytest.raises(external_events.ExternalEventDeliveryError, match="authorized"):
        await external_events.deliver_event(
            replace(context, requester_id="@stranger:example.org"),
            source="feed",
            event_id="one",
            message="Hello",
            actor_id=context.requester_id,
        )
    assert not cast("_Client", context.client).events


async def test_cancelled_send_releases_lock_and_keeps_intent(context: ToolRuntimeContext) -> None:
    """Cancellation after Matrix acceptance leaves durable ownership for a caller retry."""
    client = cast("_Client", context.client)
    original_send = client.room_send

    async def cancelled_send(*, room_id: str, content: dict[str, Any], tx_id: str, **kwargs: object) -> object:
        await original_send(room_id=room_id, content=content, tx_id=tx_id, **kwargs)
        raise asyncio.CancelledError

    client.room_send = cancelled_send
    with pytest.raises(asyncio.CancelledError):
        await external_events.deliver_event(context, source="feed", event_id="one", message="Original")
    client.room_send = original_send
    receipt = await external_events.deliver_event(context, source="feed", event_id="one", message="Edited")
    assert receipt["accepted"] is True
    assert len(client.events) == 1


async def test_confirmed_payloads_are_compacted_with_independent_thread_roots(context: ToolRuntimeContext) -> None:
    """Completed text is removed while receipt and thread identities remain usable."""
    first = await external_events.deliver_event(
        context,
        source="feed",
        event_id="one",
        message="Sensitive original text",
        conversation_key="topic",
    )
    directory = context.runtime_paths.control_state_root / "external_events"
    state_file = next(directory.glob("*.json"))
    assert "Sensitive original text" not in state_file.read_text()
    second = await external_events.deliver_event(
        context,
        source="feed",
        event_id="two",
        message="Next",
        conversation_key="topic",
    )
    client = cast("_Client", context.client)
    assert client.events[str(second["matrix_event_id"])[1:]]["m.relates_to"]["event_id"] == first["matrix_event_id"]


async def test_aliases_share_canonical_scope_and_membership(context: ToolRuntimeContext) -> None:
    """A bridge alias shares one durable source with its canonical human owner."""
    data = context.config.authored_model_dump()
    data["authorization"] = {"aliases": {context.requester_id: ["@bridge:example.org"]}}
    config = bind_runtime_paths(Config.model_validate(data), context.runtime_paths)
    canonical = replace(context, config=config)
    first = await external_events.deliver_event(canonical, source="feed", event_id="one", message="Hello")
    second = await external_events.deliver_event(
        replace(canonical, requester_id="@bridge:example.org"),
        source="feed",
        event_id="one",
        message="Hello",
    )
    assert second == {**first, "duplicate": True}
    assert len(cast("_Client", context.client).events) == 1


async def test_expired_receipts_are_pruned_but_pending_intents_survive(
    context: ToolRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eight-day receipt retention never erases unfinished delivery intent."""
    monkeypatch.setattr(external_events, "_now", lambda: 1000)
    await external_events.deliver_event(
        context,
        source="feed",
        event_id="old",
        message="Old",
        conversation_key="old-topic",
    )
    client = cast("_Client", context.client)
    client.fail_after_send = True
    with pytest.raises(external_events.ExternalEventDeliveryError):
        await external_events.deliver_event(
            context,
            source="feed",
            event_id="pending",
            message="Pending",
            conversation_key="pending-topic",
        )
    monkeypatch.setattr(external_events, "_now", lambda: 1000 + 9 * 86400)
    await external_events.deliver_event(context, source="feed", event_id="new", message="New")
    state_file = next((context.runtime_paths.control_state_root / "external_events").glob("*.json"))
    state = json.loads(state_file.read_text())
    assert external_events._key("old") not in state["events"]
    assert external_events._key("old-topic") not in state["threads"]
    assert external_events._key("pending") in state["events"]
    receipt = await external_events.deliver_event(context, source="feed", event_id="pending", message="Changed")
    assert receipt["accepted"] is True
    assert len(client.events) == 3


async def test_self_authored_delivery_enters_ingress_and_plans_response(context: ToolRuntimeContext) -> None:
    """The generated Matrix event wakes its own agent as the authorized human requester."""
    receipt = await external_events.deliver_event(context, source="feed", event_id="one", message="Inspect this event")
    client = cast("_Client", context.client)
    content = next(iter(client.events.values()))
    entity_id = current_entity_id(context.agent_name, context.runtime_paths)
    assert entity_id.full_id == client.user_id
    assert content["m.mentions"]["user_ids"] == [entity_id.full_id]
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": receipt["matrix_event_id"],
            "sender": client.user_id,
            "origin_server_ts": 1000,
            "content": content,
        },
    )
    runtime = BotRuntimeState(
        client=context.client,
        config=context.config,
        runtime_paths=context.runtime_paths,
        agent_reply_memberships=context.require_agent_reply_memberships(),
        enable_streaming=False,
        orchestrator=None,
    )
    policy = TurnPolicy(
        make_test_turn_policy_deps(
            runtime=runtime,
            logger=get_logger("test_external_events"),
            runtime_paths=context.runtime_paths,
            agent_name=context.agent_name,
            matrix_id=entity_id,
        ),
    )
    turn_store = MagicMock()
    turn_store.is_handled.return_value = False
    validator = IngressValidator(
        IngressValidatorDeps(
            runtime=runtime,
            runtime_paths=context.runtime_paths,
            matrix_id=entity_id,
            turn_store=turn_store,
            turn_policy=policy,
        ),
    )
    room = client.rooms[context.room_id]
    room.add_member(client.user_id, "Watcher", None)
    room.add_member(context.requester_id, "Owner", None)
    requester = await validator.precheck_event(room, event)
    assert requester == context.requester_id
    target = MessageTarget.resolve(room_id=context.room_id, thread_id=event.event_id, reply_to_event_id=event.event_id)
    message_context = MessageContext(
        am_i_mentioned=True,
        is_thread=False,
        thread_id=None,
        thread_history=[],
        mentioned_agents=[entity_id],
        has_non_agent_mentions=False,
    )
    dispatch = PreparedDispatch(
        requester_user_id=requester,
        context=message_context,
        target=target,
        correlation_id="external-event",
        envelope=request_envelope(
            room_id=context.room_id,
            reply_to_event_id=event.event_id,
            thread_id=event.event_id,
            prompt=event.body,
            user_id=requester,
            target=target,
            agent_name=context.agent_name,
        ),
    )
    plan = await policy.plan_turn(
        room,
        event,
        dispatch,
        is_dm=False,
        has_active_response_for_target=lambda _target: False,
    )
    assert plan.kind == "respond"


async def test_joined_bridge_alias_proves_canonical_membership(
    context: ToolRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Joined human bridge identities count toward the canonical owner's room membership."""
    data = context.config.authored_model_dump()
    data["authorization"] = {"aliases": {context.requester_id: ["@bridge:example.org"]}}
    config = bind_runtime_paths(Config.model_validate(data), context.runtime_paths)

    async def members(*_args: object) -> list[str]:
        return ["@bridge:example.org", _Client.user_id]

    monkeypatch.setattr(external_events, "get_room_members", members)
    receipt = await external_events.deliver_event(
        replace(context, config=config),
        source="feed",
        event_id="one",
        message="Hello",
    )
    assert receipt["accepted"] is True
