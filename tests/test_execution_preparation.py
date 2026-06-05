"""Tests for history-message preparation helpers."""

from __future__ import annotations

import pytest
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom import execution_preparation
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.execution_preparation import (
    _build_thread_history_messages,
    _build_unseen_context_messages,
    _fallback_static_token_budget,
    _prepare_execution_context_common,
    render_prepared_messages_text,
)
from mindroom.history import HistoryPolicy, HistoryScope, PreparedScopeHistory, ResolvedHistorySettings
from mindroom.history.policy import resolve_history_execution_plan
from mindroom.history.runtime import _ResolvedPreparationInputs
from mindroom.tool_system.events import ToolTraceEntry, build_tool_trace_content
from tests.conftest import make_visible_message


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
