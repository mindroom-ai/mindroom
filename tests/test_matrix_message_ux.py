"""Agent-facing Matrix messaging uses explicit recipients and consistent conversations."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.attachments import register_local_attachment
from mindroom.constants import ATTACHMENT_IDS_KEY, ORIGINAL_SENDER_KEY, SKIP_MENTIONS_KEY, SOURCE_KIND_KEY
from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.matrix.runtime_media import RuntimeEncryptedMediaAttachment
from mindroom.tool_system.runtime_context import register_tool_runtime_media_attachment, tool_runtime_context
from tests.conftest import delivered_matrix_event, delivered_matrix_side_effect, make_latest_thread_event_id_mock
from tests.test_matrix_agent_discovery import context as context  # noqa: PLC0414 - expose imported pytest fixture
from tests.test_matrix_message_tool import _make_context

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


@pytest.fixture(autouse=True)
def _configure_reader(context: ToolRuntimeContext) -> None:
    """Use a reader that honors Matrix thread fallback semantics."""
    cast(
        "AsyncMock",
        context.conversation_reader.latest_thread_event_id,
    ).side_effect = make_latest_thread_event_id_mock().side_effect


@pytest.fixture(autouse=True)
def _reset_rate_limit() -> None:
    MatrixMessageTools._recent_actions.clear()


@pytest.mark.asyncio
async def test_send_uses_current_conversation_without_dispatching_body_mentions() -> None:
    """Omitting a recipient must keep the current thread and suppress agent dispatch."""
    runtime = _make_context(thread_id="$current")
    with (
        tool_runtime_context(runtime),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$sent")),
        ) as send,
    ):
        result = json.loads(await MatrixMessageTools().matrix_message(message="Update from @general"))
    assert result["thread_id"] == "$current"
    content = send.await_args.args[2]
    assert content["m.relates_to"]["event_id"] == "$current"
    assert content[SKIP_MENTIONS_KEY] is True


@pytest.mark.asyncio
async def test_explicit_recipient_is_only_dispatched_agent(context: ToolRuntimeContext) -> None:
    """Other names in the task body must not become extra dispatch targets."""
    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$sent")),
        ) as send,
    ):
        result = json.loads(
            await MatrixMessageTools().matrix_message(
                recipient="general",
                message="Compare @code with @blocked without asking them.",
            ),
        )
    assert result["status"] == "ok"
    assert result["thread_id"] == "$current"
    content = send.await_args.args[2]
    assert content["m.mentions"] == {"user_ids": ["@actual_general:localhost"]}
    assert content[ORIGINAL_SENDER_KEY] == "@alice:localhost"
    assert content[SOURCE_KIND_KEY] == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert SKIP_MENTIONS_KEY not in content


@pytest.mark.asyncio
async def test_new_thread_returns_usable_conversation_handle(context: ToolRuntimeContext) -> None:
    """Starting a new conversation must not inherit the caller's active thread."""
    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$new-root")),
        ) as send,
    ):
        result = json.loads(
            await MatrixMessageTools().matrix_message(recipient="general", message="Investigate", new_thread=True),
        )
    assert result["thread_id"] == "$new-root"
    assert result["event_id"] == "$new-root"
    assert "m.relates_to" not in send.await_args.args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient", ["blocked", "absent", "unknown"])
