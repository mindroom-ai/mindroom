"""An opt-in smoke test of the ``claude_code`` route against the real, signed-in ``claude`` binary.

Set ``MINDROOM_CLAUDE_CODE_LIVE_TEST=1`` to run it: it spends a few small requests of the signed-in subscription on
the cheapest model. It is the check to repeat when Claude Code changes, since the route relies on the call id Claude
Code sends with each MCP tool call.
"""
# ruff: noqa: D103

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from mindroom.agent_loop.model_loop import LoopEnded
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import RunMetrics
from mindroom.providers.claude_code import session as session_module
from mindroom.providers.registry import build_model_provider
from tests.agent_loop.helpers import run_loop, spec, toolkit
from tests.providers.claude_code.support import CONVERSATION, MARKERS, lookup

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("MINDROOM_CLAUDE_CODE_LIVE_TEST") != "1",
        reason="set MINDROOM_CLAUDE_CODE_LIVE_TEST=1 to spend a few requests of the signed-in Claude subscription",
    ),
]

_PROMPT = (
    "Call lookup twice in a single response, once with key a and once with key b. After both results, call confirm "
    "with those two markers. Then reply only with the final marker returned by confirm."
)


def confirm(markers: list[str]) -> str:  # noqa: ARG001 - the tool schema takes the markers the run records
    """Confirm the looked-up markers and return the final marker.

    Args:
        markers: The markers lookup returned.

    """
    return MARKERS["c"]


def _transcripts() -> set[Path]:
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return set((claude_home / "projects").rglob("*.jsonl"))


@pytest.mark.timeout(180)
async def test_a_tool_turn_runs_on_the_signed_in_subscription(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert shutil.which("claude"), "the live test needs `claude` on PATH"
    transcripts = _transcripts()
    started = []
    start = session_module.SESSIONS.start

    async def counted(**kwargs: Any) -> session_module.LiveSession:  # noqa: ANN401 - whatever ``start`` takes
        started.append(kwargs["model"])
        return await start(**kwargs)

    monkeypatch.setattr(session_module.SESSIONS, "start", counted)
    config = Config.model_validate({"models": {"default": {"provider": "claude_code", "id": "claude-haiku-5-5"}}})
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data")
    agent = spec(build_model_provider(config, runtime_paths, execution_identity=CONVERSATION), toolkit(lookup, confirm))
    messages = [Message(role="system", content="You are a test assistant."), Message(role="user", content=_PROMPT)]

    async def turn(run_id: str) -> RunRecord:
        record = RunRecord(run_id=run_id, agent_id="code", metrics=RunMetrics())
        events, _ = await run_loop(agent, messages, record=record)
        assert events[-1] == LoopEnded(paused=False)
        return record

    first = await turn("run-1")
    answers = [message for message in messages if message.role == "assistant"]
    messages.append(Message(role="user", content="Reply with only the word DONE."))
    second = await turn("run-2")

    assert "COPPER-OTTER" in (first.content or "")
    assert "DONE" in (second.content or "")
    calls = [(call.tool_name, call.tool_args or {}) for call in (*first.tools, *second.tools)]
    assert sorted(args["key"] for name, args in calls if name == "lookup") == ["a", "b"]
    [confirmed] = [args["markers"] for name, args in calls if name == "confirm"]
    assert sorted(confirmed) == sorted([MARKERS["a"], MARKERS["b"]])
    assert len(calls) == 3
    # One process served every request of the tool turn, parked on its calls, and then the next user message, idle.
    assert started == ["claude-haiku-5-5"]
    assert len(answers) >= 3
    # The requests after the first continue the same Claude Code turn, so they read its prompt from the cache.
    assert answers[-1].metrics.tokens.cache_read_tokens > 0
    assert all(message.metrics.tokens.output_tokens > 0 for message in answers)
    # MindRoom's history is the only record of the conversation.
    assert _transcripts() == transcripts
