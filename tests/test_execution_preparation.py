"""Tests for history-message preparation helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom import execution_preparation
from mindroom.attachments import _attachment_id_for_event, register_local_attachment
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig
from mindroom.constants import ATTACHMENT_IDS_KEY, ORIGINAL_SENDER_KEY
from mindroom.execution_preparation import (
    _build_thread_history_messages,
    _build_unseen_context_messages,
    _fallback_static_token_budget,
    _prepare_execution_context_common,
    _ThreadAttachmentContext,
    render_prepared_messages_text,
)
from mindroom.history import HistoryPolicy, HistoryScope, PreparedScopeHistory, ResolvedHistorySettings
from mindroom.history.policy import resolve_history_execution_plan
from mindroom.history.runtime import _ResolvedPreparationInputs
from mindroom.tool_system.events import ToolTraceEntry, build_tool_trace_content
from tests.conftest import make_visible_message

if TYPE_CHECKING:
    from pathlib import Path


def _config() -> Config:
    return Config.model_validate({})


def _tool_trace_content() -> dict[str, object]:
    content = build_tool_trace_content(
        [ToolTraceEntry(type="tool_call_completed", tool_name="run_shell_command")],
    )
    assert content is not None
    return content


def _prepared_scope_with_persisted_replay() -> PreparedScopeHistory:
    history_settings = ResolvedHistorySettings(
        policy=HistoryPolicy(mode="runs", limit=1),
        max_tool_calls_from_history=None,
    )
    compaction_config = CompactionConfig(enabled=False, reserve_tokens=100)
    execution_plan = resolve_history_execution_plan(
        config=_config(),
        compaction_config=compaction_config,
        has_authored_compaction_config=False,
        active_model_name="test-model",
        active_context_window=10_000,
        static_prompt_tokens=10,
    )
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    session = AgentSession(
        session_id="thread-session",
        agent_id="test_agent",
        runs=[
            RunOutput(
                run_id="run-1",
                agent_id="test_agent",
                status=RunStatus.completed,
                messages=[
                    Message(role="user", content="persisted question"),
                    Message(role="assistant", content="persisted answer"),
                ],
            ),
        ],
        created_at=1,
        updated_at=1,
    )
    return PreparedScopeHistory(
        scope=scope,
        session=session,
        resolved_inputs=_ResolvedPreparationInputs(
            history_settings=history_settings,
            compaction_config=compaction_config,
            has_authored_compaction_config=False,
            active_model_name="test-model",
            active_context_window=10_000,
            static_prompt_tokens=10,
            execution_plan=execution_plan,
        ),
    )


def test_fallback_static_token_budget_preserves_context_window_bounds() -> None:
    """Fallback static budgeting should keep missing and reserve-clamped bounds."""
    assert _fallback_static_token_budget(context_window=None, reserve_tokens=100) is None
    assert _fallback_static_token_budget(context_window=0, reserve_tokens=100) is None
    assert _fallback_static_token_budget(context_window=1_000, reserve_tokens=800) == 500
    assert _fallback_static_token_budget(context_window=1_000, reserve_tokens=100) == 900


@pytest.mark.asyncio
async def test_prepare_execution_context_skips_fallback_replay_when_persisted_history_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persisted replay should avoid building unused Matrix fallback context."""

    async def prepare_scope_history(_prepared_prompt: str) -> PreparedScopeHistory:
        return _prepared_scope_with_persisted_replay()

    def fail_if_fallback_context_is_built(*_args: object, **_kwargs: object) -> tuple[Message, ...]:
        message = "unused Matrix fallback context was built"
        raise AssertionError(message)

    monkeypatch.setattr(execution_preparation, "_build_thread_history_messages", fail_if_fallback_context_is_built)

    prepared = await _prepare_execution_context_common(
        scope_context=None,
        prompt="Current request",
        thread_history=[
            make_visible_message(sender="@alice:localhost", body="older context", event_id="$older"),
            make_visible_message(sender="@alice:localhost", body="Current request", event_id="$current"),
        ],
        reply_to_event_id="$current",
        active_event_ids=(),
        response_sender_id="@mindroom_code:localhost",
        current_sender_id="@alice:localhost",
        config=_config(),
        prepare_scope_history_fn=prepare_scope_history,
        estimate_static_tokens_fn=lambda text: len(text.split()),
        render_messages_text_fn=render_prepared_messages_text,
        fallback_static_token_budget=100,
    )

    assert prepared.replays_persisted_history is True
    assert prepared.context_messages[0].content == "@alice:localhost: older context"


