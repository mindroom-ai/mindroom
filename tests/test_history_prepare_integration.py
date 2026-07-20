"""Tests for prepare_agent_and_prompt integration and native Agno history replay."""
# ruff: noqa: D103, TC003

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.agent import Agent
from agno.models.message import Message
from agno.run.agent import RunInput, RunOutput
from agno.session.summary import SessionSummary
from agno.team import Team
from defusedxml.ElementTree import fromstring

from mindroom.agent_storage import create_session_storage, get_agent_session, get_team_session
from mindroom.agents import create_agent
from mindroom.ai import _prepare_agent_and_prompt
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, DefaultsConfig, ModelConfig
from mindroom.constants import MINDROOM_LOCATION_MARKER_METADATA_KEY
from mindroom.execution_preparation import (
    _build_matrix_prompt_with_history,
    _PreparedExecutionContext,
)
from mindroom.history.compaction import _build_summary_input
from mindroom.history.prompt_tokens import (
    estimate_agent_static_tokens,
)
from mindroom.history.runtime import (
    open_bound_scope_session_context,
    open_scope_session_context,
)
from mindroom.history.storage import (
    update_scope_seen_event_ids,
)
from mindroom.history.types import HistoryScope, PreparedHistoryState
from mindroom.memory import MemoryPromptParts
from mindroom.teams import prepare_materialized_team_execution
from mindroom.token_budget import estimate_text_tokens, stable_serialize
from tests.conftest import (
    FakeModel,
    bind_runtime_paths,
    make_turn_context,
    make_visible_message,
    test_runtime_paths,
)
from tests.history_helpers import (  # noqa: F401
    _ALL_HISTORY_SETTINGS,
    RecordingModel,
    _agent,
    _close_test_storages,
    _completed_run,
    _make_config,
    _runtime_paths,
    _session,
)
from tests.identity_helpers import persist_entity_accounts


def test_create_agent_enables_agno_native_history_replay(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path, num_history_runs=2)

    with patch("mindroom.model_loading.get_model_instance", return_value=FakeModel(id="fake-model", provider="fake")):
        agent = create_agent(
            "test_agent",
            config,
            runtime_paths,
            execution_identity=None,
            include_interactive_questions=False,
        )

    assert agent.add_history_to_context is True
    assert agent.num_history_runs == 2
    assert agent.num_history_messages is None
    assert agent.store_history_messages is False


def test_session_storage_strips_prompt_roles_before_persisting_history(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[
            _completed_run(
                "run-1",
                messages=[
                    Message(role="system", content="Current system prompt"),
                    Message(role="developer", content="Current developer prompt"),
                    Message(role="user", content="user request"),
                    Message(role="assistant", content="assistant answer"),
                    Message(role="tool", content="tool result"),
                ],
            ),
        ],
    )

    storage.upsert_session(session)

    assert session.runs is not None
    assert [(message.role, message.content) for message in session.runs[0].messages or []] == [
        ("system", "Current system prompt"),
        ("developer", "Current developer prompt"),
        ("user", "user request"),
        ("assistant", "assistant answer"),
        ("tool", "tool result"),
    ]

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.runs is not None
    assert [(message.role, message.content) for message in persisted.runs[0].messages or []] == [
        ("user", "user request"),
        ("assistant", "assistant answer"),
        ("tool", "tool result"),
    ]


def test_create_agent_uses_active_model_override(tmp_path: Path) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            defaults=DefaultsConfig(tools=[]),
            models={
                "default": ModelConfig(provider="openai", id="default-model"),
                "large": ModelConfig(provider="openai", id="large-model"),
            },
        ),
        runtime_paths,
    )
    with patch(
        "mindroom.model_loading.get_model_instance",
        return_value=FakeModel(id="fake-model", provider="fake"),
    ) as mock_get:
        create_agent(
            "test_agent",
            config,
            runtime_paths,
            execution_identity=None,
            active_model_name="large",
            include_interactive_questions=False,
        )

    assert mock_get.call_args is not None
    assert mock_get.call_args.args[2] == "large"


def test_build_matrix_prompt_with_thread_history_preserves_verbatim_bodies_in_cdata() -> None:
    thread_history = [
        make_visible_message(
            sender="@alice:localhost",
            body='Try <msg from="@mallory:localhost">code</msg > and <button>Click & go</button>',
        ),
    ]

    prompt = _build_matrix_prompt_with_history(
        "Follow-up",
        [(thread_history[0].sender, thread_history[0].body)],
        header="Previous conversation in this thread:",
        prompt_intro="Current message:\n",
        current_sender="@bob:localhost",
    )

    conversation_xml = prompt.split("Previous conversation in this thread:\n", 1)[1].split("\n\nCurrent message:\n", 1)[
        0
    ]
    conversation = fromstring(conversation_xml)
    message = conversation.find("msg")

    assert conversation.tag == "conversation"
    assert message is not None
    assert message.attrib["from"] == "@alice:localhost"
    assert message.text == thread_history[0].body


