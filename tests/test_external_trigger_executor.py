"""Tests for external trigger Matrix dispatch execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import (
    EXTERNAL_TRIGGER_SOURCE_KIND,
    is_automation_source_kind,
    source_kind_allows_trusted_original_sender,
    source_kind_bypasses_coalescing,
    source_kind_from_content,
)
from mindroom.entity_resolution import entity_identity_registry
from mindroom.external_triggers.executor import _build_external_trigger_text, execute_external_trigger
from mindroom.external_triggers.models import ExternalTriggerPayload
from mindroom.external_triggers.store import ExternalTriggerTarget, TriggerDeliverySnapshot
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.state import MatrixState
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={
                "research": AgentConfig(display_name="Research"),
                "ops": AgentConfig(display_name="Ops"),
            },
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        test_runtime_paths(tmp_path),
    )


def _snapshot(
    *,
    room_id: str = "!fixed:localhost",
    resolved_room_id: str | None = None,
    new_thread: bool = False,
    thread_id: str | None = "$thread-root",
) -> TriggerDeliverySnapshot:
    return TriggerDeliverySnapshot(
        trigger_id="campground",
        uid="uid",
        version=1,
        auth_epoch=1,
        config_generation=7,
        enabled=True,
        description="Campground",
        owner_user_id="@owner:localhost",
        created_by_agent_name="research",
        created_in_room_id="!fixed:localhost",
        target=ExternalTriggerTarget(
            room_id=room_id,
            thread_id=thread_id,
            agent="research",
            new_thread=new_thread,
        ),
        resolved_room_id=resolved_room_id or room_id,
        auth="ed25519",
        key_id="default",
        public_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        public_key_fingerprint="sha256:test",
        allowed_kinds=("campground.availability",),
        replay_window_seconds=300,
        max_body_bytes=65536,
        replay_scope="uid:1",
    )


def _payload() -> ExternalTriggerPayload:
    return ExternalTriggerPayload(
        kind="campground.availability",
        title="Campground opened",
        message="Site 42 is available.",
        event_id="availability-42",
        data={"site": "42", "nested": {"z": 1, "a": 2}},
    )


def _payload_with_payload_controlled_mentions() -> ExternalTriggerPayload:
    return ExternalTriggerPayload(
        kind="campground.availability",
        title="@ops campground opened",
        message="Site 42 is available. Notify @ops.",
        event_id="availability-42",
        data={"assignee": "@ops", "nested": {"watcher": "@ops"}},
    )


def _conversation_cache(*, latest_thread_event_id: str | None = "$latest") -> AsyncMock:
    access = AsyncMock()
    access.get_latest_thread_event_id_if_needed.return_value = latest_thread_event_id
    return access


def test_build_external_trigger_message_text_mentions_agent_and_includes_payload_details() -> None:
    """External trigger text is built from configured target plus signed payload fields only."""
    text = _build_external_trigger_text("@research", _payload())

    assert text == (
        "@research Campground opened\n\n"
        "Site 42 is available.\n\n"
        "```json\n"
        "{\n"
        '  "nested": {\n'
        '    "a": 2,\n'
        '    "z": 1\n'
        "  },\n"
        '  "site": "42"\n'
        "}\n"
        "```"
    )
    assert "!fixed:localhost" not in text


def test_build_external_trigger_message_text_preserves_payload_mentions() -> None:
    """Signed payload text should stay visible exactly as supplied."""
    text = _build_external_trigger_text("@research", _payload_with_payload_controlled_mentions())

    assert "@ops campground opened" in text
    assert "Notify @ops." in text
    assert '"assignee": "@ops"' in text
    assert "(at)ops" not in text


def test_dispatch_source_recognizes_external_trigger_as_automation_and_trusted_source() -> None:
    """External trigger messages use automation semantics and trusted original-source metadata."""
    content = {SOURCE_KIND_KEY: EXTERNAL_TRIGGER_SOURCE_KIND}

    assert is_automation_source_kind(EXTERNAL_TRIGGER_SOURCE_KIND)
    assert source_kind_bypasses_coalescing(EXTERNAL_TRIGGER_SOURCE_KIND)
    assert source_kind_allows_trusted_original_sender(EXTERNAL_TRIGGER_SOURCE_KIND)
    assert source_kind_from_content(content) == EXTERNAL_TRIGGER_SOURCE_KIND


@pytest.mark.asyncio
async def test_execute_external_trigger_sends_to_fixed_thread_target_with_source_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Executor uses configured room/thread target and returns the Matrix delivery event ID."""
    config = _config(tmp_path)
    conversation_cache = _conversation_cache(latest_thread_event_id="$latest")
    send_and_track_message = AsyncMock(
        return_value=DeliveredMatrixEvent(event_id="$matrix-event", content_sent={}),
    )
    monkeypatch.setattr("mindroom.external_triggers.executor.send_and_track_message", send_and_track_message)

    event_id = await execute_external_trigger(
        client=AsyncMock(),
        snapshot=_snapshot(),
        payload=_payload(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=conversation_cache,
    )

    assert event_id == "$matrix-event"
    conversation_cache.get_latest_thread_event_id_if_needed.assert_awaited_once_with(
        "!fixed:localhost",
        "$thread-root",
        caller_label="external_trigger",
    )
    send_and_track_message.assert_awaited_once()
    _client, room_id, content, sent_config, sent_cache = send_and_track_message.await_args.args
    assert room_id == "!fixed:localhost"
    assert sent_config is config
    assert sent_cache is conversation_cache
    assert content["m.relates_to"]["event_id"] == "$thread-root"
    assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$latest"
    assert content[SOURCE_KIND_KEY] == EXTERNAL_TRIGGER_SOURCE_KIND
    assert content[ORIGINAL_SENDER_KEY] == "@owner:localhost"
    assert content["io.mindroom.external_trigger.id"] == "campground"
    assert content["io.mindroom.external_trigger.kind"] == "campground.availability"
    assert content["io.mindroom.external_trigger.event_id"] == "availability-42"


@pytest.mark.asyncio
async def test_execute_external_trigger_resolves_configured_room_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Executor resolves authored room keys before thread lookup and Matrix send."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    state = MatrixState.load(runtime_paths)
    state.add_room("lobby", "!resolved:localhost", "#lobby:localhost", "Lobby")
    state.save(runtime_paths)
    conversation_cache = _conversation_cache(latest_thread_event_id="$latest")
    send_and_track_message = AsyncMock(
        return_value=DeliveredMatrixEvent(event_id="$matrix-event", content_sent={}),
    )
    monkeypatch.setattr("mindroom.external_triggers.executor.send_and_track_message", send_and_track_message)

    await execute_external_trigger(
        client=AsyncMock(),
        snapshot=_snapshot(room_id="lobby", resolved_room_id="!resolved:localhost"),
        payload=_payload(),
        config=config,
        runtime_paths=runtime_paths,
        conversation_cache=conversation_cache,
    )

    conversation_cache.get_latest_thread_event_id_if_needed.assert_awaited_once_with(
        "!resolved:localhost",
        "$thread-root",
        caller_label="external_trigger",
    )
    assert send_and_track_message.await_args.args[1] == "!resolved:localhost"


@pytest.mark.asyncio
async def test_execute_external_trigger_only_parses_configured_target_mention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Payload-controlled @ text must not wake agents other than the configured target."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    registry = entity_identity_registry(config, runtime_paths)
    send_and_track_message = AsyncMock(
        return_value=DeliveredMatrixEvent(event_id="$matrix-event", content_sent={}),
    )
    monkeypatch.setattr("mindroom.external_triggers.executor.send_and_track_message", send_and_track_message)

    await execute_external_trigger(
        client=AsyncMock(),
        snapshot=_snapshot(),
        payload=_payload_with_payload_controlled_mentions(),
        config=config,
        runtime_paths=runtime_paths,
        conversation_cache=_conversation_cache(),
    )

    content: dict[str, Any] = send_and_track_message.await_args.args[2]
    mentioned_user_ids = content["m.mentions"]["user_ids"]
    assert mentioned_user_ids == [registry.current_id("research").full_id]
    assert registry.current_id("ops").full_id not in mentioned_user_ids
    assert "@ops" in content["body"]
    assert "@ops" in content["formatted_body"]
    assert "(at)ops" not in content["body"]
    assert "(at)ops" not in content["formatted_body"]


@pytest.mark.asyncio
async def test_execute_external_trigger_new_thread_sends_room_message_without_thread_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """new_thread external triggers ignore configured thread_id and avoid thread lookup."""
    config = _config(tmp_path)
    conversation_cache = _conversation_cache()
    send_and_track_message = AsyncMock(
        return_value=DeliveredMatrixEvent(event_id="$new-thread-event", content_sent={}),
    )
    monkeypatch.setattr("mindroom.external_triggers.executor.send_and_track_message", send_and_track_message)

    event_id = await execute_external_trigger(
        client=AsyncMock(),
        snapshot=_snapshot(new_thread=True),
        payload=_payload(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=conversation_cache,
    )

    assert event_id == "$new-thread-event"
    conversation_cache.get_latest_thread_event_id_if_needed.assert_not_awaited()
    send_and_track_message.assert_awaited_once()
    content: dict[str, Any] = send_and_track_message.await_args.args[2]
    assert "m.relates_to" not in content
    assert content[SOURCE_KIND_KEY] == EXTERNAL_TRIGGER_SOURCE_KIND