async def test_unavailable_recipient_fails_before_sending(context: ToolRuntimeContext, recipient: str) -> None:
    """The room's actual requester and live-agent policy must gate explicit recipients."""
    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new_callable=AsyncMock,
        ) as send,
    ):
        result = json.loads(await MatrixMessageTools().matrix_message(recipient=recipient, message="Do work"))
    assert result["status"] == "error"
    assert recipient in result["message"]
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_recipient_attachments_keep_order_and_arrive_before_dispatch(
    context: ToolRuntimeContext,
    tmp_path: Path,
) -> None:
    """A recipient must see all ordered files before the message starts its response."""
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first")
    second.write_text("second")
    registered = register_local_attachment(tmp_path, second, kind="file", attachment_id="att_second")
    assert registered is not None
    runtime = replace(context, storage_path=tmp_path, attachment_ids=("att_second",))
    observed: list[tuple[str, str | None]] = []

    async def send_file(
        _client: nio.AsyncClient,
        _room_id: str,
        path: Path,
        *,
        thread_id: str | None,
        **_kwargs: object,
    ) -> str:
        observed.append((path.read_text(), thread_id))
        return f"$file{len(observed)}"

    async def send_text(_client: nio.AsyncClient, _room_id: str, content: dict[str, Any]) -> object:
        observed.append(("dispatch", content["m.relates_to"]["event_id"]))
        return delivered_matrix_event("$task")

    with (
        tool_runtime_context(runtime),
        patch("mindroom.custom_tools.attachments.send_file_message", side_effect=send_file),
        patch("mindroom.custom_tools.matrix_conversation_operations.send_message_result", side_effect=send_text),
    ):
        result = json.loads(
            await MatrixMessageTools(tool_output_workspace_root=tmp_path).matrix_message(
                recipient="general",
                message="Compare both files",
                attachments=["first.txt", "att_second"],
                new_thread=True,
            ),
        )
    assert observed == [("first", None), ("second", "$file1"), ("dispatch", "$file1")]
    assert result["thread_id"] == "$file1"
    assert result["event_id"] == "$task"


@pytest.mark.asyncio
async def test_missing_attachment_prevents_message_dispatch(context: ToolRuntimeContext) -> None:
    """Attachment lookup failure must not start a task missing its required files."""
    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new_callable=AsyncMock,
        ) as send,
    ):
        result = json.loads(
            await MatrixMessageTools().matrix_message(
                recipient="general",
                message="Read this",
                attachments=["att_missing"],
            ),
        )
    assert result["status"] == "error"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_recipient_uses_current_authorization_config(context: ToolRuntimeContext) -> None:
    """Revocation after context creation must prevent dispatch."""
    current = context.config.model_copy(deep=True)
    assert current.agents["general"].access is not None
    current.agents["general"].access.users = []
    runtime = replace(context, config_provider=lambda: current)
    with tool_runtime_context(runtime):
        result = json.loads(await MatrixMessageTools().matrix_message(recipient="general", message="Do work"))
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_room_mode_recipient_uses_room_conversation(context: ToolRuntimeContext) -> None:
    """Room-mode recipients must receive files and messages in the room they answer in."""
    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$sent")),
        ) as send,
    ):
        result = json.loads(await MatrixMessageTools().matrix_message(recipient="code", message="Do work"))
    assert result["status"] == "ok"
    assert result["thread_id"] is None
    assert "m.relates_to" not in send.await_args.args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"new_thread": True, "thread_id": "$existing"},
        {"new_thread": True, "action": "read"},
        {"recipient": "general", "action": "read"},
        {"recipient": "code", "new_thread": True},
        {"recipient": "code", "thread_id": "$existing"},
    ],
)
async def test_conflicting_message_options_do_not_send(
    context: ToolRuntimeContext,
    arguments: dict[str, Any],
) -> None:
    """Conflicting targeting options must be explained before any visible side effect."""
    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new_callable=AsyncMock,
        ) as send,
    ):
        result = json.loads(await MatrixMessageTools().matrix_message(message="Do work", **arguments))
    assert result["status"] == "error"
    send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient", [None, "general"])
@pytest.mark.parametrize("with_attachment", [False, True])
async def test_explicit_room_timeline_keeps_all_deliveries_outside_threads(
    context: ToolRuntimeContext,
    tmp_path: Path,
    recipient: str | None,
    *,
    with_attachment: bool,
) -> None:
    """An explicit room target must override automatic grouping and returned scope."""
    (tmp_path / "file.txt").write_text("file")
    runtime = replace(context, storage_path=tmp_path)
    with (
        tool_runtime_context(runtime),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$text")),
        ) as send,
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file")) as send_file,
    ):
        result = json.loads(
            await MatrixMessageTools(tool_output_workspace_root=tmp_path).matrix_message(
                message="Update",
                recipient=recipient,
                thread_id="room",
                attachments=["file.txt"] if with_attachment else None,
            ),
        )
    assert result["status"] == "ok"
    assert result["thread_id"] == ("$text" if recipient is not None else None)
    assert "m.relates_to" not in send.await_args.args[2]
    if with_attachment:
        assert send_file.await_args.kwargs["thread_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_text", [False, True])
