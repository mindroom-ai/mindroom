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

import pytest

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, NO_HOOKS, LoopEnded, run_model_loop
from mindroom.agent_loop.tools import PreparedTools
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import RunMetrics
from mindroom.providers.claude_code.session import SESSIONS
from mindroom.providers.registry import build_model_provider
from tests.agent_loop.helpers import context, spec, toolkit

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("MINDROOM_CLAUDE_CODE_LIVE_TEST"),
        reason="set MINDROOM_CLAUDE_CODE_LIVE_TEST=1 to spend a few requests of the signed-in Claude subscription",
    ),
]

_MARKERS = {"a": "LILAC-HERON", "b": "LEMON-IBIS"}
_PROMPT = (
    "Call lookup twice in a single response, once with key a and once with key b. After both results, call confirm "
    "with those two markers. Then reply only with the final marker returned by confirm."
)
_calls: list[tuple[str, object]] = []


def lookup(key: str) -> str:
    """Look up a marker by key.

    Args:
        key: The marker's key, a or b.

    """
    _calls.append(("lookup", key))
    return _MARKERS[key]


def confirm(markers: list[str]) -> str:
    """Confirm the looked-up markers and return the final marker.

    Args:
        markers: The markers lookup returned.

    """
    _calls.append(("confirm", sorted(markers)))
    return "COPPER-OTTER"


def _transcripts() -> set[Path]:
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return set((claude_home / "projects").rglob("*.jsonl"))


@pytest.mark.timeout(180)
async def test_a_tool_turn_runs_on_the_signed_in_subscription(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert shutil.which("claude"), "the live test needs `claude` on PATH"
    # A key or token in the environment would be used instead of the subscription, and the route refuses it.
    for variable in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(variable, raising=False)
    _calls.clear()
    transcripts = _transcripts()
    config = Config.model_validate({"models": {"default": {"provider": "claude_code", "id": "claude-haiku-5-5"}}})
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data")
    agent = spec(build_model_provider(config, runtime_paths), toolkit(lookup, confirm))
    messages = [Message(role="system", content="You are a test assistant."), Message(role="user", content=_PROMPT)]
    record = RunRecord(run_id="run-1", agent_id="code", metrics=RunMetrics())

    try:
        events = [
            event
            async for event in run_model_loop(
                agent.model,
                messages,
                PreparedTools(functions=agent.functions),
                record=record,
                run=context(),
                hooks=NO_HOOKS,
                max_tool_calls_per_turn=None,
                request_template=EMPTY_REQUEST,
            )
        ]
        # One process served every request of the turn, and it now rests idle.
        assert len(SESSIONS.live) == 1
    finally:
        await SESSIONS.close_all()

    assert events[-1] == LoopEnded(paused=False)
    assert "COPPER-OTTER" in (record.content or "")
    assert sorted(_calls, key=str) == [
        ("confirm", ["LEMON-IBIS", "LILAC-HERON"]),
        ("lookup", "a"),
        ("lookup", "b"),
    ]
    answers = [message for message in messages if message.role == "assistant"]
    assert len(answers) >= 3
    # The requests after the first continue the same Claude Code turn, so they read its prompt from the cache.
    assert answers[-1].metrics.tokens.cache_read_tokens > 0
    assert all(message.metrics.tokens.output_tokens > 0 for message in answers)
    # MindRoom's history is the only record of the conversation.
    assert _transcripts() == transcripts
