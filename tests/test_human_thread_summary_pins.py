"""Human-authored summary pins share the manual tool's responder authorization."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.authorization import ReplyMembershipPendingError
from mindroom.config.agent import AgentConfig
from mindroom.constants import THREAD_SUMMARY_CONTENT_KEY
from mindroom.entity_resolution import current_internal_sender_ids, entity_identity_registry
from mindroom.matrix.conversation_reads import DeliveredResponse
from mindroom.matrix.room_membership import ensure_room_membership_synced
from mindroom.thread_summary.automatic import maybe_generate_thread_summary
from mindroom.thread_summary.notices import ThreadSummaryWriteError, set_manual_thread_summary
from mindroom.thread_summary.state import SummaryState, current_thread_summary_from_history, human_summary_authorizer
from tests.access_schema_support import membership_config, membership_index
from tests.conftest import (
    fresh_thread_summary_baselines,
    install_model_providers,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    runtime_paths_for,
    serve_conversation_reader,
)
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.identity_helpers import persist_entity_accounts
from tests.test_thread_summary import _make_summary_notice_message, _make_thread_history, serve_source_thread

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path
    from unittest.mock import AsyncMock

    from mindroom.config.main import Config
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.thread_summary.schedule import _ThreadSummaryBaselines

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization", "thread_summary_baselines")


def _config(
    tmp_path: Path,
    *,
    rooms: Mapping[str, Mapping[str, object]] | None = None,
    agent_rooms: Sequence[str] = (),
    access: Mapping[str, object] | None = None,
) -> Config:
    """Build a membership config whose automatic summaries are due from a thread's first message, as shipped."""
    config = membership_config(tmp_path, rooms=rooms, agent_rooms=agent_rooms, access=access)
    config.defaults.thread_summary_first_threshold = 1
    return config


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


def _agent(config: Config) -> str:
    return next(iter(current_internal_sender_ids(config, runtime_paths_for(config))))


def _state(config: Config, history: Sequence[ResolvedVisibleMessage]) -> SummaryState:
    """Classify one history under the config's trusted senders and the responder's human pin authority."""
    runtime_paths = runtime_paths_for(config)
    return SummaryState.from_history(
        history,
        thread_id="$thread1",
        trusted_sender_ids=current_internal_sender_ids(config, runtime_paths),
        authorize_human=human_summary_authorizer(
            make_matrix_client_mock(),
            "!room:x",
            config,
            runtime_paths,
            "talent",
            AgentReplyMembershipIndex(),
        ),
    )