def test_fallback_thread_history_caps_long_messages_without_dropping_them() -> None:
    """Oversized Matrix fallback messages should stay in context with a capped body."""
    long_body = "x" * 201
    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@alice:localhost",
                body=long_body,
                event_id="$long",
            ),
        ],
        response_sender_id="@mindroom_team:localhost",
        config=_config(),
        max_message_length=200,
    )

    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[0].content == f"@alice:localhost: {'x' * 199}…"
    assert long_body not in str(messages[0].content)
    assert messages[1].content == "Current request"


def test_fallback_thread_history_pins_attachments_to_their_messages(tmp_path: Path) -> None:
    """History attachments annotate and attach media on the message that carried them."""
    image_path = tmp_path / "car.jpg"
    image_path.write_bytes(b"\xff\xd8\xffjpeg")
    record = register_local_attachment(
        tmp_path,
        image_path,
        kind="image",
        attachment_id="att_car",
        filename="car.jpg",
        mime_type="image/jpeg",
        room_id="!room:localhost",
    )
    assert record is not None

    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@alice:localhost",
                body="look at this",
                event_id="$img",
                content={ATTACHMENT_IDS_KEY: ["att_car"]},
            ),
            make_visible_message(
                sender="@alice:localhost",
                body="no attachments here",
                event_id="$text",
            ),
        ],
        response_sender_id="@mindroom_team:localhost",
        config=_config(),
        attachment_context=_ThreadAttachmentContext(storage_path=tmp_path, room_id="!room:localhost"),
    )

    assert len(messages) == 3
    history_with_media = messages[0]
    assert history_with_media.role == "user"
    assert history_with_media.content == ('@alice:localhost: look at this\n[attachments: att_car (image, "car.jpg")]')
    assert [image.id for image in (history_with_media.images or [])] == ["att_car"]
    assert messages[1].content == "@alice:localhost: no attachments here"
    assert not messages[1].images
    assert not messages[2].images


def test_fallback_thread_history_maps_raw_media_events_to_attachments(tmp_path: Path) -> None:
    """Raw media events without MindRoom metadata resolve via the deterministic event ID."""
    attachment_id = _attachment_id_for_event("$raw-img")
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    record = register_local_attachment(
        tmp_path,
        image_path,
        kind="image",
        attachment_id=attachment_id,
        filename="photo.png",
        mime_type="image/png",
        room_id="!room:localhost",
    )
    assert record is not None

    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@alice:localhost",
                body="photo.png",
                event_id="$raw-img",
                content={"msgtype": "m.image", "body": "photo.png"},
            ),
        ],
        response_sender_id="@mindroom_team:localhost",
        config=_config(),
        attachment_context=_ThreadAttachmentContext(storage_path=tmp_path, room_id="!room:localhost"),
    )

    assert messages[0].content == (f'@alice:localhost: photo.png\n[attachments: {attachment_id} (image, "photo.png")]')
    assert [image.id for image in (messages[0].images or [])] == [attachment_id]


def test_fallback_thread_history_agent_attachments_annotate_without_media(tmp_path: Path) -> None:
    """Assistant-authored attachments surface as text only; providers reject assistant media."""
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF-1.4")
    record = register_local_attachment(
        tmp_path,
        file_path,
        kind="file",
        attachment_id="att_report",
        filename="report.pdf",
        room_id="!room:localhost",
    )
    assert record is not None

    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@mindroom_team:localhost",
                body="here is the report",
                event_id="$agent-file",
                content={ATTACHMENT_IDS_KEY: ["att_report"]},
            ),
        ],
        response_sender_id="@mindroom_team:localhost",
        config=_config(),
        attachment_context=_ThreadAttachmentContext(storage_path=tmp_path, room_id="!room:localhost"),
    )

    assert messages[0].role == "assistant"
    assert messages[0].content == 'here is the report\n[attachments: att_report (file, "report.pdf")]'
    assert not messages[0].files


