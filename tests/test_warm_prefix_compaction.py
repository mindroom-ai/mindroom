"""Tests for warm-prefix compaction summary requests.

When the compaction summary model is the active reply model, the summary call
reproduces the reply-path request prefix (system prompt, tool schemas, history
runs) and appends one summary instruction, so providers serve the prefix from
their prompt cache instead of re-reading it at full price.
"""
# ruff: noqa: D103, TC003

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.anthropic import Claude
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary

from mindroom.agent_storage import create_session_storage, get_agent_session
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.constants import MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS, RuntimePaths, resolve_runtime_paths
from mindroom.history.agno_forked_request import build_agent_provider_request_from_runs
from mindroom.history.compaction import _warm_summary_instruction, compact_scope_history
from mindroom.history.runtime import _warm_prefix_summary_context
from mindroom.history.storage import read_scope_state, write_scope_state
from mindroom.history.summary_call import (
    SUMMARY_MAX_OUTPUT_TOKENS,
    SummaryProviderRequest,
    configure_summary_model,
    generate_compaction_summary,
)
from mindroom.history.types import HistoryPolicy, HistoryScope, HistoryScopeState, ResolvedHistorySettings
from mindroom.history.warm_prefix import WarmPrefixSummaryContext, build_warm_prefix_summary_request
from mindroom.prompts import COMPACTION_SUMMARY_PROMPT, COMPACTION_WARM_SUMMARY_INSTRUCTION
from tests.conftest import FakeModel, bind_runtime_paths

if TYPE_CHECKING:
    from agno.tools.function import Function

_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")
_HISTORY_SETTINGS = ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None)