async def _run_automatic_summary(
    config: Config,
    history: list[ResolvedVisibleMessage],
    source_history: list[ResolvedVisibleMessage],
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: AsyncMock | None = None,
    entity_name: str = "talent",
    membership: AgentReplyMembershipIndex | None = None,
) -> tuple[AsyncMock, ScriptedModel]:
    """Exercise real pin, authorization, vocabulary, and tag reads with empty room state."""
    if client is None:
        client = make_matrix_client_mock()
    client.room_get_state.return_value = nio.RoomGetStateResponse(events=[], room_id="!room:x")
    client.room_send.return_value = nio.RoomSendResponse(event_id="$automatic", room_id="!room:x")
    serve_source_thread(client, source_history)
    model = ScriptedModel([answer('{"summary": "Automatic"}')])
    install_model_providers(monkeypatch, {"default": model})
    reader = make_conversation_reader_mock()
    serve_conversation_reader(reader, history, room_id="!room:x", thread_id="$thread1")
    await maybe_generate_thread_summary(
        client,
        "!room:x",
        "$thread1",
        config,
        runtime_paths_for(config),
        conversation_reader=reader,
        delivered_response=DeliveredResponse(event_id="$event0", body="Message 0"),
        entity_name=entity_name,
        membership_index=membership or AgentReplyMembershipIndex(),
    )
    return client, model


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", ["@owner:example.com", "@bridge-owner:example.com"])
async def test_authorized_human_pin_survives_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sender: str,
) -> None:
    """Recovered human pins stop generation without trusting forged counters."""
    config = _config(tmp_path, access={"users": ["@owner:example.com"]})
    config.authorization.aliases = {"@owner:example.com": ["@bridge-owner:example.com"]}
    history = [*_make_thread_history(12), _human_notice(sender)]
    for _ in range(2):
        restarted = fresh_thread_summary_baselines(monkeypatch)
        client, model = await _run_automatic_summary(config, history, [], monkeypatch)
        assert not model.requests
        client.room_send.assert_not_awaited()
        assert restarted.count("!room:x", "$thread1") == 12
    state = _state(config, history)
    assert state.pinned
    assert state.last_summary_count == 0
    assert not state.initial_enrichment_complete


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [True, False])
@pytest.mark.parametrize("pin_source", ["history", "source"])
@pytest.mark.parametrize("configured_room", [False, True])
async def test_human_pin_uses_responder_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authorized: bool,
    pin_source: str,
    configured_room: bool,
) -> None:
    """Source recheck honors allowed users and rejects spoofed unauthorized notices."""
    config = _config(
        tmp_path,
        agent_rooms=["!room:x"] if configured_room else [],
        access={"users": ["@owner:example.com"]},
    )
    client = make_matrix_client_mock()
    client.rooms["!room:x"].members_synced = not configured_room
    notice = _human_notice("@owner:example.com" if authorized else "@outsider:example.com")
    notice.content["io.mindroom.original_sender"] = "@owner:example.com"
    history = _make_thread_history(12)
    if pin_source == "history":
        history.append(notice)
    client, model = await _run_automatic_summary(config, history, [notice], monkeypatch, client=client)
    assert client.room_send.await_count == (0 if authorized else 1)
    assert len(model.requests) == (0 if authorized and pin_source == "history" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("pin_source", ["history", "source"])
@pytest.mark.parametrize("edited_pin", [False, True])
async def test_later_human_pin_overrides_unseen_future_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pin_source: str,
    edited_pin: bool,
) -> None:
    """A cold client's pin wins even when it missed an older future-dated release."""
    config = _config(tmp_path, access={"users": ["@owner:example.com"]})
    release = _make_summary_notice_message(
        "$thread1",
        message_count=1,
        event_id="$old-release",
        sender=_agent(config),
        pinned=False,
        generated_at="2099-01-01T00:00:00+00:00",
    )
    release.timestamp = 1000
    notice = _human_notice()
    notice.timestamp = 500 if edited_pin else 2000
    if edited_pin:
        notice.edited_timestamp = 2000
        notice.latest_event_id = "$pin-edit"
    history = [*_make_thread_history(12), release]
    if pin_source == "history":
        history.append(notice)
    for _ in range(2):
        fresh_thread_summary_baselines(monkeypatch)
        client, model = await _run_automatic_summary(config, history, [notice, release], monkeypatch)
        client.room_send.assert_not_awaited()
        assert len(model.requests) == (0 if pin_source == "history" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("edited_release", [False, True])
async def test_later_agent_release_overrides_future_human_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edited_release: bool,
) -> None:
    """The same event chronology lets an agent intentionally release a skewed human pin."""
    config = _config(tmp_path, access={"users": ["@owner:example.com"]})
    future = datetime(2099, 1, 1, tzinfo=UTC)
    notice = _human_notice(generated_at=future.isoformat())
    notice.timestamp = 1000
    release = _make_summary_notice_message(
        "$thread1",
        message_count=1,
        event_id="$later-release",
        sender=_agent(config),
        pinned=False,
    )
    release.timestamp = 500 if edited_release else 2000
    if edited_release:
        release.edited_timestamp = 2000
        release.latest_event_id = "$release-edit"
    history = [*_make_thread_history(12), notice, release]
    client, _ = await _run_automatic_summary(config, history, [release, notice], monkeypatch)
    client.room_send.assert_awaited_once()
    sent = client.room_send.call_args.kwargs["content"][THREAD_SUMMARY_CONTENT_KEY]
    assert datetime.fromisoformat(sent["generated_at"]) > future