async def test_recipient_partial_delivery_keeps_handle_and_does_not_retry(
    context: ToolRuntimeContext,
    tmp_path: Path,
    *,
    fail_text: bool,
) -> None:
    """File or dispatch failure must retain delivered IDs without starting an incomplete task."""
    (tmp_path / "first.txt").write_text("first")
    (tmp_path / "second.txt").write_text("second")
    runtime = replace(context, storage_path=tmp_path)
    with (
        tool_runtime_context(runtime),
        patch(
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(side_effect=["$first", "$second" if fail_text else None]),
        ) as files,
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(return_value=None),
        ) as send,
    ):
        result = json.loads(
            await MatrixMessageTools(tool_output_workspace_root=tmp_path).matrix_message(
                recipient="general",
                message="Review files",
                new_thread=True,
                attachments=["first.txt", "second.txt"],
            ),
        )
    assert result["status"] == "error"
    assert result["thread_id"] == "$first"
    assert result["attachment_event_ids"] == (["$first", "$second"] if fail_text else ["$first"])
    assert files.await_count == 2
    assert send.await_count == int(fail_text)


@pytest.mark.asyncio
async def test_attachment_id_like_filename_can_be_sent_with_dot_slash(
    context: ToolRuntimeContext,
    tmp_path: Path,
) -> None:
    """Explicit relative paths disambiguate local filenames from context attachment IDs."""
    local = tmp_path / "att_report"
    local.write_text("report")
    runtime = replace(context, storage_path=tmp_path)
    with (
        tool_runtime_context(runtime),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file")) as send,
    ):
        result = json.loads(
            await MatrixMessageTools(tool_output_workspace_root=tmp_path).matrix_message(
                attachments=["./att_report"],
            ),
        )
    assert result["status"] == "ok"
    assert send.await_args.args[2].read_text() == "report"
    assert send.await_args.kwargs["filename"] == "att_report"


@pytest.mark.asyncio
async def test_room_recipient_receives_trusted_attachment_references(
    context: ToolRuntimeContext,
    tmp_path: Path,
) -> None:
    """Room-mode agents need explicit file references because they do not load thread history."""
    (tmp_path / "report.txt").write_text("report")
    runtime = replace(context, storage_path=tmp_path)
    with (
        tool_runtime_context(runtime),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file")),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$text")),
        ) as send,
    ):
        result = json.loads(
            await MatrixMessageTools(tool_output_workspace_root=tmp_path).matrix_message(
                recipient="code",
                message="Read the report",
                attachments=["report.txt"],
            ),
        )
    assert result["status"] == "ok"
    assert send.await_args.args[2][ATTACHMENT_IDS_KEY] == result["resolved_attachment_ids"]
    assert result["resolved_attachment_ids"]
    assert result["thread_id"] is None


@pytest.mark.asyncio
async def test_self_message_without_human_requester_does_not_report_false_dispatch(context: ToolRuntimeContext) -> None:
    """Own-agent ingress cannot promote a bot requester into a human relay."""
    context.client.user_id = "@actual_general:localhost"
    context.config.bot_accounts = [context.requester_id]
    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$text")),
        ) as send,
    ):
        result = json.loads(await MatrixMessageTools().matrix_message(recipient="general", message="Continue"))
    assert result["status"] == "error"
    assert "human requester" in result["message"]
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_room_recipient_rejects_ephemeral_media_before_delivery(context: ToolRuntimeContext) -> None:
    """Ephemeral media cannot be forwarded as a persisted room attachment reference."""
    media = RuntimeEncryptedMediaAttachment(
        attachment_id="att_screenshot",
        filename="screenshot.png",
        url="mxc://example.org/image",
        key="key",
        iv="iv",
        sha256="hash",
        mime_type="image/png",
        size=123,
    )
    register_tool_runtime_media_attachment(context, media)
    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.attachments.send_runtime_encrypted_media_message", new=AsyncMock()) as send_media,
        patch("mindroom.custom_tools.matrix_conversation_operations.send_message_result", new=AsyncMock()) as send_text,
    ):
        result = json.loads(
            await MatrixMessageTools().matrix_message(
                recipient="code",
                message="Inspect this",
                attachments=[media.attachment_id],
            ),
        )
    assert result["status"] == "error"
    assert "threaded recipient conversation" in result["message"]
    send_media.assert_not_awaited()
    send_text.assert_not_awaited()