def test_fallback_thread_history_drops_cross_room_attachments(tmp_path: Path) -> None:
    """Attachment references from other rooms neither annotate nor attach media."""
    file_path = tmp_path / "secret.txt"
    file_path.write_text("secret", encoding="utf-8")
    record = register_local_attachment(
        tmp_path,
        file_path,
        kind="file",
        attachment_id="att_other_room",
        filename="secret.txt",
        room_id="!other:localhost",
    )
    assert record is not None

    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@alice:localhost",
                body="see file",
                event_id="$cross",
                content={ATTACHMENT_IDS_KEY: ["att_other_room"]},
            ),
        ],
        response_sender_id="@mindroom_team:localhost",
        config=_config(),
        attachment_context=_ThreadAttachmentContext(storage_path=tmp_path, room_id="!room:localhost"),
    )

    assert messages[0].content == "@alice:localhost: see file"
    assert not messages[0].files


def test_fallback_thread_history_drops_cross_thread_attachments(tmp_path: Path) -> None:
    """Attachment references from another thread in the same room stay out of scope."""
    file_path = tmp_path / "other-thread.txt"
    file_path.write_text("other thread", encoding="utf-8")
    cross_thread = register_local_attachment(
        tmp_path,
        file_path,
        kind="file",
        attachment_id="att_other_thread",
        filename="other-thread.txt",
        room_id="!room:localhost",
        thread_id="$other_thread",
    )
    in_thread = register_local_attachment(
        tmp_path,
        file_path,
        kind="file",
        attachment_id="att_in_thread",
        filename="in-thread.txt",
        room_id="!room:localhost",
        thread_id="$thread",
    )
    assert cross_thread is not None
    assert in_thread is not None

    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@alice:localhost",
                body="see files",
                event_id="$in-thread",
                thread_id="$thread",
                content={ATTACHMENT_IDS_KEY: ["att_other_thread", "att_in_thread"]},
            ),
        ],
        response_sender_id="@mindroom_team:localhost",
        config=_config(),
        attachment_context=_ThreadAttachmentContext(storage_path=tmp_path, room_id="!room:localhost"),
    )

    assert messages[0].content == ('@alice:localhost: see files\n[attachments: att_in_thread (file, "in-thread.txt")]')
    assert [file.id for file in (messages[0].files or [])] == ["att_in_thread"]


def test_fallback_thread_history_matches_thread_root_attachments(tmp_path: Path) -> None:
    """Thread-root media records (registered under the root event ID) stay in scope."""
    image_path = tmp_path / "root.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    record = register_local_attachment(
        tmp_path,
        image_path,
        kind="image",
        attachment_id="att_root",
        filename="root.png",
        room_id="!room:localhost",
        thread_id="$root",
    )
    assert record is not None

    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@alice:localhost",
                body="root image",
                event_id="$root",
                content={ATTACHMENT_IDS_KEY: ["att_root"]},
            ),
        ],
        response_sender_id="@mindroom_team:localhost",
        config=_config(),
        attachment_context=_ThreadAttachmentContext(storage_path=tmp_path, room_id="!room:localhost"),
    )

    assert messages[0].content == '@alice:localhost: root image\n[attachments: att_root (image, "root.png")]'
    assert [image.id for image in (messages[0].images or [])] == ["att_root"]


def test_fallback_thread_history_strips_visible_tool_markers_from_assistant_context() -> None:
    """Visible Matrix tool markers should not train the model to echo fake tool calls."""
    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@mindroom_code:localhost",
                body=(
                    "Checking status.\n\n"
                    "🔧 `run_shell_command` [1]\n\n"
                    "Still checking.\n\n"
                    "🔧 `read_file` [2]\n\n"
                    "---\n\n"
                    "Done."
                ),
                event_id="$assistant",
            ),
        ],
        response_sender_id="@mindroom_code:localhost",
        config=_config(),
    )

    assert messages[0].role == "assistant"
    assert messages[0].content == "Checking status.\n\n\nStill checking.\n\n\nDone."
    assert "🔧" not in str(messages[0].content)


def test_fallback_thread_history_drops_marker_only_messages_from_context() -> None:
    """Marker-only visible messages should not become empty assistant context turns."""
    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@mindroom_code:localhost",
                body="🔧 `run_shell_command` [1]\n\n🔧 `read_file` [2]",
                event_id="$markers",
            ),
        ],
        response_sender_id="@mindroom_code:localhost",
        config=_config(),
    )

    assert len(messages) == 1
    assert messages[0].content == "Current request"


