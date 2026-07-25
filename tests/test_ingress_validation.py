"""Focused tests for requester identity resolution at ingress."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import nio

from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.main import Config
from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.ingress_validation import IngressValidator, IngressValidatorDeps
from mindroom.matrix import stale_stream_cleanup
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path


def test_trusted_relay_resolves_requester_and_allows_self_authored_ingress(tmp_path: Path) -> None:
    """Trusted relays should preserve human requesters without trusting outsiders."""
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": {"display_name": "Test Agent"}},
            models={"default": {"provider": "test", "id": "test-model"}},
            authorization={"default_room_access": True},
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    turn_store = MagicMock()
    turn_store.is_handled.return_value = False
    turn_policy = MagicMock()
    turn_policy.can_reply_to_sender.return_value = True
    validator = IngressValidator(
        IngressValidatorDeps(
            runtime=runtime,
            runtime_paths=runtime_paths,
            matrix_id=ids["test_agent"],
            turn_store=turn_store,
            turn_policy=turn_policy,
        ),
    )
    human_sender = "@human:localhost"
    content = stale_stream_cleanup._build_auto_resume_content(
        stale_stream_cleanup._InterruptedThread(
            room_id="!room:localhost",
            thread_id="$thread",
            target_event_id="$target",
            partial_text="partial",
            agent_name="test_agent",
            original_sender_id=human_sender,
        ),
        config=config,
        runtime_paths=runtime_paths,
    )

    assert content[ORIGINAL_SENDER_KEY] == human_sender
    assert content[SOURCE_KIND_KEY] == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert (
        validator.requester_user_id(
            sender=ids["router"].full_id,
            source={"content": content},
        )
        == human_sender
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
                ORIGINAL_SENDER_KEY: human_sender,
                SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            },
        },
    )
    room = nio.MatrixRoom("!room:localhost", agent_id.full_id)

    assert validator.precheck_event(room, event) == human_sender
