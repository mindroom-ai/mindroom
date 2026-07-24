"""Focused tests for requester identity resolution at ingress."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.main import Config
from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.handled_turns import TurnRecord
from mindroom.ingress_validation import IngressValidator, IngressValidatorDeps
from mindroom.matrix import stale_stream_cleanup
from mindroom.turn_store import TurnStore, TurnStoreDeps
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.turn_policy import TurnPolicy


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


def _real_turn_store(tmp_path: Path) -> TurnStore:
    return TurnStore(
        TurnStoreDeps(
            agent_name="test_agent",
            tracking_base_path=tmp_path,
            state_writer=MagicMock(),
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )


def _prepared_text_event(sender: str, event_id: str) -> PreparedTextEvent:
    return PreparedTextEvent(
        sender=sender,
        event_id=event_id,
        body="hello @test_agent",
        source={"content": {"body": "hello @test_agent", "msgtype": "m.text"}},
    )


def test_precheck_drops_replayed_delivery_of_in_flight_claimed_event(tmp_path: Path) -> None:
    """A duplicate sync delivery of an event whose first delivery is still being answered is dropped.

    This is the deterministic owning-seam reproduction of the live-fuzz starvation: without the
    in-flight guard the duplicate re-enters coalescing, folds into a follow-up batch, and makes
    that batch's all-or-nothing turn claim collide so innocent co-batched sources are dropped.
    """
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
    turn_store = _real_turn_store(tmp_path)
    validator = IngressValidator(
        IngressValidatorDeps(
            runtime=runtime,
            runtime_paths=runtime_paths,
            matrix_id=ids["test_agent"],
            turn_store=turn_store,
            turn_policy=cast("TurnPolicy", SimpleNamespace(can_reply_to_sender=lambda _sender: True)),
        ),
    )
    room = cast("nio.MatrixRoom", SimpleNamespace(room_id="!room:localhost"))
    human = "@human:localhost"
    event = _prepared_text_event(human, "$first_delivery")

    # First delivery: authorized, not yet claimed -> resolves to the requester.
    assert validator.precheck_event(room, event) == human

    # The first delivery's turn is now claimed and actively streaming a response.
    assert turn_store.try_claim_turn(TurnRecord.create([event.event_id], completed=False)) is True

    # Second (replayed) sync delivery of the SAME event is dropped idempotently.
    assert validator.precheck_event(room, event) is None

    # An edit of the same event must still pass (edits legitimately re-enter the pipeline).
    assert validator.precheck_event(room, event, is_edit=True) == human

    # A genuinely different event is unaffected by the in-flight guard.
    other = _prepared_text_event(human, "$other_delivery")
    assert validator.precheck_event(room, other) == human

    # Once the first delivery's claim is released, replays are allowed again.
    turn_store.release_pending_turn_claim(TurnRecord.create([event.event_id], completed=False))
    assert validator.precheck_event(room, event) == human