def test_fallback_thread_history_preserves_user_authored_tool_marker_text() -> None:
    """Human-authored marker-shaped text is conversation content, not MindRoom display chrome."""
    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@alice:localhost",
                body="Please see:\n\n🔧 `run_shell_command` [1]\n\nActual content",
                event_id="$user",
            ),
        ],
        response_sender_id="@mindroom_code:localhost",
        config=_config(),
    )

    assert messages[0].role == "user"
    assert messages[0].content == "@alice:localhost: Please see:\n\n🔧 `run_shell_command` [1]\n\nActual content"


def test_fallback_thread_history_strips_structured_tool_markers_from_labeled_context() -> None:
    """Structured MindRoom tool trace metadata identifies marker lines as display chrome."""
    messages = _build_thread_history_messages(
        "Current request",
        [
            make_visible_message(
                sender="@mindroom_research:localhost",
                body="Please see:\n\n🔧 `run_shell_command` [1]\n\nActual content",
                event_id="$agent",
                content={
                    "body": "Please see:\n\n🔧 `run_shell_command` [1]\n\nActual content",
                    **_tool_trace_content(),
                },
            ),
        ],
        response_sender_id="@mindroom_code:localhost",
        config=_config(),
    )

    assert messages[0].role == "user"
    assert messages[0].content == "@mindroom_research:localhost: Please see:\n\n\nActual content"


def test_unseen_context_keeps_self_sent_relayed_user_message() -> None:
    """A tool-relayed user message from the agent account should remain user context."""
    thread_history = [
        make_visible_message(
            sender="@mindroom_code:localhost",
            body="@mindroom_missing_agent Please investigate this",
            event_id="$spawn-root",
            content={
                "body": "@mindroom_missing_agent Please investigate this",
                ORIGINAL_SENDER_KEY: "@alice:localhost",
            },
        ),
        make_visible_message(
            sender="@alice:localhost",
            body="What happened?",
            event_id="$question",
        ),
    ]

    messages, unseen_event_ids = _build_unseen_context_messages(
        "What happened?",
        thread_history,
        seen_event_ids=set(),
        current_event_id="$question",
        active_event_ids=(),
        response_sender_id="@mindroom_code:localhost",
        current_sender_id="@alice:localhost",
        config=_config(),
    )

    assert unseen_event_ids == ["$spawn-root"]
    assert messages[0].role == "user"
    assert messages[0].content == "@alice:localhost: @mindroom_missing_agent Please investigate this"


def test_unseen_context_keeps_unpersisted_self_sent_message() -> None:
    """A self-sent Matrix event not known to persisted history should remain visible context."""
    thread_history = [
        make_visible_message(
            sender="@mindroom_code:localhost",
            body="@mindroom_missing_agent Please investigate this",
            event_id="$spawn-root",
        ),
        make_visible_message(
            sender="@alice:localhost",
            body="What happened?",
            event_id="$question",
        ),
    ]

    messages, unseen_event_ids = _build_unseen_context_messages(
        "What happened?",
        thread_history,
        seen_event_ids=set(),
        current_event_id="$question",
        active_event_ids=(),
        response_sender_id="@mindroom_code:localhost",
        current_sender_id="@alice:localhost",
        config=_config(),
    )

    assert unseen_event_ids == ["$spawn-root"]
    assert messages[0].role == "assistant"
    assert messages[0].content == "@mindroom_missing_agent Please investigate this"


def test_unseen_context_skips_persisted_self_sent_response_event() -> None:
    """A self-sent Matrix event already represented in persisted history should not be duplicated."""
    thread_history = [
        make_visible_message(
            sender="@mindroom_code:localhost",
            body="Persisted assistant answer",
            event_id="$answer",
        ),
        make_visible_message(
            sender="@alice:localhost",
            body="What next?",
            event_id="$question",
        ),
    ]

    messages, unseen_event_ids = _build_unseen_context_messages(
        "What next?",
        thread_history,
        seen_event_ids={"$answer"},
        current_event_id="$question",
        active_event_ids=(),
        response_sender_id="@mindroom_code:localhost",
        current_sender_id="@alice:localhost",
        config=_config(),
    )

    assert unseen_event_ids == []
    assert len(messages) == 1
    assert messages[0].content == 'Current message:\n<msg from="@alice:localhost"><![CDATA[What next?]]></msg>'