@pytest.mark.asyncio
async def test_membership_grant_authorizes_human_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A proven responder membership grant authorizes the same pin as a static user grant."""
    config = _config(
        tmp_path,
        rooms={"team": {}},
        agent_rooms=["team"],
        access={"members_of_rooms": ["team"]},
    )
    memberships = await membership_index(config, {"team": {"@owner:example.com"}})
    _, model = await _run_automatic_summary(
        config,
        [*_make_thread_history(12), _human_notice()],
        [],
        monkeypatch,
        membership=memberships,
    )
    assert not model.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [True, False])
async def test_explicit_release_advances_past_authorized_future_pin(tmp_path: Path, authorized: bool) -> None:
    """Explicit tool writes beat skewed allowed predecessors, never outsider timestamps."""
    config = _config(tmp_path, agent_rooms=["!room:x"], access={"users": ["@owner:example.com"]})
    client = make_matrix_client_mock()
    client.room_send.return_value = nio.RoomSendResponse(event_id="$release", room_id="!room:x")
    reader = make_conversation_reader_mock()
    future = datetime(2099, 1, 1, tzinfo=UTC)
    notice = _human_notice(
        "@owner:example.com" if authorized else "@outsider:example.com",
        generated_at=future.isoformat(),
    )
    serve_conversation_reader(reader, [*_make_thread_history(3), notice], thread_id="$thread1")
    result = await set_manual_thread_summary(
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
    sent = client.room_send.call_args.kwargs["content"][THREAD_SUMMARY_CONTENT_KEY]
    assert sent["pinned"] is False
    assert (datetime.fromisoformat(sent["generated_at"]) > future) is authorized
    # An authorized pin is a summary, not a message; an outsider's look-alike is an ordinary message.
    assert result.message_count == sent["message_count"] == (3 if authorized else 4)


@pytest.mark.asyncio
async def test_pin_authorized_for_another_room_responder_stops_automatic_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared room titles honor an allowed editor even when another responder runs."""
    config = _config(tmp_path, access={"users": ["@owner:example.com"]})
    config.agents["talent"].rooms = ["!room:x"]
    config.agents["other"] = AgentConfig(display_name="Other", rooms=["!room:x"], access={"users": []})
    persist_entity_accounts(config, runtime_paths_for(config))
    _, model = await _run_automatic_summary(
        config,
        [*_make_thread_history(12), _human_notice()],
        [],
        monkeypatch,
        entity_name="other",
    )
    assert not model.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("pin_source", ["history", "source"])
async def test_missing_room_cache_defers_shared_pin_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    thread_summary_baselines: _ThreadSummaryBaselines,
    pin_source: str,
) -> None:
    """Incomplete shared-responder discovery cannot turn an allowed pin into a denial."""
    config = _config(tmp_path, access={"users": ["@owner:example.com"]})
    config.agents["talent"].rooms = ["!room:x"]
    config.agents["other"] = AgentConfig(display_name="Other", rooms=["!room:x"], access={"users": []})
    runtime_paths = runtime_paths_for(config)
    persist_entity_accounts(config, runtime_paths)
    client = make_matrix_client_mock()
    client.rooms = {}
    notice = _human_notice()
    history = _make_thread_history(12)
    if pin_source == "history":
        history.append(notice)

    client, model = await _run_automatic_summary(
        config,
        history,
        [notice],
        monkeypatch,
        client=client,
        entity_name="other",
    )

    client.room_send.assert_not_awaited()
    if pin_source == "history":
        assert not model.requests
        assert thread_summary_baselines.count("!room:x", "$thread1") == 0
    else:
        # The model ran before the re-check met the pending pin, so the attempt still counts.
        assert len(model.requests) == 1
        assert thread_summary_baselines.count("!room:x", "$thread1") == 12

    client.rooms["!room:x"] = nio.MatrixRoom("!room:x", client.user_id)
    assert human_summary_authorizer(
        client,
        "!room:x",
        config,
        runtime_paths,
        "other",
        AgentReplyMembershipIndex(),
    )(notice.sender)


