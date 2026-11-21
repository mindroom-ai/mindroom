"""MindRoom's own tool loop on the ``claude_code`` route, with the request pipeline the registry builds around it."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.agent_loop.model_loop import (
    EMPTY_REQUEST,
    NO_HOOKS,
    LoopEnded,
    LoopHooks,
    LoopState,
    SendRequest,
    run_model_loop,
)
from mindroom.agent_loop.tools import PreparedTools
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.events import ToolCompleted
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import RunMetrics
from mindroom.providers.claude_code.session import SESSIONS
from mindroom.providers.registry import build_model_provider
from tests.agent_loop.helpers import context, spec, toolkit
from tests.providers.claude_code.support import call, fake_cli

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.agent_loop.model_loop import LoopEvent
    from tests.providers.claude_code.support import FakeCli

pytestmark = pytest.mark.asyncio

_MARKERS = {"a": "LILAC-HERON", "b": "LEMON-IBIS", "c": "COPPER-OTTER"}
_FIRST = {"input_tokens": 3, "cache_creation_input_tokens": 900, "cache_read_input_tokens": 0, "output_tokens": 40}
_SECOND = {"input_tokens": 4, "cache_creation_input_tokens": 60, "cache_read_input_tokens": 900, "output_tokens": 20}
_THIRD = {"input_tokens": 5, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 960, "output_tokens": 9}
_TOOL_TURN = [
    {"text": "Looking.", "tools": [call("toolu_a", "a"), call("toolu_b", "b")], "usage": _FIRST},
    {"tools": [call("toolu_c", "c")], "usage": _SECOND},
    {"text": "{toolu_a}+{toolu_b}+{toolu_c}", "usage": _THIRD},
]


@pytest_asyncio.fixture(autouse=True)
async def _no_processes_left() -> AsyncIterator[None]:
    yield
    await SESSIONS.close_all()


def lookup(key: str) -> str:
    """Look up a marker by key.

    Args:
        key: The marker's key.

    """
    return _MARKERS[key]


async def _run(cli: FakeCli, hooks: LoopHooks = NO_HOOKS) -> tuple[list[LoopEvent], RunRecord, list[Message]]:
    model = {"provider": "claude_code", "id": "claude-invented-1", "extra_kwargs": {"cli_path": str(cli.path)}}
    config = Config.model_validate({"models": {"default": model}})
    runtime_paths = resolve_runtime_paths(
        config_path=cli.storage_root.parent / "config.yaml",
        storage_path=cli.storage_root,
    )
    agent = spec(build_model_provider(config, runtime_paths), toolkit(lookup))
    messages = [Message(role="system", content="You are Code."), Message(role="user", content="Markers for a, b, c?")]
    record = RunRecord(run_id="run-1", agent_id="code", metrics=RunMetrics())
    events = [
        event
        async for event in run_model_loop(
            agent.model,
            messages,
            PreparedTools(functions=agent.functions),
            record=record,
            run=context(),
            hooks=hooks,
            max_tool_calls_per_turn=None,
            request_template=EMPTY_REQUEST,
        )
    ]
    return events, record, messages


async def test_a_tool_turn_of_three_requests_runs_on_one_process_and_counts_each_request_once(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [_TOOL_TURN]})

    events, record, messages = await _run(cli)

    assert events[-1] == LoopEnded(paused=False)
    assert record.content == "Looking.\n\nLILAC-HERON+LEMON-IBIS+COPPER-OTTER"
    assert [message.role for message in messages[2:]] == ["assistant", "tool", "tool", "assistant", "tool", "assistant"]
    # One process served the whole loop, and each MindRoom tool ran once, for the call Claude made.
    assert len(cli.events("spawn")) == 1
    assert len(cli.events("user")) == 1
    assert len([event for event in events if isinstance(event, ToolCompleted)]) == 3
    assert [(entry["id"], entry["result"]["content"][0]["text"]) for entry in cli.events("tool_result")] == [
        ("toolu_a", "LILAC-HERON"),
        ("toolu_b", "LEMON-IBIS"),
        ("toolu_c", "COPPER-OTTER"),
    ]
    # Claude Code's result repeats the whole turn's usage; each request's own counts once.
    assert record.metrics is not None
    tokens = record.metrics.tokens
    assert (tokens.input_tokens, tokens.output_tokens) == (3 + 4 + 5, 40 + 20 + 9)
    assert (tokens.cache_write_tokens, tokens.cache_read_tokens) == (900 + 60 + 30, 900 + 960)
    answers = [message for message in messages if message.role == "assistant"]
    assert [message.metrics.tokens.output_tokens for message in answers] == [40, 20, 9]


async def test_history_rewritten_mid_turn_rebuilds_a_new_process_from_mindrooms_history(tmp_path: Path) -> None:
    async def compact(state: LoopState) -> SendRequest:
        if state.request_index == 1:
            # As mid-turn compaction does: the first user message becomes a summary.
            state.messages[1] = Message(role="user", content="Summary: the user wants the markers for a, b and c.")
        return SendRequest()

    rebuilt = [{"text": "LILAC-HERON+LEMON-IBIS", "usage": _THIRD}]
    cli = fake_cli(tmp_path, {"turns": [_TOOL_TURN]}, {"turns": [rebuilt]})

    events, record, _messages = await _run(cli, LoopHooks(before_request=(compact,)))

    assert events[-1] == LoopEnded(paused=False)
    assert record.content == "Looking.\n\nLILAC-HERON+LEMON-IBIS"
    assert len(cli.events("spawn")) == 2
    # The parked process never got a result: it rests until its time to live passes, and the new process read the
    # results as history.
    assert cli.events("tool_result") == []
    assert sorted(session.emitted.tool_call_ids for session in SESSIONS.resting.values() if session.emitted) == [
        (),
        ("toolu_a", "toolu_b"),
    ]
    history = cli.events("user")[1]["content"][0]["text"]
    assert "Summary: the user wants the markers for a, b and c." in history
    assert '"output": "LILAC-HERON"' in history
    assert '"output": "LEMON-IBIS"' in history
    assert len([event for event in events if isinstance(event, ToolCompleted)]) == 2
    assert record.metrics is not None
    assert record.metrics.tokens.output_tokens == 40 + 9
