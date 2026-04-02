"""Tests for metadata-driven partial reply context handling."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.main import Config
from mindroom.constants import (
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.execution_preparation import (
    _build_prompt_with_unseen,
    _classify_partial_reply,
    _clean_partial_reply_body,
    _get_unseen_event_ids_for_metadata,
    _get_unseen_messages_for_sender,
    _PartialReplyKind,
)
from mindroom.matrix.client import VisibleMessageLike, _stream_status_from_content, fetch_thread_history
from mindroom.streaming import (
    _CANCELLED_RESPONSE_NOTE,
    _PROGRESS_PLACEHOLDER,
    _RESTART_INTERRUPTED_RESPONSE_NOTE,
    StreamingResponse,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths


def _make_config() -> Config:
    """Return a minimal runtime-bound config for partial-reply tests."""
    config = Config.model_validate(
        {
            "agents": {"helper": {"display_name": "Helper", "role": "test"}},
            "models": {"default": {"provider": "openai", "id": "gpt-4"}},
        },
    )
    return bind_runtime_paths(config, test_runtime_paths(Path(tempfile.mkdtemp())))


def _get_unseen_messages(
    thread_history: list[dict[str, object]],
    agent_name: str,
    config: Config,
    runtime_paths: object,
    *,
    seen_event_ids: set[str],
    current_event_id: str | None,
    active_event_ids: set[str],
) -> tuple[list[VisibleMessageLike], set[_PartialReplyKind], set[str]]:
    response_sender_id = config.get_ids(runtime_paths).get(agent_name)
    response_sender = response_sender_id.full_id if response_sender_id is not None else None
    return _get_unseen_messages_for_sender(
        thread_history,
        sender_id=response_sender,
        seen_event_ids=seen_event_ids,
        current_event_id=current_event_id,
        active_event_ids=active_event_ids,
    )


def _make_text_event(
    *,
    event_id: str,
    sender: str,
    body: str,
    server_timestamp: int,
    source_content: dict[str, object],
) -> MagicMock:
    event = MagicMock(spec=nio.RoomMessageText)
    event.event_id = event_id
    event.sender = sender
    event.body = body
    event.server_timestamp = server_timestamp
    event.source = {
        "type": "m.room.message",
        "content": source_content,
    }
    return event


class TestClassifyPartialReply:
    """Test metadata-first partial reply classification."""

    def test_completed_metadata_is_not_partial(self) -> None:
        """Treat completed messages as fully delivered, even if the body looks partial."""
        assert (
            _classify_partial_reply(
                {"body": "Final answer", "stream_status": STREAM_STATUS_COMPLETED},
                active_event_ids=set(),
            )
            is None
        )

    def test_cancelled_metadata_is_interrupted(self) -> None:
        """Treat cancelled messages as interrupted partial replies."""
        assert (
            _classify_partial_reply(
                {"body": "Partial answer", "stream_status": STREAM_STATUS_CANCELLED},
                active_event_ids=set(),
            )
            is _PartialReplyKind.INTERRUPTED
        )

    def test_error_metadata_is_interrupted(self) -> None:
        """Treat errored messages as interrupted partial replies."""
        assert (
            _classify_partial_reply(
                {"body": "Partial answer", "stream_status": STREAM_STATUS_ERROR},
                active_event_ids=set(),
            )
            is _PartialReplyKind.INTERRUPTED
        )

    def test_pending_metadata_is_in_progress(self) -> None:
        """Treat initial sent-but-not-finalized messages as still in progress."""
        assert (
            _classify_partial_reply(
                {"body": "Thinking...", "stream_status": STREAM_STATUS_PENDING},
                active_event_ids=set(),
            )
            is _PartialReplyKind.IN_PROGRESS
        )

    def test_streaming_metadata_is_in_progress(self) -> None:
        """Treat actively edited streaming messages as still in progress."""
        assert (
            _classify_partial_reply(
                {"body": "Partial answer ⋯", "stream_status": STREAM_STATUS_STREAMING},
                active_event_ids=set(),
            )
            is _PartialReplyKind.IN_PROGRESS
        )

    def test_streaming_metadata_with_live_event_id_is_in_progress_even_when_old(self) -> None:
        """Prefer the live active-event set over any stale timestamp in the event body."""
        assert (
            _classify_partial_reply(
                {
                    "event_id": "e1",
                    "body": "Partial answer ⋯",
                    "stream_status": STREAM_STATUS_STREAMING,
                    "timestamp": 1_000,
                },
                active_event_ids={"e1"},
            )
            is _PartialReplyKind.IN_PROGRESS
        )

    def test_streaming_metadata_without_live_event_id_is_interrupted_immediately(self) -> None:
        """Treat non-live streaming events as interrupted when the bot has no active task for them."""
        assert (
            _classify_partial_reply(
                {
                    "event_id": "e1",
                    "body": "Partial answer ⋯",
                    "stream_status": STREAM_STATUS_STREAMING,
                    "timestamp": 599_000,
                },
                active_event_ids=set(),
            )
            is _PartialReplyKind.INTERRUPTED
        )

    def test_completed_metadata_wins_over_trailing_marker(self) -> None:
        """Prefer persisted completion metadata over stale visible marker text."""
        assert (
            _classify_partial_reply(
                {"body": "Finished text ⋯", "stream_status": STREAM_STATUS_COMPLETED},
                active_event_ids=set(),
            )
            is None
        )

    def test_trailing_marker_without_metadata_is_in_progress(self) -> None:
        """Fallback to body-marker detection for pre-upgrade messages."""
        assert (
            _classify_partial_reply(
                {"body": "Legacy partial ⋯"},
                active_event_ids=set(),
            )
            is _PartialReplyKind.IN_PROGRESS
        )

    @pytest.mark.parametrize(
        "body",
        [
            f"Legacy partial\n\n{_CANCELLED_RESPONSE_NOTE}",
            f"Legacy partial\n\n{_RESTART_INTERRUPTED_RESPONSE_NOTE}",
            "Legacy partial\n\n**[Response interrupted by an error: boom]**",
            "Legacy partial [cancelled]",
            "Legacy partial [error]",
        ],
    )
    def test_legacy_interrupted_markers_without_metadata_are_interrupted(self, body: str) -> None:
        """Fallback to interrupted classification for legacy cancelled/error/restart bodies."""
        assert (
            _classify_partial_reply(
                {"body": body},
                active_event_ids=set(),
            )
            is _PartialReplyKind.INTERRUPTED
        )

    def test_no_metadata_and_no_marker_is_not_partial(self) -> None:
        """Ignore messages that have neither metadata nor partial markers."""
        assert (
            _classify_partial_reply(
                {"body": "Completed response"},
                active_event_ids=set(),
            )
            is None
        )

    def test_non_text_body_without_metadata_is_not_partial(self) -> None:
        """Ignore malformed legacy bodies when there is no stream-status metadata to trust."""
        assert (
            _classify_partial_reply(
                {"body": {"structured": True}},
                active_event_ids=set(),
            )
            is None
        )


class TestCleanPartialReplyBody:
    """Test marker stripping and preserved partial-draft cleanup."""

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("Hello world ⋯", "Hello world"),
            ("Hello world ⋯..", "Hello world"),
            (f"Partial answer\n\n{_CANCELLED_RESPONSE_NOTE}", "Partial answer"),
            (f"Partial answer\n\n{_RESTART_INTERRUPTED_RESPONSE_NOTE}", "Partial answer"),
            ("Partial answer [cancelled]", "Partial answer"),
            ("Partial answer [error]", "Partial answer"),
            ("Partial answer\n\n**[Response interrupted by an error: boom]**", "Partial answer"),
            (f"{_PROGRESS_PLACEHOLDER} ⋯", ""),
        ],
    )
    def test_clean_partial_reply_body_strips_markers(self, body: str, expected: str) -> None:
        """Remove streaming markers and terminal status notes from preserved text."""
        assert _clean_partial_reply_body(body) == expected

    def test_clean_partial_reply_body_preserves_long_content(self) -> None:
        """Keep long partial-reply content once markers are removed."""
        result = _clean_partial_reply_body(f"{'x' * 5000} ⋯")
        assert result == "x" * 5000


class TestUnseenMessagesPartialReplies:
    """Test unseen-context extraction for self-authored partial replies."""

    def test_mixed_partial_reply_header_warns_about_live_and_interrupted_content(self) -> None:
        """Mixed partial states should use the combined warning header."""
        prompt = _build_prompt_with_unseen(
            "Continue.",
            [
                {"sender": "You (reply still streaming)", "body": "Live draft"},
                {"sender": "You (interrupted reply draft)", "body": "Interrupted draft"},
            ],
            partial_reply_kinds={_PartialReplyKind.IN_PROGRESS, _PartialReplyKind.INTERRUPTED},
        )

        assert "Some partial content from your previous response is still being delivered" in prompt
        assert "Other partial content was interrupted before completion" in prompt

    def test_includes_streaming_self_reply_with_cleaned_body_and_header(self) -> None:
        """Inject still-streaming self replies with the non-duplication warning header."""
        config = _make_config()
        runtime_paths = runtime_paths_for(config)
        agent_id = config.get_ids(runtime_paths)["helper"].full_id

        thread_history = [
            {"event_id": "e1", "sender": "@user:localhost", "body": "Hello"},
            {
                "event_id": "e2",
                "sender": agent_id,
                "body": "Partial reply ⋯",
                "stream_status": STREAM_STATUS_STREAMING,
                "content": {STREAM_STATUS_KEY: STREAM_STATUS_STREAMING},
            },
            {"event_id": "e3", "sender": "@user:localhost", "body": "New question"},
        ]

        unseen, partial_reply_kinds, in_progress_event_ids = _get_unseen_messages(
            thread_history,
            "helper",
            config,
            runtime_paths,
            seen_event_ids={"e1"},
            current_event_id="e3",
            active_event_ids={"e2"},
        )

        assert partial_reply_kinds == {_PartialReplyKind.IN_PROGRESS}
        assert in_progress_event_ids == {"e2"}
        assert len(unseen) == 1
        assert unseen[0]["body"] == "Partial reply"

        prompt = _build_prompt_with_unseen("Answer the new question.", unseen, partial_reply_kinds=partial_reply_kinds)
        assert "Your previous response is still being delivered." in prompt
        assert "Do NOT repeat or redo that work." in prompt

    def test_includes_interrupted_self_reply_with_interrupted_header(self) -> None:
        """Inject interrupted self replies with the continuation-oriented warning header."""
        config = _make_config()
        runtime_paths = runtime_paths_for(config)
        agent_id = config.get_ids(runtime_paths)["helper"].full_id

        thread_history = [
            {
                "event_id": "e1",
                "sender": agent_id,
                "body": f"Partial answer\n\n{_CANCELLED_RESPONSE_NOTE}",
                "stream_status": STREAM_STATUS_CANCELLED,
                "content": {STREAM_STATUS_KEY: STREAM_STATUS_CANCELLED},
            },
            {"event_id": "e2", "sender": "@user:localhost", "body": "Continue"},
        ]

        unseen, partial_reply_kinds, in_progress_event_ids = _get_unseen_messages(
            thread_history,
            "helper",
            config,
            runtime_paths,
            seen_event_ids=set(),
            current_event_id="e2",
            active_event_ids=set(),
        )

        assert partial_reply_kinds == {_PartialReplyKind.INTERRUPTED}
        assert in_progress_event_ids == set()
        assert len(unseen) == 1
        assert unseen[0]["body"] == "Partial answer"

        prompt = _build_prompt_with_unseen("Continue.", unseen, partial_reply_kinds=partial_reply_kinds)
        assert "Your previous response was interrupted before completion." in prompt
        assert "Continue from where you left off if appropriate." in prompt

    def test_in_progress_partial_reply_event_ids_are_excluded_from_seen_metadata(self) -> None:
        """Keep live self partial replies out of seen metadata until they become terminal."""
        config = _make_config()
        runtime_paths = runtime_paths_for(config)
        agent_id = config.get_ids(runtime_paths)["helper"].full_id

        unseen, partial_reply_kinds, in_progress_event_ids = _get_unseen_messages(
            [
                {
                    "event_id": "e1",
                    "sender": agent_id,
                    "body": "Partial reply ⋯",
                    "stream_status": STREAM_STATUS_STREAMING,
                    "content": {STREAM_STATUS_KEY: STREAM_STATUS_STREAMING},
                },
                {"event_id": "e2", "sender": "@user:localhost", "body": "Question"},
            ],
            "helper",
            config,
            runtime_paths,
            seen_event_ids=set(),
            current_event_id=None,
            active_event_ids={"e1"},
        )

        assert [msg["event_id"] for msg in unseen] == ["e1", "e2"]
        assert partial_reply_kinds == {_PartialReplyKind.IN_PROGRESS}
        assert _get_unseen_event_ids_for_metadata(unseen, in_progress_event_ids=in_progress_event_ids) == ["e2"]

    def test_recent_streaming_reply_without_live_event_id_is_treated_as_interrupted(self) -> None:
        """After restart, recent streaming metadata should not be mistaken for still-active output."""
        config = _make_config()
        runtime_paths = runtime_paths_for(config)
        agent_id = config.get_ids(runtime_paths)["helper"].full_id

        unseen, partial_reply_kinds, in_progress_event_ids = _get_unseen_messages(
            [
                {
                    "event_id": "e1",
                    "sender": agent_id,
                    "body": f"Partial reply\n\n{_RESTART_INTERRUPTED_RESPONSE_NOTE}",
                    "stream_status": STREAM_STATUS_STREAMING,
                    "timestamp": 599_000,
                    "content": {STREAM_STATUS_KEY: STREAM_STATUS_STREAMING},
                },
                {"event_id": "e2", "sender": "@user:localhost", "body": "Question"},
            ],
            "helper",
            config,
            runtime_paths,
            seen_event_ids=set(),
            current_event_id="e2",
            active_event_ids=set(),
        )

        assert partial_reply_kinds == {_PartialReplyKind.INTERRUPTED}
        assert in_progress_event_ids == set()
        assert unseen[0]["body"] == "Partial reply"

    def test_placeholder_only_self_reply_is_not_injected(self) -> None:
        """Do not inject placeholder-only self replies as meaningful unseen context."""
        config = _make_config()
        runtime_paths = runtime_paths_for(config)
        agent_id = config.get_ids(runtime_paths)["helper"].full_id

        unseen, partial_reply_kinds, _in_progress_event_ids = _get_unseen_messages(
            [
                {
                    "event_id": "e1",
                    "sender": agent_id,
                    "body": f"{_PROGRESS_PLACEHOLDER} ⋯",
                    "stream_status": STREAM_STATUS_STREAMING,
                    "content": {STREAM_STATUS_KEY: STREAM_STATUS_STREAMING},
                },
                {"event_id": "e2", "sender": "@user:localhost", "body": "Question"},
            ],
            "helper",
            config,
            runtime_paths,
            seen_event_ids=set(),
            current_event_id="e2",
            active_event_ids={"e1"},
        )

        assert unseen == []
        assert partial_reply_kinds == set()

    def test_interrupted_partial_reply_event_id_is_marked_seen_after_consumption(self) -> None:
        """Mark interrupted self partial replies as seen so they do not reappear forever."""
        config = _make_config()
        runtime_paths = runtime_paths_for(config)
        agent_id = config.get_ids(runtime_paths)["helper"].full_id

        initial_unseen, initial_kinds, initial_in_progress_event_ids = _get_unseen_messages(
            [
                {
                    "event_id": "e1",
                    "sender": agent_id,
                    "body": f"Partial reply\n\n{_CANCELLED_RESPONSE_NOTE}",
                    "stream_status": STREAM_STATUS_CANCELLED,
                    "content": {STREAM_STATUS_KEY: STREAM_STATUS_CANCELLED},
                },
                {"event_id": "e2", "sender": "@user:localhost", "body": "Continue"},
            ],
            "helper",
            config,
            runtime_paths,
            seen_event_ids=set(),
            current_event_id="e2",
            active_event_ids=set(),
        )
        seen_event_ids = set(
            _get_unseen_event_ids_for_metadata(
                initial_unseen,
                in_progress_event_ids=initial_in_progress_event_ids,
            ),
        ) | {"e2"}

        repeated_unseen, repeated_kinds, _repeated_in_progress_event_ids = _get_unseen_messages(
            [
                {
                    "event_id": "e1",
                    "sender": agent_id,
                    "body": f"Partial reply\n\n{_CANCELLED_RESPONSE_NOTE}",
                    "stream_status": STREAM_STATUS_CANCELLED,
                    "content": {STREAM_STATUS_KEY: STREAM_STATUS_CANCELLED},
                },
                {"event_id": "e3", "sender": "@user:localhost", "body": "New question"},
            ],
            "helper",
            config,
            runtime_paths,
            seen_event_ids=seen_event_ids,
            current_event_id="e3",
            active_event_ids=set(),
        )

        assert [msg["event_id"] for msg in initial_unseen] == ["e1"]
        assert initial_kinds == {_PartialReplyKind.INTERRUPTED}
        assert _get_unseen_event_ids_for_metadata(
            initial_unseen,
            in_progress_event_ids=initial_in_progress_event_ids,
        ) == ["e1"]
        assert repeated_unseen == []
        assert repeated_kinds == set()

    def test_same_partial_event_can_be_reclassified_after_status_change(self) -> None:
        """Keep self partial replies eligible for later interrupted reclassification."""
        config = _make_config()
        runtime_paths = runtime_paths_for(config)
        agent_id = config.get_ids(runtime_paths)["helper"].full_id

        initial_unseen, _initial_kinds, initial_in_progress_event_ids = _get_unseen_messages(
            [
                {
                    "event_id": "e1",
                    "sender": agent_id,
                    "body": "Partial reply ⋯",
                    "stream_status": STREAM_STATUS_STREAMING,
                    "content": {STREAM_STATUS_KEY: STREAM_STATUS_STREAMING},
                },
                {"event_id": "e2", "sender": "@user:localhost", "body": "Question"},
            ],
            "helper",
            config,
            runtime_paths,
            seen_event_ids=set(),
            current_event_id="e2",
            active_event_ids={"e1"},
        )
        seen_event_ids = set(
            _get_unseen_event_ids_for_metadata(
                initial_unseen,
                in_progress_event_ids=initial_in_progress_event_ids,
            ),
        ) | {"e2"}

        updated_unseen, updated_kinds, updated_in_progress_event_ids = _get_unseen_messages(
            [
                {
                    "event_id": "e1",
                    "sender": agent_id,
                    "body": f"Partial reply\n\n{_CANCELLED_RESPONSE_NOTE}",
                    "stream_status": STREAM_STATUS_CANCELLED,
                    "content": {STREAM_STATUS_KEY: STREAM_STATUS_CANCELLED},
                },
                {"event_id": "e3", "sender": "@user:localhost", "body": "Continue"},
            ],
            "helper",
            config,
            runtime_paths,
            seen_event_ids=seen_event_ids,
            current_event_id="e3",
            active_event_ids=set(),
        )

        assert updated_kinds == {_PartialReplyKind.INTERRUPTED}
        assert updated_in_progress_event_ids == set()
        assert [msg["event_id"] for msg in updated_unseen] == ["e1"]
        assert updated_unseen[0]["body"] == "Partial reply"


class TestThreadHistoryStreamStatus:
    """Test stream-status propagation through thread history reconstruction."""

    def test_stream_status_from_content_reads_current_namespace(self) -> None:
        """Read persisted stream status from the io.mindroom namespace."""
        assert _stream_status_from_content({STREAM_STATUS_KEY: STREAM_STATUS_STREAMING}) == STREAM_STATUS_STREAMING
        assert _stream_status_from_content({STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED}) == STREAM_STATUS_COMPLETED
        assert _stream_status_from_content(None) is None
        assert _stream_status_from_content({}) is None
        assert _stream_status_from_content({"unrelated": "key"}) is None

    @pytest.mark.asyncio
    async def test_fetch_thread_history_includes_status_from_latest_edit(self) -> None:
        """Apply the latest edit body and stream status to the synthesized history entry."""
        client = AsyncMock()

        root_event = _make_text_event(
            event_id="$thread_root",
            sender="@user:localhost",
            body="Question",
            server_timestamp=1000,
            source_content={"body": "Question"},
        )
        partial_event = _make_text_event(
            event_id="$agent_msg",
            sender="@agent:localhost",
            body="Partial answer ⋯",
            server_timestamp=2000,
            source_content={
                "body": "Partial answer ⋯",
                STREAM_STATUS_KEY: STREAM_STATUS_PENDING,
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root",
                },
            },
        )
        edit_event = _make_text_event(
            event_id="$edit1",
            sender="@agent:localhost",
            body="* Final answer",
            server_timestamp=3000,
            source_content={
                "body": "* Final answer",
                "m.new_content": {
                    "body": "Final answer",
                    STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED,
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread_root",
                    },
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$agent_msg",
                },
            },
        )

        response = MagicMock(spec=nio.RoomMessagesResponse)
        response.chunk = [edit_event, partial_event, root_event]
        response.end = None
        client.room_messages.return_value = response

        history = await fetch_thread_history(client, "!room:localhost", "$thread_root")

        assert history[1].body == "Final answer"
        assert history[1].stream_status == "completed"
        assert history[1].content[STREAM_STATUS_KEY] == "completed"


class TestStreamingFinalizeStatuses:
    """Test persisted stream-status values on finalize paths."""

    @pytest.mark.asyncio
    async def test_finalize_sets_completed_status(self) -> None:
        """Persist completed status on the final successful edit."""
        config = _make_config()
        runtime_paths = runtime_paths_for(config)
        client = AsyncMock()

        with (
            patch("mindroom.streaming.send_message", new_callable=AsyncMock) as mock_send_message,
            patch("mindroom.streaming.edit_message", new_callable=AsyncMock) as mock_edit_message,
        ):
            mock_send_message.return_value = "$event1"
            mock_edit_message.return_value = "$edit1"

            streaming = StreamingResponse(
                room_id="!room:localhost",
                reply_to_event_id=None,
                thread_id=None,
                sender_domain="localhost",
                config=config,
                runtime_paths=runtime_paths,
            )

            await streaming.update_content("Partial answer", client)
            await streaming.finalize(client)

        initial_content = mock_send_message.await_args.args[2]
        final_content = mock_edit_message.await_args.args[3]
        assert initial_content[STREAM_STATUS_KEY] == STREAM_STATUS_PENDING
        assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED

    @pytest.mark.asyncio
    async def test_cancelled_finalize_sets_cancelled_status(self) -> None:
        """Persist cancelled status on the final cancellation edit."""
        config = _make_config()
        runtime_paths = runtime_paths_for(config)
        client = AsyncMock()

        with (
            patch("mindroom.streaming.send_message", new_callable=AsyncMock) as mock_send_message,
            patch("mindroom.streaming.edit_message", new_callable=AsyncMock) as mock_edit_message,
        ):
            mock_send_message.return_value = "$event1"
            mock_edit_message.return_value = "$edit1"

            streaming = StreamingResponse(
                room_id="!room:localhost",
                reply_to_event_id=None,
                thread_id=None,
                sender_domain="localhost",
                config=config,
                runtime_paths=runtime_paths,
            )

            await streaming.update_content("Partial answer", client)
            await streaming.finalize(client, cancelled=True)

        final_content = mock_edit_message.await_args.args[3]
        assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_CANCELLED

    @pytest.mark.asyncio
    async def test_error_finalize_sets_error_status(self) -> None:
        """Persist error status on the final error edit."""
        config = _make_config()
        runtime_paths = runtime_paths_for(config)
        client = AsyncMock()

        with (
            patch("mindroom.streaming.send_message", new_callable=AsyncMock) as mock_send_message,
            patch("mindroom.streaming.edit_message", new_callable=AsyncMock) as mock_edit_message,
        ):
            mock_send_message.return_value = "$event1"
            mock_edit_message.return_value = "$edit1"

            streaming = StreamingResponse(
                room_id="!room:localhost",
                reply_to_event_id=None,
                thread_id=None,
                sender_domain="localhost",
                config=config,
                runtime_paths=runtime_paths,
            )

            await streaming.update_content("Partial answer", client)
            await streaming.finalize(client, error=RuntimeError("boom"))

        final_content = mock_edit_message.await_args.args[3]
        assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR

    @pytest.mark.asyncio
    async def test_finalize_retries_terminal_edit_once(self) -> None:
        """Retry the terminal edit once before giving up on persisting the status."""
        config = _make_config()
        runtime_paths = runtime_paths_for(config)
        client = AsyncMock()

        with (
            patch("mindroom.streaming.send_message", new_callable=AsyncMock) as mock_send_message,
            patch("mindroom.streaming.edit_message", new_callable=AsyncMock) as mock_edit_message,
        ):
            mock_send_message.return_value = "$event1"
            mock_edit_message.side_effect = [None, "$edit2"]

            streaming = StreamingResponse(
                room_id="!room:localhost",
                reply_to_event_id=None,
                thread_id=None,
                sender_domain="localhost",
                config=config,
                runtime_paths=runtime_paths,
            )

            await streaming.update_content("Partial answer", client)
            await streaming.finalize(client, cancelled=True)

        assert mock_edit_message.await_count == 2
        for call in mock_edit_message.await_args_list:
            assert call.args[3][STREAM_STATUS_KEY] == STREAM_STATUS_CANCELLED

    @pytest.mark.asyncio
    async def test_finalize_retries_terminal_edit_after_exception(self) -> None:
        """Retry terminal edits when the first edit attempt raises."""
        config = _make_config()
        runtime_paths = runtime_paths_for(config)
        client = AsyncMock()

        with (
            patch("mindroom.streaming.send_message", new_callable=AsyncMock) as mock_send_message,
            patch("mindroom.streaming.edit_message", new_callable=AsyncMock) as mock_edit_message,
        ):
            mock_send_message.return_value = "$event1"
            mock_edit_message.side_effect = [RuntimeError("transport boom"), "$edit2"]

            streaming = StreamingResponse(
                room_id="!room:localhost",
                reply_to_event_id=None,
                thread_id=None,
                sender_domain="localhost",
                config=config,
                runtime_paths=runtime_paths,
            )

            await streaming.update_content("Partial answer", client)
            await streaming.finalize(client, cancelled=True)

        assert mock_edit_message.await_count == 2
        for call in mock_edit_message.await_args_list:
            assert call.args[3][STREAM_STATUS_KEY] == STREAM_STATUS_CANCELLED
