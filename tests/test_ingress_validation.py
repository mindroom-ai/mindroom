"""Focused tests for requester identity resolution at ingress."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_handoff import DispatchIngressMetadata, DispatchPayloadMetadata
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.entity_resolution import mindroom_user_id
from mindroom.ingress_validation import IngressValidator, IngressValidatorDeps
from mindroom.matrix import stale_stream_cleanup
from tests.access_schema_support import with_current_room_member_access
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_trusted_relay_resolves_requester_and_allows_self_authored_ingress(tmp_path: Path) -> None:
    """Trusted relays should preserve human requesters without trusting outsiders."""
    canonical_human = "@human:localhost"
    bridge_human = "@bridge_human:localhost"
    config = bind_runtime_paths(
        with_current_room_member_access(
            Config(
                agents={"test_agent": {"display_name": "Test Agent"}},
                bot_accounts=["@bridge_bot:localhost"],
                mindroom_user=MindRoomUserConfig(),
                models={"default": {"provider": "test", "id": "test-model"}},
                authorization={"aliases": {canonical_human: [bridge_human]}},
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    internal_user_id = mindroom_user_id(config, runtime_paths)
    assert internal_user_id is not None
    non_human_senders = (
        ids["test_agent"].full_id,
        ids["router"].full_id,
        "@bridge_bot:localhost",
        internal_user_id,
    )
    config.authorization.aliases[canonical_human].extend(non_human_senders)
    runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        enable_streaming=False,
        orchestrator=None,
    )
    turn_store = MagicMock()
    turn_store.is_handled.return_value = False
    turn_policy = MagicMock()
    turn_policy.can_reply_to_sender_in_room.return_value = True
    validator = IngressValidator(
        IngressValidatorDeps(
            runtime=runtime,
            runtime_paths=runtime_paths,
            matrix_id=ids["test_agent"],
            turn_store=turn_store,
            turn_policy=turn_policy,
        ),
    )
    content = stale_stream_cleanup._build_auto_resume_content(
        stale_stream_cleanup._InterruptedThread(
            room_id="!room:localhost",
            thread_id="$thread",
            target_event_id="$target",
            partial_text="partial",
            agent_name="test_agent",
            original_sender_id=bridge_human,
        ),
        config=config,
        runtime_paths=runtime_paths,
    )

    assert content[ORIGINAL_SENDER_KEY] == bridge_human
    assert content[SOURCE_KIND_KEY] == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert (
        validator.requester_user_id(
            sender=ids["router"].full_id,
            source={"content": content},
        )
        == canonical_human
    )
    assert (
        validator.requester_user_id(
            sender="@untrusted:localhost",
            source={"content": content},
        )
        == "@untrusted:localhost"
    )
    agent_id = ids["test_agent"]
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$spawn",
            "sender": agent_id.full_id,
            "origin_server_ts": 1234567890,
            "content": {
                "msgtype": "m.text",
                "body": f"{agent_id.full_id} do work",
                "m.mentions": {"user_ids": [agent_id.full_id]},
                ORIGINAL_SENDER_KEY: bridge_human,
                SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            },
        },
    )
    room = nio.MatrixRoom("!room:localhost", agent_id.full_id)

    assert await validator.precheck_event(room, event) == canonical_human
    ingress_metadata = DispatchIngressMetadata(source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND)
    router_event = nio.RoomMessageText.from_dict(
        {
            "event_id": "$router-relay",
            "sender": ids["router"].full_id,
            "origin_server_ts": 1234567890,
            "content": {"msgtype": "m.text", "body": "relay"},
        },
    )
    assert validator.should_use_trusted_router_relay_context(
        router_event,
        ingress_metadata=ingress_metadata,
        payload_metadata=DispatchPayloadMetadata(original_sender=bridge_human),
    )

    for non_human_sender in non_human_senders:
        assert validator.requester_user_id(sender=non_human_sender, source=None) == non_human_sender

    self_echo = nio.RoomMessageText.from_dict(
        {
            "event_id": "$self-echo",
            "sender": agent_id.full_id,
            "origin_server_ts": 1234567890,
            "content": {"msgtype": "m.text", "body": "self echo"},
        },
    )
    assert await validator.precheck_event(room, self_echo) is None

    for non_human_sender in ("@bridge_bot:localhost", internal_user_id):
        assert (
            validator.requester_user_id(
                sender=agent_id.full_id,
                source={
                    "content": {
                        ORIGINAL_SENDER_KEY: non_human_sender,
                        SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
                    },
                },
            )
            == agent_id.full_id
        )
        assert not validator.should_use_trusted_router_relay_context(
            router_event,
            ingress_metadata=ingress_metadata,
            payload_metadata=DispatchPayloadMetadata(original_sender=non_human_sender),
        )
