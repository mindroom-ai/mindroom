"""Human-authored summary pins share the manual tool's responder authorization."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.agent import AgentConfig
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.matrix.conversation_reads import DeliveredResponse
from mindroom.thread_summary import (
    ThreadSummaryWriteError,
    _human_summary_authorizer,
    _last_summary_counts,
    _recover_initial_enrichment_complete,
    _recover_last_summary_count,
    _recover_pin_state,
    _thread_locks,
    maybe_generate_thread_summary,
    set_manual_thread_summary,
)
from tests.access_schema_support import membership_config, membership_index
from tests.conftest import (
    make_conversation_reader_mock,
    make_matrix_client_mock,
    runtime_paths_for,
    serve_conversation_reader,
)
from tests.identity_helpers import persist_entity_accounts
from tests.test_thread_summary import _make_summary_notice_message, _make_thread_history

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


@pytest.fixture(autouse=True)
def _clear_summary_state() -> None:
    _last_summary_counts.clear()
    _thread_locks.clear()


def _human_notice(
    sender: str = "@owner:example.com",
    *,
    generated_at: str = "2026-01-01T00:00:00+00:00",
) -> ResolvedVisibleMessage:
    return _make_summary_notice_message(
        "$thread1",
        message_count=999999,
        sender=sender,
        pinned=True,
        generated_at=generated_at,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", ["@owner:example.com", "@bridge-owner:example.com"])
async def test_authorized_human_pin_survives_restart(tmp_path: Path, sender: str) -> None:
    """Recovered human pins stop generation without trusting forged counters."""
    config = membership_config(tmp_path, access={"users": ["@owner:example.com"]})
    config.authorization.aliases = {"@owner:example.com": ["@bridge-owner:example.com"]}
    runtime_paths = runtime_paths_for(config)
    history = [*_make_thread_history(12), _human_notice(sender)]
    client = make_matrix_client_mock()
    reader = make_conversation_reader_mock()
    serve_conversation_reader(reader, history, thread_id="$thread1")
    with (
        patch("mindroom.thread_summary.maybe_rebuild_tag_vocabulary", new=AsyncMock(return_value=None)),
        patch("mindroom.thread_summary._generate_summary", new=AsyncMock(return_value="Automatic")) as generate,
    ):
        for _ in range(2):
            _last_summary_counts.clear()
            await maybe_generate_thread_summary(
                client,
                "!room:x",
                "$thread1",
                config,
                runtime_paths,
                conversation_reader=reader,
                delivered_response=DeliveredResponse(event_id="$event0", body="Message 0"),
                entity_name="talent",
                membership_index=AgentReplyMembershipIndex(),
            )
        generate.assert_not_awaited()
        client.room_send.assert_not_awaited()
    trusted = current_internal_sender_ids(config, runtime_paths)
    assert _recover_last_summary_count(history, trusted_sender_ids=trusted) == 0
    assert not _recover_initial_enrichment_complete(history, trusted_sender_ids=trusted)
    assert _last_summary_counts["!room:x:$thread1"] < 999999


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [True, False])
@pytest.mark.parametrize("pin_source", ["history", "source"])
async def test_human_pin_uses_responder_authority(tmp_path: Path, authorized: bool, pin_source: str) -> None:
    """Source recheck honors allowed users and rejects spoofed unauthorized notices."""
    config = membership_config(tmp_path, access={"users": ["@owner:example.com"]})
    client = make_matrix_client_mock()
    client.room_send.return_value = nio.RoomSendResponse(event_id="$automatic", room_id="!room:x")
    reader = make_conversation_reader_mock()
    notice = _human_notice("@owner:example.com" if authorized else "@outsider:example.com")
    notice.content["io.mindroom.original_sender"] = "@owner:example.com"
    history = _make_thread_history(12)
    if pin_source == "history":
        history.append(notice)
    serve_conversation_reader(reader, history, thread_id="$thread1")
    with (
        patch("mindroom.thread_summary.maybe_rebuild_tag_vocabulary", new=AsyncMock(return_value=None)),
        patch("mindroom.thread_summary.get_thread_tags", new=AsyncMock(return_value=None)),
        patch("mindroom.thread_summary._generate_summary", new=AsyncMock(return_value="Automatic")),
        patch("mindroom.thread_summary.fetch_thread_messages_from_source", new=AsyncMock(return_value=[notice])),
    ):
        await maybe_generate_thread_summary(
            client,
            "!room:x",
            "$thread1",
            config,
            runtime_paths_for(config),
            conversation_reader=reader,
            delivered_response=DeliveredResponse(event_id="$event0", body="Message 0"),
            entity_name="talent",
            membership_index=AgentReplyMembershipIndex(),
        )
    assert client.room_send.await_count == (0 if authorized else 1)


@pytest.mark.asyncio
async def test_membership_grant_authorizes_human_pin(tmp_path: Path) -> None:
    """A proven responder membership grant authorizes the same pin as a static user grant."""
    config = membership_config(
        tmp_path,
        rooms={"team": {}},
        agent_rooms=["team"],
        access={"members_of_rooms": ["team"]},
    )
    memberships = await membership_index(config, {"team": {"@owner:example.com"}})
    reader = make_conversation_reader_mock()
    serve_conversation_reader(reader, [*_make_thread_history(12), _human_notice()], thread_id="$thread1")
    with (
        patch("mindroom.thread_summary.maybe_rebuild_tag_vocabulary", new=AsyncMock(return_value=None)),
        patch("mindroom.thread_summary._generate_summary", new=AsyncMock(return_value="Automatic")) as generate,
    ):
        await maybe_generate_thread_summary(
            make_matrix_client_mock(),
            "!room:x",
            "$thread1",
            config,
            runtime_paths_for(config),
            conversation_reader=reader,
            delivered_response=DeliveredResponse(event_id="$event0", body="Message 0"),
            entity_name="talent",
            membership_index=memberships,
        )
    generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [True, False])
async def test_explicit_release_advances_past_authorized_future_pin(tmp_path: Path, authorized: bool) -> None:
    """Explicit tool writes beat skewed allowed predecessors, never outsider timestamps."""
    config = membership_config(tmp_path, access={"users": ["@owner:example.com"]})
    client = make_matrix_client_mock()
    client.room_send.return_value = nio.RoomSendResponse(event_id="$release", room_id="!room:x")
    reader = make_conversation_reader_mock()
    future = datetime(2099, 1, 1, tzinfo=UTC)
    notice = _human_notice(
        "@owner:example.com" if authorized else "@outsider:example.com",
        generated_at=future.isoformat(),
    )
    serve_conversation_reader(reader, [*_make_thread_history(3), notice], thread_id="$thread1")
    await set_manual_thread_summary(
        client,
        "!room:x",
        "$thread1",
        "Intentional replacement",
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_reader=reader,
        entity_name="talent",
        membership_index=AgentReplyMembershipIndex(),
        pin=False,
    )
    sent = client.room_send.call_args.kwargs["content"]["io.mindroom.thread_summary"]
    assert sent["pinned"] is False
    assert (datetime.fromisoformat(sent["generated_at"]) > future) is authorized


@pytest.mark.asyncio
async def test_pin_authorized_for_another_room_responder_stops_automatic_updates(tmp_path: Path) -> None:
    """Shared room titles honor an allowed editor even when another responder runs."""
    config = membership_config(tmp_path, access={"users": ["@owner:example.com"]})
    config.agents["talent"].rooms = ["!room:x"]
    config.agents["other"] = AgentConfig(display_name="Other", rooms=["!room:x"], access={"users": []})
    runtime_paths = runtime_paths_for(config)
    persist_entity_accounts(config, runtime_paths)
    client = make_matrix_client_mock()
    reader = make_conversation_reader_mock()
    serve_conversation_reader(reader, [*_make_thread_history(12), _human_notice()], thread_id="$thread1")
    with (
        patch("mindroom.thread_summary.maybe_rebuild_tag_vocabulary", new=AsyncMock(return_value=None)),
        patch("mindroom.thread_summary._generate_summary", new=AsyncMock(return_value="Automatic")) as generate,
    ):
        await maybe_generate_thread_summary(
            client,
            "!room:x",
            "$thread1",
            config,
            runtime_paths,
            conversation_reader=reader,
            delivered_response=DeliveredResponse(event_id="$event0", body="Message 0"),
            entity_name="other",
            membership_index=AgentReplyMembershipIndex(),
        )
    generate.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", True),
        ("version", 2),
        ("model", "automatic"),
        ("pinned", False),
        ("pinned", "true"),
        ("summary", ""),
        ("summary", "different body"),
        ("summary", "x" * 301),
        ("generated_at", "invalid"),
    ],
)
def test_malformed_human_pin_does_not_stop_summaries(tmp_path: Path, field: str, value: object) -> None:
    """Only the supported explicit manual-pin payload changes automatic behavior."""
    config = membership_config(tmp_path, access={"users": ["@owner:example.com"]})
    runtime_paths = runtime_paths_for(config)
    notice = _human_notice()
    notice.content["io.mindroom.thread_summary"][field] = value
    assert not _recover_pin_state(
        [notice],
        trusted_sender_ids=current_internal_sender_ids(config, runtime_paths),
        human_sender_allowed=_human_summary_authorizer(
            make_matrix_client_mock(),
            "!room:x",
            config,
            runtime_paths,
            "talent",
            AgentReplyMembershipIndex(),
        ),
        thread_id="$thread1",
    )


@pytest.mark.asyncio
async def test_pending_membership_defers_generation_without_advancing_baseline(tmp_path: Path) -> None:
    """Cold authorization state must not overwrite a pin before membership recovers."""
    config = membership_config(tmp_path, rooms={"team": {}}, agent_rooms=["team"])
    reader = make_conversation_reader_mock()
    serve_conversation_reader(reader, [*_make_thread_history(12), _human_notice()], thread_id="$thread1")
    with (
        patch("mindroom.thread_summary.maybe_rebuild_tag_vocabulary", new=AsyncMock(return_value=None)),
        patch("mindroom.thread_summary._generate_summary", new=AsyncMock(return_value="Automatic")) as generate,
    ):
        await maybe_generate_thread_summary(
            make_matrix_client_mock(),
            "!room:x",
            "$thread1",
            config,
            runtime_paths_for(config),
            conversation_reader=reader,
            delivered_response=DeliveredResponse(event_id="$event0", body="Message 0"),
            entity_name="talent",
            membership_index=AgentReplyMembershipIndex(),
        )
    generate.assert_not_awaited()
    assert "!room:x:$thread1" not in _last_summary_counts


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_membership", [False, True])
async def test_manual_write_reports_unorderable_predecessor(tmp_path: Path, pending_membership: bool) -> None:
    """Unresolved authority and exhausted timestamps fail as controlled tool errors."""
    config = membership_config(
        tmp_path,
        rooms={"team": {}},
        agent_rooms=["team"],
        access=None if pending_membership else {"users": ["@owner:example.com"]},
    )
    client = make_matrix_client_mock()
    reader = make_conversation_reader_mock()
    notice = _human_notice(generated_at="9999-12-31T23:59:59.999999+00:00")
    serve_conversation_reader(reader, [notice], thread_id="$thread1")
    with pytest.raises(ThreadSummaryWriteError):
        await set_manual_thread_summary(
            client,
            "!room:x",
            "$thread1",
            "Replacement",
            config=config,
            runtime_paths=runtime_paths_for(config),
            conversation_reader=reader,
            entity_name="talent",
            membership_index=AgentReplyMembershipIndex(),
            pin=False,
        )
    client.room_send.assert_not_awaited()
    assert "!room:x:$thread1" not in _last_summary_counts


@pytest.mark.asyncio
@pytest.mark.parametrize("release_source", ["history", "source"])
async def test_automatic_summary_advances_past_future_explicit_release(tmp_path: Path, release_source: str) -> None:
    """An explicit release restores visible automatic updates despite clock skew."""
    config = membership_config(tmp_path, access={"users": ["@owner:example.com"]})
    runtime_paths = runtime_paths_for(config)
    trusted = current_internal_sender_ids(config, runtime_paths)
    future = datetime(2099, 1, 1, tzinfo=UTC)
    release = _make_summary_notice_message(
        "$thread1",
        message_count=1,
        sender=next(iter(trusted)),
        pinned=False,
        generated_at=future.isoformat(),
    )
    history = _make_thread_history(12)
    if release_source == "history":
        history.append(release)
    client = make_matrix_client_mock()
    client.room_send.return_value = nio.RoomSendResponse(event_id="$automatic", room_id="!room:x")
    reader = make_conversation_reader_mock()
    serve_conversation_reader(reader, history, thread_id="$thread1")
    with (
        patch("mindroom.thread_summary.maybe_rebuild_tag_vocabulary", new=AsyncMock(return_value=None)),
        patch("mindroom.thread_summary.get_thread_tags", new=AsyncMock(return_value=None)),
        patch("mindroom.thread_summary._generate_summary", new=AsyncMock(return_value="Automatic")),
        patch("mindroom.thread_summary.fetch_thread_messages_from_source", new=AsyncMock(return_value=[release])),
    ):
        await maybe_generate_thread_summary(
            client,
            "!room:x",
            "$thread1",
            config,
            runtime_paths,
            conversation_reader=reader,
            delivered_response=DeliveredResponse(event_id="$event0", body="Message 0"),
            entity_name="talent",
            membership_index=AgentReplyMembershipIndex(),
        )
    sent = client.room_send.call_args.kwargs["content"]["io.mindroom.thread_summary"]
    assert datetime.fromisoformat(sent["generated_at"]) > future


@pytest.mark.parametrize("generated_at", ["9999-12-31T23:00:00-02:00", "0001-01-01T01:00:00+02:00"])
def test_out_of_range_utc_human_timestamp_cannot_pin(tmp_path: Path, generated_at: str) -> None:
    """ISO offsets cannot smuggle unsupported UTC dates into persistent pin state."""
    config = membership_config(tmp_path, access={"users": ["@owner:example.com"]})
    runtime_paths = runtime_paths_for(config)
    assert not _recover_pin_state(
        [_human_notice(generated_at=generated_at)],
        trusted_sender_ids=current_internal_sender_ids(config, runtime_paths),
        human_sender_allowed=_human_summary_authorizer(
            make_matrix_client_mock(),
            "!room:x",
            config,
            runtime_paths,
            "talent",
            AgentReplyMembershipIndex(),
        ),
        thread_id="$thread1",
    )