@pytest.mark.asyncio
@pytest.mark.parametrize("pin_source", ["history", "source"])
async def test_partial_ad_hoc_room_cache_defers_pin_until_membership_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    thread_summary_baselines: _ThreadSummaryBaselines,
    pin_source: str,
) -> None:
    """An unseen joined responder cannot turn a valid human pin into a denial."""
    config = _config(tmp_path, access={"users": ["@owner:example.com"]})
    config.agents["other"] = AgentConfig(display_name="Other", access={"users": []})
    runtime_paths = runtime_paths_for(config)
    persist_entity_accounts(config, runtime_paths)
    registry = entity_identity_registry(config, runtime_paths)
    client = make_matrix_client_mock(user_id=registry.current_id("other").full_id)
    room = nio.MatrixRoom("!room:x", client.user_id)
    room.add_member(client.user_id, None, None)
    client.rooms = {room.room_id: room}
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[
            nio.RoomMember(client.user_id, None, None),
            nio.RoomMember("@owner:example.com", None, None),
            nio.RoomMember(registry.current_id("talent").full_id, None, None),
        ],
        room_id=room.room_id,
    )
    notice = _human_notice()
    history = _make_thread_history(12)
    if pin_source == "history":
        history.append(notice)

    client, model = await _run_automatic_summary(
        config,
        history,
        [notice],
        monkeypatch,
        client=client,
        entity_name="other",
    )
    client.room_send.assert_not_awaited()
    assert len(model.requests) == (0 if pin_source == "history" else 1)
    assert thread_summary_baselines.count("!room:x", "$thread1") == (0 if pin_source == "history" else 12)

    assert await ensure_room_membership_synced(client, room, sender_id=notice.sender)
    restarted = fresh_thread_summary_baselines(monkeypatch)
    client, model = await _run_automatic_summary(
        config,
        [*_make_thread_history(12), notice],
        [notice],
        monkeypatch,
        client=client,
        entity_name="other",
    )
    assert not model.requests
    client.room_send.assert_not_awaited()
    assert restarted.count("!room:x", "$thread1") == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_room", [False, True])
async def test_partial_room_cache_preserves_proven_shared_pin_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    thread_summary_baselines: _ThreadSummaryBaselines,
    configured_room: bool,
) -> None:
    """A known allowed responder proves a pin despite other uncached members."""
    config = _config(
        tmp_path,
        agent_rooms=["!room:x"] if configured_room else [],
        access={"users": ["@owner:example.com"]},
    )
    config.agents["other"] = AgentConfig(display_name="Other", access={"users": []})
    runtime_paths = runtime_paths_for(config)
    persist_entity_accounts(config, runtime_paths)
    registry = entity_identity_registry(config, runtime_paths)
    client = make_matrix_client_mock(user_id=registry.current_id("other").full_id)
    room = nio.MatrixRoom("!room:x", client.user_id)
    room.add_member(client.user_id, None, None)
    if not configured_room:
        room.add_member(registry.current_id("talent").full_id, None, None)
    client.rooms = {room.room_id: room}
    notice = _human_notice()

    client, model = await _run_automatic_summary(
        config,
        [*_make_thread_history(12), notice],
        [notice],
        monkeypatch,
        client=client,
        entity_name="other",
    )

    assert not model.requests
    client.room_send.assert_not_awaited()
    assert thread_summary_baselines.count("!room:x", "$thread1") == 12


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
    """Only the supported explicit manual-pin payload changes automatic behavior; anything else is a message."""
    config = _config(tmp_path, access={"users": ["@owner:example.com"]})
    notice = _human_notice()
    notice.content[THREAD_SUMMARY_CONTENT_KEY][field] = value

    state = _state(config, [notice])

    assert not state.pinned
    assert state.summary is None
    assert state.ordinary_messages == (notice,)


@pytest.mark.asyncio
async def test_pending_membership_defers_generation_without_advancing_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    thread_summary_baselines: _ThreadSummaryBaselines,
) -> None:
    """Cold authorization state must not overwrite a pin before membership recovers."""
    config = _config(tmp_path, rooms={"team": {}}, agent_rooms=["team"])
    _, model = await _run_automatic_summary(config, [*_make_thread_history(12), _human_notice()], [], monkeypatch)
    assert not model.requests
    assert thread_summary_baselines.count("!room:x", "$thread1") == 0


