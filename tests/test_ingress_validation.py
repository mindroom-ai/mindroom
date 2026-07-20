"""Focused tests for requester identity resolution at ingress."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

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

    from mindroom.turn_policy import TurnPolicy
    from mindroom.turn_store import TurnStore


def test_auto_resume_relay_resolves_requester_to_original_human(tmp_path: Path) -> None:
    """Router-authored auto-resume should preserve human requester without trusting outsiders."""
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
    runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=False,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )
    validator = IngressValidator(
        IngressValidatorDeps(
            runtime=runtime,
            runtime_paths=runtime_paths,
            matrix_id=ids["test_agent"],
            turn_store=cast("TurnStore", object()),
            turn_policy=cast("TurnPolicy", object()),
        ),
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