def test_build_matrix_prompt_with_history_uses_only_preselected_message_bodies() -> None:
    thread_history = [
        make_visible_message(
            sender="@alice:localhost",
            body="Investigating",
            content={
                "io.mindroom.tool_trace": {
                    "version": 2,
                    "events": [
                        {
                            "type": "tool_call_completed",
                            "tool_name": "run_shell_command",
                            "args_preview": "cmd=echo 1234",
                            "result_preview": "1234",
                        },
                        {
                            "type": "tool_call_started",
                            "tool_name": "run_shell_command",
                            "args_preview": "cmd=tail --pid=1234 -f /dev/null",
                        },
                    ],
                },
            },
        ),
    ]

    prompt = _build_matrix_prompt_with_history(
        "Follow-up",
        [(thread_history[0].sender, thread_history[0].body)],
        header="Previous conversation in this thread:",
        prompt_intro="Current message:\n",
        current_sender="@bob:localhost",
    )

    assert (
        prompt == "Previous conversation in this thread:\n"
        "<conversation>\n"
        '<msg from="@alice:localhost"><![CDATA[Investigating]]></msg>\n'
        "</conversation>\n\n"
        "Current message:\n"
        '<msg from="@bob:localhost"><![CDATA[Follow-up]]></msg>'
    )