class _RecordingFakeModel(FakeModel):
    """FakeModel double that records aresponse keyword arguments."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.seen_messages: list[Message] = []
        self.seen_tools: list[Function | dict] | None = None
        self.seen_tool_choice: str | dict[str, object] | None = None

    async def aresponse(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            self.seen_messages = list(messages)
        self.seen_tools = kwargs.get("tools")  # type: ignore[assignment]
        self.seen_tool_choice = kwargs.get("tool_choice")  # type: ignore[assignment]
        return ModelResponse(content="warm summary")


def _lookup_weather(city: str) -> str:
    """Return fake weather for one city."""
    return f"sunny in {city}"


def _completed_run(run_id: str, marker: str) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        agent_id="test_agent",
        status=RunStatus.completed,
        messages=[
            Message(role="user", content=f"{marker} question"),
            Message(role="assistant", content=f"{marker} answer"),
        ],
    )


def _session(runs: list[RunOutput]) -> AgentSession:
    return AgentSession(
        session_id="session-1",
        agent_id="test_agent",
        runs=list(runs),
        metadata=None,
        created_at=1,
        updated_at=1,
    )


def _agent(model: FakeModel | None = None) -> Agent:
    return Agent(
        id="test_agent",
        name="Test Agent",
        model=model or FakeModel(id="fake-model", provider="fake"),
        tools=[_lookup_weather],
        add_history_to_context=True,
        store_history_messages=False,
    )


def _make_config(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": AgentConfig(
                    display_name="Test Agent",
                    compaction=CompactionOverrideConfig(enabled=True),
                ),
            },
            defaults=DefaultsConfig(tools=[], compaction=CompactionConfig()),
            models={
                "default": ModelConfig(provider="openai", id="test-model", context_window=64_000),
            },
        ),
        runtime_paths,
    )
    return config, runtime_paths


# --- Warm-prefix model configuration (invariant 3 extension) ------------------


def test_configure_summary_model_warm_prefix_preserves_cache_and_thinking() -> None:
    model = Claude(
        id="claude-sonnet-4-6",
        cache_system_prompt=True,
        extended_cache_time=True,
        thinking={"type": "enabled", "budget_tokens": 8192},
        max_tokens=64_000,
        timeout=3600.0,
        client_params={"max_retries": 2},
    )

    configure_summary_model(model, reuses_reply_prefix=True)

    assert model.cache_system_prompt is True
    assert model.extended_cache_time is True
    assert model.thinking == {"type": "enabled", "budget_tokens": 8192}
    assert model.max_tokens == 64_000
    assert model.timeout == MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS
    assert model.client_params == {"max_retries": 0}


def test_configure_summary_model_warm_prefix_caps_output_without_thinking() -> None:
    model = Claude(id="claude-sonnet-4-6", cache_system_prompt=True, max_tokens=64_000)

    configure_summary_model(model, reuses_reply_prefix=True)

    assert model.cache_system_prompt is True
    assert model.max_tokens == SUMMARY_MAX_OUTPUT_TOKENS


# --- Warm request construction -------------------------------------------------


@pytest.mark.asyncio
async def test_forked_request_preserves_run_history_and_appends_instruction() -> None:
    agent = _agent()
    session = _session([_completed_run("run-1", "FIRST-MARKER"), _completed_run("run-2", "SECOND-MARKER")])
    runs_before = [run.run_id for run in session.runs or []]
    replay_settings_before = (agent.add_history_to_context, agent.num_history_runs, agent.num_history_messages)

    request = await build_agent_provider_request_from_runs(
        agent=agent,
        source_session=session,
        prefix_runs=list(session.runs or []),
        final_user_message=Message(role="user", content="SUMMARY-INSTRUCTION"),
        synthetic_run_id="run-1+run-2",
    )

    contents = [str(message.content) for message in request.messages]
    assert contents[-1] == "SUMMARY-INSTRUCTION"
    first_index = next(index for index, content in enumerate(contents) if "FIRST-MARKER question" in content)
    second_index = next(index for index, content in enumerate(contents) if "SECOND-MARKER answer" in content)
    assert first_index < second_index < len(contents) - 1
    assert request.tool_choice == "none"
    tool_names = [
        function_payload["name"] for tool in request.tools if isinstance(function_payload := tool.get("function"), dict)
    ]
    assert tool_names == ["_lookup_weather"]
    # The live agent and the source session are untouched.
    assert [run.run_id for run in session.runs or []] == runs_before
    assert (agent.add_history_to_context, agent.num_history_runs, agent.num_history_messages) == (
        replay_settings_before
    )


@pytest.mark.asyncio
async def test_warm_prefix_summary_request_is_marked_for_prefix_reuse() -> None:
    agent = _agent()
    session = _session([_completed_run("run-1", "MARKER")])

    request = await build_warm_prefix_summary_request(
        agent=agent,
        working_session=session,
        prefix_runs=list(session.runs or []),
        final_instruction="SUMMARY-INSTRUCTION",
    )

    assert isinstance(request, SummaryProviderRequest)
    assert request.reuses_reply_prefix is True
    assert str(request.messages[-1].content) == "SUMMARY-INSTRUCTION"


@pytest.mark.asyncio
async def test_generate_compaction_summary_sends_provider_request_verbatim() -> None:
    model = _RecordingFakeModel(id="fake-model", provider="fake")
    request = SummaryProviderRequest(
        messages=(
            Message(role="system", content="system prefix"),
            Message(role="user", content="history"),
            Message(role="user", content="instruction"),
        ),
        tools=({"type": "function", "function": {"name": "_lookup_weather"}},),
        tool_choice="none",
    )

    summary = await generate_compaction_summary(
        model=model,
        summary_input="unused for warm requests",
        summary_prompt=COMPACTION_SUMMARY_PROMPT,
        provider_request=request,
    )

    assert summary.summary == "warm summary"
    assert [(message.role, message.content) for message in model.seen_messages] == [
        ("system", "system prefix"),
        ("user", "history"),
        ("user", "instruction"),
    ]
    assert model.seen_tools == [{"type": "function", "function": {"name": "_lookup_weather"}}]
    assert model.seen_tool_choice == "none"


# --- Warm instruction ------------------------------------------------------------


def test_warm_summary_instruction_embeds_previous_summary() -> None:
    instruction = _warm_summary_instruction(COMPACTION_WARM_SUMMARY_INSTRUCTION, "earlier summary")

    assert instruction.startswith(COMPACTION_WARM_SUMMARY_INSTRUCTION)
    assert "<previous_summary>\nearlier summary\n</previous_summary>" in instruction


def test_warm_summary_instruction_without_previous_summary_is_bare() -> None:
    assert _warm_summary_instruction(COMPACTION_WARM_SUMMARY_INSTRUCTION, None) == COMPACTION_WARM_SUMMARY_INSTRUCTION
    assert _warm_summary_instruction(COMPACTION_WARM_SUMMARY_INSTRUCTION, "  ") == COMPACTION_WARM_SUMMARY_INSTRUCTION


# --- Runtime selection -----------------------------------------------------------


def test_warm_prefix_context_requires_active_model(tmp_path: Path) -> None:
    config, _runtime_paths = _make_config(tmp_path)
    agent = _agent()
    active_inputs = SimpleNamespace(
        execution_plan=SimpleNamespace(compaction_model_name="default"),
        active_model_name="default",
    )
    dedicated_inputs = SimpleNamespace(
        execution_plan=SimpleNamespace(compaction_model_name="cheap-summary"),
        active_model_name="default",
    )

    context = _warm_prefix_summary_context(agent=agent, resolved_inputs=active_inputs, config=config)
    assert context is not None
    assert context.agent is agent
    assert context.instruction == COMPACTION_WARM_SUMMARY_INSTRUCTION

    assert _warm_prefix_summary_context(agent=agent, resolved_inputs=dedicated_inputs, config=config) is None


# --- End to end through compact_scope_history -------------------------------------


@pytest.mark.asyncio
async def test_compact_scope_history_sends_warm_prefix_request(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = _session([_completed_run("run-1", "FIRST-MARKER"), _completed_run("run-2", "SECOND-MARKER")])
    session.summary = SessionSummary(summary="earlier summary", updated_at=datetime.now(UTC))
    write_scope_state(session, _SCOPE, HistoryScopeState(force_compact_before_next_run=True))
    storage.upsert_session(session)
    summary_model = _RecordingFakeModel(id="summary-model", provider="fake")
    agent = _agent()

    _state, outcome = await compact_scope_history(
        storage=storage,
        session=session,
        scope=_SCOPE,
        state=read_scope_state(session, _SCOPE),
        history_settings=_HISTORY_SETTINGS,
        available_history_budget=None,
        summary_input_budget=50_000,
        summary_model=summary_model,
        summary_model_name="default",
        active_context_window=64_000,
        replay_window_tokens=64_000,
        threshold_tokens=None,
        reserve_tokens=0,
        summary_prompt=COMPACTION_SUMMARY_PROMPT,
        warm_prefix=WarmPrefixSummaryContext(agent=agent, instruction=COMPACTION_WARM_SUMMARY_INSTRUCTION),
    )

    assert outcome is not None
    contents = [str(message.content) for message in summary_model.seen_messages]
    assert any("FIRST-MARKER question" in content for content in contents)
    assert any("SECOND-MARKER answer" in content for content in contents)
    assert contents[-1].startswith(COMPACTION_WARM_SUMMARY_INSTRUCTION)
    assert "<previous_summary>\nearlier summary\n</previous_summary>" in contents[-1]
    assert summary_model.seen_tool_choice == "none"

    persisted = get_agent_session(storage, "session-1")
    assert persisted is not None
    assert persisted.summary is not None
    assert persisted.summary.summary == "warm summary"
    assert persisted.runs == []
    assert set(read_scope_state(persisted, _SCOPE).compacted_run_ids) == {"run-1", "run-2"}
    storage.close()