def test_pending_membership_leaves_the_thread_state_unknown(tmp_path: Path) -> None:
    """A person's pin whose authority is not known yet makes the whole state unknown rather than guessed."""
    config = _config(tmp_path, rooms={"team": {}}, agent_rooms=["team"])
    runtime_paths = runtime_paths_for(config)
    history = [*_make_thread_history(3), _human_notice()]

    with pytest.raises(ReplyMembershipPendingError):
        _state(config, history)
    assert (
        current_thread_summary_from_history(
            make_matrix_client_mock(),
            "!room:x",
            "$thread1",
            history,
            config=config,
            runtime_paths=runtime_paths,
            entity_name="talent",
            membership_index=AgentReplyMembershipIndex(),
            trusted_sender_ids=current_internal_sender_ids(config, runtime_paths),
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_membership", [False, True])
async def test_manual_write_reports_unorderable_predecessor(
    tmp_path: Path,
    thread_summary_baselines: _ThreadSummaryBaselines,
    pending_membership: bool,
) -> None:
    """Unresolved authority and exhausted timestamps fail as controlled tool errors."""
    config = _config(
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
    assert thread_summary_baselines.count("!room:x", "$thread1") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("release_source", ["history", "source"])
async def test_automatic_summary_advances_past_future_explicit_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_source: str,
) -> None:
    """An explicit release restores visible automatic updates despite clock skew."""
    config = _config(tmp_path, access={"users": ["@owner:example.com"]})
    future = datetime(2099, 1, 1, tzinfo=UTC)
    release = _make_summary_notice_message(
        "$thread1",
        message_count=1,
        sender=_agent(config),
        pinned=False,
        generated_at=future.isoformat(),
    )
    history = _make_thread_history(12)
    if release_source == "history":
        history.append(release)
    client, _ = await _run_automatic_summary(config, history, [release], monkeypatch)
    sent = client.room_send.call_args.kwargs["content"][THREAD_SUMMARY_CONTENT_KEY]
    assert datetime.fromisoformat(sent["generated_at"]) > future


@pytest.mark.parametrize("generated_at", ["9999-12-31T23:00:00-02:00", "0001-01-01T01:00:00+02:00"])
def test_out_of_range_utc_human_timestamp_cannot_pin(tmp_path: Path, generated_at: str) -> None:
    """ISO offsets cannot smuggle unsupported UTC dates into persistent pin state."""
    config = _config(tmp_path, access={"users": ["@owner:example.com"]})
    assert not _state(config, [_human_notice(generated_at=generated_at)]).pinned


def test_newest_of_two_racing_pins_titles_the_thread(tmp_path: Path) -> None:
    """Two people pinning at once leave the later event's title, whatever their clocks wrote."""
    config = _config(tmp_path, access={"users": ["@owner:example.com", "@editor:example.com"]})
    first = _human_notice(generated_at="2026-01-01T00:05:00+00:00")
    first.timestamp = 1000
    second = _human_notice("@editor:example.com", generated_at="2026-01-01T00:00:00+00:00")
    second.timestamp = 2000
    for pin, title in ((first, "First title"), (second, "Second title")):
        pin.body = title
        pin.content["body"] = title
        pin.content[THREAD_SUMMARY_CONTENT_KEY]["summary"] = title

    state = _state(config, [*_make_thread_history(3), second, first])

    assert state.summary == "Second title"
    assert state.pinned
    assert state.message_count == 3
    assert state.newest_generated_at == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)


def test_equal_event_times_order_a_pin_and_a_release_by_their_summary_clock(tmp_path: Path) -> None:
    """A pin and a release in the same millisecond resolve by ``generated_at``, then by position."""
    config = _config(tmp_path, access={"users": ["@owner:example.com"]})
    pin = _human_notice(generated_at="2026-01-01T00:05:00+00:00")
    release = _make_summary_notice_message(
        "$thread1",
        message_count=3,
        event_id="$release",
        sender=_agent(config),
        pinned=False,
        generated_at="2026-01-01T00:00:00+00:00",
    )
    pin.timestamp = release.timestamp = 1000

    assert _state(config, [pin, release]).pinned
    release.content[THREAD_SUMMARY_CONTENT_KEY]["generated_at"] = "2026-01-01T00:05:00+00:00"
    assert not _state(config, [pin, release]).pinned
    assert _state(config, [release, pin]).pinned


@pytest.mark.asyncio
async def test_a_released_human_pin_is_neither_a_message_nor_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once an agent releases a person's pinned summary, the next summary neither counts nor reads that pin."""
    config = _config(tmp_path, access={"users": ["@owner:example.com"]})
    config.defaults.thread_summary_subsequent_interval = 1
    pin = _human_notice()
    pin.timestamp = 1000
    # One message has arrived since the agent released the pin, so the next summary is due at three messages.
    release = _make_summary_notice_message(
        "$thread1",
        message_count=2,
        event_id="$release",
        sender=_agent(config),
        pinned=False,
    )
    release.timestamp = 2000
    history = [*_make_thread_history(3), pin, release]

    client, model = await _run_automatic_summary(config, history, history, monkeypatch)

    (request,) = model.requests
    assert "@owner:example.com" not in json.dumps([message.content for message in request.messages], default=str)
    sent = client.room_send.call_args.kwargs["content"][THREAD_SUMMARY_CONTENT_KEY]
    assert sent["message_count"] == 3