def test_build_matrix_prompt_with_history_renders_preselected_message_body() -> None:
    thread_history = [make_visible_message(sender="@alice:localhost", body="Earlier context")]

    prompt = _build_matrix_prompt_with_history(
        "Follow-up",
        [(thread_history[0].sender, thread_history[0].body)],
        header="Previous conversation in this thread:",
        prompt_intro="Current message:\n",
        current_sender="@bob:localhost",
    )

    assert (
        prompt == "Previous conversation in this thread:\n"
        "<conversation>\n"
        '<msg from="@alice:localhost"><![CDATA[Earlier context]]></msg>\n'
        "</conversation>\n\n"
        "Current message:\n"
        '<msg from="@bob:localhost"><![CDATA[Follow-up]]></msg>'
    )


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_budgets_persisted_replay_against_primary_prompt(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    live_agent = _agent()
    live_agent.role = "Verbose role " + ("r" * 200)
    thread_history = [
        make_visible_message(sender="alice", body="Earlier context"),
        make_visible_message(sender="bob", body="More context"),
    ]

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_prepare,
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(),
        ),
    ):
        await _prepare_agent_and_prompt(
            make_turn_context("test_agent"),
            prompt="Current prompt",
            runtime_paths=runtime_paths,
            config=config,
            thread_history=thread_history,
        )

    assert mock_prepare.await_args is not None
    assert mock_prepare.await_args.kwargs["resolved_inputs"].static_prompt_tokens == estimate_agent_static_tokens(
        live_agent,
        "Current prompt",
    )


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_uses_room_resolved_agent_model_for_execution_and_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            defaults=DefaultsConfig(tools=[]),
            room_models={"lobby": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model", context_window=None),
                "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
            },
        ),
        runtime_paths,
    )
    monkeypatch.setattr("mindroom.matrix.state.get_room_alias_from_id", lambda *_args: "lobby")
    live_agent = _agent()

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent) as mock_create_agent,
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_prepare,
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(),
        ),
    ):
        await _prepare_agent_and_prompt(
            make_turn_context("test_agent", room_id="!room:localhost"),
            prompt="Current prompt",
            runtime_paths=runtime_paths,
            config=config,
        )

    assert mock_create_agent.call_args is not None
    assert mock_create_agent.call_args.kwargs["active_model_name"] == "large"
    assert mock_prepare.await_args is not None
    assert mock_prepare.await_args.kwargs["resolved_inputs"].active_model_name == "large"
    assert mock_prepare.await_args.kwargs["resolved_inputs"].active_context_window == 48_000


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_prefers_explicit_turn_model_over_room_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", model="default")},
            defaults=DefaultsConfig(tools=[]),
            room_models={"lobby": "large"},
            models={
                "default": ModelConfig(provider="openai", id="default-model"),
                "large": ModelConfig(provider="openai", id="large-model", context_window=48_000),
                "call_fast": ModelConfig(provider="openai", id="fast-model", context_window=16_000),
            },
        ),
        runtime_paths,
    )
    monkeypatch.setattr("mindroom.matrix.state.get_room_alias_from_id", lambda *_args: "lobby")
    live_agent = _agent()
    turn = replace(
        make_turn_context("test_agent", room_id="!room:localhost"),
        active_model_name="call_fast",
    )

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent) as mock_create_agent,
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ) as mock_prepare,
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(),
        ),
    ):
        prepared = await _prepare_agent_and_prompt(
            turn,
            prompt="Current prompt",
            runtime_paths=runtime_paths,
            config=config,
        )

    assert prepared.runtime_model_name == "call_fast"
    assert mock_create_agent.call_args.kwargs["active_model_name"] == "call_fast"
    resolved_inputs = mock_prepare.await_args.kwargs["resolved_inputs"]
    assert resolved_inputs.active_model_name == "call_fast"
    assert resolved_inputs.active_context_window == 16_000


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_uses_thread_history_and_transient_memory_when_replay_is_disabled(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    live_agent = _agent()
    thread_history = [
        make_visible_message(sender="alice", body="Earlier context", event_id="$earlier"),
        make_visible_message(sender="bob", body="More context", event_id="$more"),
    ]

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch(
            "mindroom.ai.build_memory_prompt_parts",
            new=AsyncMock(
                return_value=MemoryPromptParts(
                    transient_turn_context="Retrieved memory for this turn",
                ),
            ),
        ),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(replays_persisted_history=False),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            make_turn_context("test_agent"),
            prompt="Current prompt",
            runtime_paths=runtime_paths,
            config=config,
            thread_history=thread_history,
        )

    prepared_agent = prepared_run.agent
    full_prompt = prepared_run.prompt_text
    prepared = prepared_run.prepared_history
    assert prepared_agent is live_agent
    assert prepared.replays_persisted_history is False
    assert [message.content for message in prepared_run.messages] == [
        '<msg event_id="$earlier" from="alice"><![CDATA[Earlier context]]></msg>',
        '<msg event_id="$more" from="bob"><![CDATA[More context]]></msg>',
        "Retrieved memory for this turn",
        "Current prompt",
    ]
    assert [message.add_to_agent_memory for message in prepared_run.messages] == [True, True, False, True]
    assert full_prompt == (
        '<msg event_id="$earlier" from="alice"><![CDATA[Earlier context]]></msg>\n\n'
        '<msg event_id="$more" from="bob"><![CDATA[More context]]></msg>\n\n'
        "Retrieved memory for this turn\n\nCurrent prompt"
    )


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_caps_thread_fallback_to_active_window(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(reserve_tokens=0),
        context_window=32,
    )
    live_agent = _agent()
    thread_history = [
        make_visible_message(sender="alice", body="Old context " + ("o" * 120), event_id="$old"),
        make_visible_message(sender="bob", body="Recent context", event_id="$recent"),
    ]

    class FakeAgentStaticTokenEstimator:
        def __init__(self, agent: Agent) -> None:
            assert agent is live_agent

        def estimate(self, full_prompt: str) -> int:
            return estimate_text_tokens(full_prompt)

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch("mindroom.execution_preparation.agent_static_token_estimator", FakeAgentStaticTokenEstimator),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(replays_persisted_history=False),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            make_turn_context("test_agent"),
            prompt="Current prompt",
            runtime_paths=runtime_paths,
            config=config,
            thread_history=thread_history,
        )

    assert prepared_run.prompt_text == (
        '<msg event_id="$recent" from="bob"><![CDATA[Recent context]]></msg>\n\nCurrent prompt'
    )
    assert estimate_text_tokens(prepared_run.prompt_text) <= 32


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_uses_full_thread_fallback_for_threaded_missing_replay(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    persist_entity_accounts(config, runtime_paths, usernames={"test_agent": "bot"})
    live_agent = _agent()
    thread_history = [
        make_visible_message(sender="@alice:localhost", body="Original question", event_id="$root"),
        make_visible_message(sender="@bot:localhost", body="Prior diagnosis", event_id="$agent-reply"),
        make_visible_message(sender="@alice:localhost", body="What was that?", event_id="$current"),
        make_visible_message(sender="@carol:localhost", body="Later reaction", event_id="$later"),
    ]

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(replays_persisted_history=False),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            make_turn_context(
                "test_agent",
                reply_to_event_id="$current",
                requester_id="@alice:localhost",
            ),
            prompt="What was that?",
            runtime_paths=runtime_paths,
            config=config,
            thread_history=thread_history,
        )

    assert prepared_run.prepared_history.replays_persisted_history is False
    assert prepared_run.prompt_text == (
        '<msg event_id="$root" from="@alice:localhost"><![CDATA[Original question]]></msg>\n\n'
        '<msg event_id="$agent-reply" from="@bot:localhost"><![CDATA[Prior diagnosis]]></msg>\n\n'
        "Current message:\n"
        '<msg from="@alice:localhost"><![CDATA[What was that?]]></msg>'
    )
    assert "Later reaction" not in prepared_run.prompt_text


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_trims_oversized_full_thread_fallback(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path, context_window=1_000)
    live_agent = _agent()
    thread_history = [
        make_visible_message(
            sender="@alice:localhost",
            body="obsolete context " + ("x" * 20_000),
            event_id="$old",
        ),
        make_visible_message(
            sender="@bob:localhost",
            body="Recent context to keep.",
            event_id="$recent",
        ),
    ]

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(replays_persisted_history=False),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            make_turn_context("test_agent"),
            prompt="Current prompt",
            runtime_paths=runtime_paths,
            config=config,
            thread_history=thread_history,
        )

    assert prepared_run.prepared_history.replays_persisted_history is False
    assert "Recent context to keep." in prepared_run.prompt_text
    assert "obsolete context" not in prepared_run.prompt_text


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_skips_thread_fallback_for_summary_only_replay(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[],
        summary=SessionSummary(summary="Compacted summary", updated_at=datetime.now(UTC)),
    )
    storage.upsert_session(session)
    live_agent = _agent()
    thread_history = [
        make_visible_message(sender="@alice:localhost", body="Original context", event_id="$root"),
        make_visible_message(sender="@bot:localhost", body="Prior answer", event_id="$agent-reply"),
    ]

    with (
        open_scope_session_context(
            agent=live_agent,
            agent_name="test_agent",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
        ) as scope_context,
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            make_turn_context("test_agent", session_id="session-1"),
            prompt="Current prompt",
            runtime_paths=runtime_paths,
            config=config,
            scope_context=scope_context,
            thread_history=thread_history,
        )

    assert prepared_run.prepared_history.replays_persisted_history is True
    assert prepared_run.prompt_text == "Current prompt"
    assert "Original context" not in prepared_run.prompt_text
    assert "Prior answer" not in prepared_run.prompt_text


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_keeps_matrix_current_sender_when_persisted_replay_is_enabled(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    live_agent = _agent()

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch(
            "mindroom.execution_preparation.prepare_scope_history",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "mindroom.execution_preparation.finalize_history_preparation",
            return_value=PreparedHistoryState(replays_persisted_history=True),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            make_turn_context("test_agent", requester_id="@alice:localhost"),
            prompt="Current prompt",
            runtime_paths=runtime_paths,
            config=config,
        )

    prepared_agent = prepared_run.agent
    full_prompt = prepared_run.prompt_text
    prepared = prepared_run.prepared_history
    assert prepared_agent is live_agent
    assert prepared.replays_persisted_history is True
    assert full_prompt == 'Current message:\n<msg from="@alice:localhost"><![CDATA[Current prompt]]></msg>'


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_keeps_outcomes_empty_when_no_compaction_occurred(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    live_agent = _agent()
    prepared_execution = _PreparedExecutionContext(
        messages=(Message(role="user", content="Current prompt"),),
        unseen_event_ids=[],
        prepared_history=PreparedHistoryState(),
    )

    with (
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch(
            "mindroom.ai.prepare_agent_execution_context",
            new=AsyncMock(return_value=prepared_execution),
        ),
    ):
        prepared_run = await _prepare_agent_and_prompt(
            make_turn_context("test_agent"),
            prompt="Current prompt",
            runtime_paths=runtime_paths,
            config=config,
        )

    prepared = prepared_run.prepared_history
    assert prepared.compaction_outcomes == []


@pytest.mark.asyncio
async def test_native_agno_replays_recent_raw_history_without_persisting_replay(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    storage.upsert_session(
        _session(
            "session-1",
            runs=[
                _completed_run("run-1"),
                _completed_run("run-2"),
            ],
            summary=SessionSummary(summary="stored summary", updated_at=datetime.now(UTC)),
        ),
    )
    model = RecordingModel(id="recording-model", provider="fake")
    agent = _agent(
        model=model,
        db=storage,
        num_history_runs=1,
    )

    response = await agent.arun("Current prompt", session_id="session-1")

    assert response.content == "ok"
    assert [message.role for message in model.seen_messages[:2]] == ["user", "assistant"]
    assert "stored summary" not in str(model.seen_messages)
    assert [message.content for message in model.seen_messages[:2]] == [
        "run-2 question",
        "run-2 answer",
    ]
    assert [message.from_history for message in model.seen_messages[:2]] == [True, True]
    assert model.seen_messages[-1].role == "user"
    assert model.seen_messages[-1].content == "Current prompt"

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    latest_run = persisted.runs[-1]
    assert isinstance(latest_run, RunOutput)
    assert [message.content for message in latest_run.messages or []] == [
        "Current prompt",
        "ok",
    ]
    assert all(message.from_history is False for message in latest_run.messages or [])
    assert latest_run.additional_input in (None, [])


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_uses_native_history_with_unseen_thread_context(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path, num_history_runs=1)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session(
        "session-1",
        runs=[_completed_run("run-1"), _completed_run("run-2")],
        summary=SessionSummary(summary="stored summary", updated_at=datetime.now(UTC)),
    )
    update_scope_seen_event_ids(session, HistoryScope(kind="agent", scope_id="test_agent"), ["event-1"])
    storage.upsert_session(session)

    recording_model = RecordingModel(id="recording-model", provider="fake")
    live_agent = _agent(model=recording_model, db=storage, num_history_runs=1)

    with open_scope_session_context(
        agent=live_agent,
        agent_name="test_agent",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    ) as scope_context:
        assert scope_context is not None
        with (
            patch("mindroom.ai.create_agent", return_value=live_agent),
            patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        ):
            prepared_run = await _prepare_agent_and_prompt(
                make_turn_context("test_agent", reply_to_event_id="event-3"),
                prompt="Current prompt",
                runtime_paths=runtime_paths,
                config=config,
                scope_context=scope_context,
                thread_history=[
                    make_visible_message(event_id="event-1", sender="alice", body="Already seen"),
                    make_visible_message(event_id="event-2", sender="alice", body="Fresh follow-up"),
                    make_visible_message(event_id="event-3", sender="alice", body="Current message body"),
                ],
            )

    agent = prepared_run.agent
    full_prompt = prepared_run.prompt_text
    unseen_event_ids = prepared_run.unseen_event_ids
    prepared = prepared_run.prepared_history
    assert unseen_event_ids == ["event-2"]
    assert prepared.replays_persisted_history is True
    assert "Fresh follow-up" in full_prompt
    assert "Already seen" not in full_prompt
    assert "stored summary" not in full_prompt
    assert "<history_context>" not in full_prompt

    response = await agent.arun(prepared_run.run_input, session_id="session-1")

    assert response.content == "ok"
    assert [message.role for message in recording_model.seen_messages[:2]] == ["user", "assistant"]
    assert "stored summary" not in str(recording_model.seen_messages)
    assert [message.content for message in recording_model.seen_messages[:2]] == [
        "run-2 question",
        "run-2 answer",
    ]

    unseen_user_message = recording_model.seen_messages[-2]
    assert unseen_user_message.role == "user"
    assert unseen_user_message.content == '<msg event_id="event-2" from="alice"><![CDATA[Fresh follow-up]]></msg>'

    final_user_message = recording_model.seen_messages[-1]
    assert final_user_message.role == "user"
    assert final_user_message.content == "Current prompt"


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_keeps_transient_memory_out_of_replay_and_compaction(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path, num_history_runs=10)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    recording_model = RecordingModel(id="recording-model", provider="fake")
    recorded_requests: list[list[dict[str, object]]] = []

    prompt_parts_by_prompt = {
        "First prompt": MemoryPromptParts(
            session_preamble="[File memory entrypoint (agent)]\nStable MEMORY.md",
            transient_turn_context="turn context one",
        ),
        "Second prompt": MemoryPromptParts(
            session_preamble="[File memory entrypoint (agent)]\nStable MEMORY.md",
            transient_turn_context="turn context two",
        ),
        "Third prompt": MemoryPromptParts(
            session_preamble="[File memory entrypoint (agent)]\nStable MEMORY.md",
            transient_turn_context="turn context three",
        ),
    }

    async def fake_build_memory_prompt_parts(
        prompt: str,
        *_args: object,
        **_kwargs: object,
    ) -> MemoryPromptParts:
        return prompt_parts_by_prompt[prompt]

    def create_agent_stub(*_args: object, **_kwargs: object) -> Agent:
        return _agent(
            model=recording_model,
            db=storage,
            num_history_runs=10,
        )

    with (
        patch(
            "mindroom.ai.create_agent",
            side_effect=create_agent_stub,
        ),
        patch(
            "mindroom.ai.build_memory_prompt_parts",
            new=AsyncMock(side_effect=fake_build_memory_prompt_parts),
        ),
    ):
        for prompt in ("First prompt", "Second prompt", "Third prompt"):
            prepared_run = await _prepare_agent_and_prompt(
                make_turn_context("test_agent", session_id="session-1"),
                prompt=prompt,
                runtime_paths=runtime_paths,
                config=config,
            )
            await prepared_run.agent.arun(prepared_run.run_input, session_id="session-1")
            recorded_requests.append(
                [
                    {
                        "role": message.role,
                        "content": str(message.content),
                        "add_to_agent_memory": message.add_to_agent_memory,
                    }
                    for message in recording_model.seen_messages
                ],
            )

    second_request = recorded_requests[1]
    third_request = recorded_requests[2]

    assert stable_serialize(second_request[:-2]) == stable_serialize(third_request[: len(second_request) - 2])
    session_preamble = "[File memory entrypoint (agent)]\nStable MEMORY.md"
    assert [message["content"] for message in recorded_requests[0]] == [
        session_preamble,
        "turn context one",
        "First prompt",
    ]
    assert [message["content"] for message in second_request] == [
        session_preamble,
        "First prompt",
        "ok",
        "turn context two",
        "Second prompt",
    ]
    assert [message["content"] for message in third_request] == [
        session_preamble,
        "First prompt",
        "ok",
        "Second prompt",
        "ok",
        "turn context three",
        "Third prompt",
    ]
    assert third_request[-2]["add_to_agent_memory"] is False
    assert third_request[-1]["add_to_agent_memory"] is True

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    persisted_contents = [str(message.content) for run in persisted.runs or [] for message in run.messages or []]
    assert persisted_contents == ["First prompt", "ok", "Second prompt", "ok", "Third prompt", "ok"]

    summary_input, included_runs = _build_summary_input(
        previous_summary=None,
        compacted_runs=persisted.runs or [],
        max_input_tokens=10_000,
        history_settings=_ALL_HISTORY_SETTINGS,
    )
    assert included_runs == persisted.runs
    assert "turn context" not in summary_input
    assert "First prompt" in summary_input
    assert "Third prompt" in summary_input


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_timestamps_current_turn_without_duplication(
    tmp_path: Path,
) -> None:
    config, runtime_paths = _make_config(tmp_path, num_history_runs=10)
    config.timezone = "America/Los_Angeles"
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    recording_model = RecordingModel(id="recording-model", provider="fake")
    recorded_requests: list[list[dict[str, object]]] = []

    prompt_parts_by_prompt = {
        "First prompt": MemoryPromptParts(
            session_preamble="[File memory entrypoint (agent)]\nStable MEMORY.md",
            transient_turn_context="turn context one",
        ),
        "Second prompt": MemoryPromptParts(
            session_preamble="[File memory entrypoint (agent)]\nStable MEMORY.md",
            transient_turn_context="turn context two",
        ),
        "Third prompt": MemoryPromptParts(
            session_preamble="[File memory entrypoint (agent)]\nStable MEMORY.md",
            transient_turn_context="turn context three",
        ),
    }
    model_prompt_by_prompt = {
        "First prompt": "First prompt\n\nAvailable attachment IDs: att_1. Use tool calls to inspect or process them.",
        "Second prompt": "Second prompt\n\nAvailable attachment IDs: att_2. Use tool calls to inspect or process them.",
        "Third prompt": "Third prompt\n\nAvailable attachment IDs: att_3. Use tool calls to inspect or process them.",
    }
    timestamp_by_prompt = {
        "First prompt": 1_774_019_700_000,
        "Second prompt": 1_774_019_760_000,
        "Third prompt": 1_774_019_820_000,
    }

    async def fake_build_memory_prompt_parts(
        prompt: str,
        *_args: object,
        **_kwargs: object,
    ) -> MemoryPromptParts:
        return prompt_parts_by_prompt[prompt]

    def create_agent_stub(*_args: object, **_kwargs: object) -> Agent:
        return _agent(
            model=recording_model,
            db=storage,
            num_history_runs=10,
        )

    with (
        patch(
            "mindroom.ai.create_agent",
            side_effect=create_agent_stub,
        ),
        patch(
            "mindroom.ai.build_memory_prompt_parts",
            new=AsyncMock(side_effect=fake_build_memory_prompt_parts),
        ),
    ):
        for prompt in ("First prompt", "Second prompt", "Third prompt"):
            prepared_run = await _prepare_agent_and_prompt(
                make_turn_context(
                    "test_agent",
                    session_id="session-1",
                    requester_id="@alice:localhost",
                ),
                prompt=prompt,
                runtime_paths=runtime_paths,
                config=config,
                model_prompt=model_prompt_by_prompt[prompt],
                current_timestamp_ms=timestamp_by_prompt[prompt],
            )
            await prepared_run.agent.arun(prepared_run.run_input, session_id="session-1")
            recorded_requests.append(
                [
                    {
                        "role": message.role,
                        "content": str(message.content),
                        "add_to_agent_memory": message.add_to_agent_memory,
                    }
                    for message in recording_model.seen_messages
                ],
            )

    second_request = recorded_requests[1]
    third_request = recorded_requests[2]

    assert stable_serialize(second_request[:-2]) == stable_serialize(third_request[: len(second_request) - 2])
    assert third_request[-2] == {
        "role": "user",
        "content": "turn context three",
        "add_to_agent_memory": False,
    }
    assert third_request[-1]["content"] == (
        'Current message:\n<msg from="@alice:localhost" ts="2026-03-20 08:17 PDT"><![CDATA['
        "Third prompt]]></msg>\n\n"
        "Available attachment IDs: att_3. Use tool calls to inspect or process them."
    )
    assert third_request[-1]["add_to_agent_memory"] is True


_HOME_LOCATION_TEXT = "status: fresh\nlatitude: 52.3702\nlongitude: 4.8952\nnearby_place: Home\nat_home: true"
_OFFICE_LOCATION_TEXT = (
    _HOME_LOCATION_TEXT.replace("nearby_place: Home", "nearby_place: Office")
    .replace("at_home: true", "at_home: false")
    .replace("latitude: 52.3702", "latitude: 52.3800")
)


@pytest.mark.asyncio
async def test_prepare_agent_and_prompt_persists_location_markers_only_on_change(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path, num_history_runs=10)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    recording_model = RecordingModel(id="recording-model", provider="fake")
    live_agent = _agent(model=recording_model, db=storage, num_history_runs=10)
    recorded_requests: list[list[dict[str, object]]] = []
    recorded_markers: list[str | None] = []

    turns: list[tuple[str, str | None, str]] = [
        ("First prompt", _HOME_LOCATION_TEXT, "$turn-1"),
        ("Second prompt", _HOME_LOCATION_TEXT, "$turn-2"),
        ("Third prompt", None, "$turn-3"),
        ("Fourth prompt", _HOME_LOCATION_TEXT, "$turn-4"),
        ("Fifth prompt", _OFFICE_LOCATION_TEXT, "$turn-5"),
    ]

    with (
        patch("mindroom.ai.create_agent", return_value=live_agent),
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
    ):
        for prompt, location_item_text, event_id in turns:
            with open_scope_session_context(
                agent=live_agent,
                agent_name="test_agent",
                session_id="session-1",
                runtime_paths=runtime_paths,
                config=config,
                execution_identity=None,
            ) as scope_context:
                prepared_run = await _prepare_agent_and_prompt(
                    make_turn_context(
                        "test_agent",
                        session_id="session-1",
                        requester_id="@alice:localhost",
                        location_item_text=location_item_text,
                    ),
                    prompt=prompt,
                    runtime_paths=runtime_paths,
                    config=config,
                    scope_context=scope_context,
                    current_event_id=event_id,
                )
            recorded_markers.append(prepared_run.location_marker)
            # The ai layer merges the accepted marker into the run metadata that
            # Agno persists with the run; mirror that seam here.
            run_metadata = (
                {MINDROOM_LOCATION_MARKER_METADATA_KEY: prepared_run.location_marker}
                if prepared_run.location_marker is not None
                else None
            )
            await prepared_run.agent.arun(prepared_run.run_input, session_id="session-1", metadata=run_metadata)
            recorded_requests.append(
                [
                    {
                        "content": str(message.content),
                        "add_to_agent_memory": message.add_to_agent_memory,
                    }
                    for message in recording_model.seen_messages
                ],
            )

    assert recorded_markers == ["📍 Home", None, None, None, "📍 52.3800, 4.8952"]

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    replayed_contents = "\n".join(
        str(message.content) for run in persisted.runs or [] for message in run.messages or []
    )
    assert replayed_contents.count("📍 Home") == 1
    assert replayed_contents.count("📍 52.3800, 4.8952") == 1
    # The marker is system-generated, so it stays outside the event-tagged
    # CDATA that mirrors the wire body.
    assert "📍 Home]]></msg>" not in replayed_contents
    assert replayed_contents.count("]]></msg>\n\n📍") == 2
    for turn_number in range(1, 6):
        assert f'event_id="$turn-{turn_number}"' in replayed_contents
    # The full location detail must not survive anywhere in the serialized
    # session, including the persisted run input Agno captures.
    serialized_session = json.dumps(persisted.to_dict(), ensure_ascii=False, default=str)
    for full_detail_fragment in ("latitude", "longitude", "nearby_place", "at_home", "mindroom_message_context"):
        assert full_detail_fragment not in serialized_session
    assert "[Matrix metadata for tool calls]" not in serialized_session

    for turn_index in (0, 1, 3, 4):
        transient_message = recorded_requests[turn_index][-2]
        assert transient_message["add_to_agent_memory"] is False
        assert '<item key="location"' in cast("str", transient_message["content"])
    assert all('<item key="location"' not in cast("str", message["content"]) for message in recorded_requests[2])

    summary_input, _ = _build_summary_input(
        previous_summary=None,
        compacted_runs=list(persisted.runs or []),
        max_input_tokens=100_000,
        history_settings=_ALL_HISTORY_SETTINGS,
    )
    # The trusted marker metadata is omitted from compaction input, so each
    # location delta appears exactly once, and never the full detail.
    assert summary_input.count("📍 Home") == 1
    assert summary_input.count("📍 52.3800, 4.8952") == 1
    assert "latitude" not in summary_input
    assert "nearby_place" not in summary_input
    assert MINDROOM_LOCATION_MARKER_METADATA_KEY not in summary_input


def test_session_storage_scrubs_transient_run_input(tmp_path: Path) -> None:
    """Current-turn-only messages never survive in the persisted run input."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    run = _completed_run(
        "run-1",
        messages=[
            Message(role="user", content="user request"),
            Message(role="assistant", content="assistant answer"),
        ],
    )
    run.input = RunInput(
        input_content=[
            Message(role="user", content="user request"),
            Message(role="user", content="latitude: 52.3702", add_to_agent_memory=False),
        ],
    )

    storage.upsert_session(_session("session-1", runs=[run]))

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.runs is not None
    serialized = json.dumps(persisted.to_dict(), ensure_ascii=False, default=str)
    assert "latitude" not in serialized
    assert "user request" in serialized
    # The caller's in-memory run keeps its live transient input untouched.
    assert run.input is not None
    assert isinstance(run.input.input_content, list)
    assert len(run.input.input_content) == 2


@pytest.mark.asyncio
async def test_team_location_detail_stays_current_turn_only_in_durable_storage(tmp_path: Path) -> None:
    """A real team run sees full location live while durable state keeps only the marker."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "agents": {
                    "code": {"display_name": "CodeAgent", "role": "Write code"},
                    "research": {"display_name": "ResearchAgent", "role": "Do research"},
                },
                "models": {"default": {"provider": "ollama", "id": "test-model"}},
            },
        ),
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths)
    team_model = RecordingModel(id="team-recording-model", provider="fake")
    member_agents = [
        Agent(id="code", name="CodeAgent", model=FakeModel(id="fake-model", provider="fake")),
        Agent(id="research", name="ResearchAgent", model=FakeModel(id="fake-model", provider="fake")),
    ]

    with open_bound_scope_session_context(
        agents=member_agents,
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    ) as scope_context:
        assert scope_context is not None
        team = Team(
            id="team-code-research",
            name="Code + Research",
            members=member_agents,
            model=team_model,
            db=scope_context.storage,
            store_history_messages=False,
        )
        prepared = await prepare_materialized_team_execution(
            make_turn_context(
                "team-code-research",
                session_id="session-1",
                requester_id="@alice:localhost",
                current_event_id="$turn-1",
                location_item_text=_HOME_LOCATION_TEXT,
            ),
            scope_context=scope_context,
            agents=member_agents,
            team=team,
            message="Where am I?",
            thread_history=[],
            config=config,
            runtime_paths=runtime_paths,
            runtime_model=config.resolve_runtime_model(entity_name=None),
            response_sender_id="@mindroom_code:localhost",
            current_sender_id="@alice:localhost",
            configured_team_name=None,
        )

        assert prepared.location_marker == "📍 Home"
        assert prepared.run_metadata is not None
        assert prepared.run_metadata[MINDROOM_LOCATION_MARKER_METADATA_KEY] == "📍 Home"
        flattened_input = prepared.prepared_prompt
        assert "📍 Home" in flattened_input
        assert "latitude" not in flattened_input

        response = await team.arun(
            flattened_input,
            session_id="session-1",
            metadata=prepared.run_metadata,
        )
        assert response is not None

        # The live model saw the full detail through the volatile system tail.
        live_system_messages = "\n".join(
            str(message.content) for message in team_model.seen_messages if message.role == "system"
        )
        assert '<item key="location"' in live_system_messages
        assert "latitude: 52.3702" in live_system_messages

        persisted = get_team_session(cast("Any", scope_context.storage), "session-1")
        assert persisted is not None
        serialized_session = json.dumps(persisted.to_dict(), ensure_ascii=False, default=str)
        # Durable team state keeps only the short marker: no coordinates, no
        # enrichment markup, in the team run, member responses, or run input.
        for full_detail_fragment in (
            "latitude",
            "longitude",
            "nearby_place",
            "at_home",
            "mindroom_message_context",
        ):
            assert full_detail_fragment not in serialized_session
        replayed_contents = "\n".join(
            str(message.content) for run in persisted.runs or [] for message in run.messages or []
        )
        assert replayed_contents.count("📍 Home") == 1
