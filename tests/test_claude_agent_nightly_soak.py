"""Nightly soak test for Claude Agent SDK tool robustness."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from agno.run.base import RunContext
from claude_agent_sdk import AssistantMessage, ClaudeSDKError, ResultMessage, TextBlock

from mindroom.custom_tools import claude_agent as claude_agent_module

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@dataclass
class _NightlySoakSDKClient:
    """Stateful SDK test double for long-running soak scenarios."""

    options: Any

    instances: ClassVar[list[_NightlySoakSDKClient]] = []
    shared_files: ClassVar[dict[str, list[str]]] = {}
    active_queries: ClassVar[int] = 0
    max_concurrent_queries: ClassVar[int] = 0
    forced_failures: ClassVar[int] = 0

    def __post_init__(self) -> None:
        self.connected = False
        self._last_text = ""
        self._session_counter = 0
        _NightlySoakSDKClient.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:  # noqa: ARG002
        type(self).active_queries += 1
        type(self).max_concurrent_queries = max(type(self).max_concurrent_queries, type(self).active_queries)
        await asyncio.sleep(0.001)

        if prompt.startswith("fail:"):
            type(self).forced_failures += 1
            type(self).active_queries -= 1
            message = f"forced failure for {prompt}"
            raise ClaudeSDKError(message)

        if prompt.startswith("write:"):
            _, file_name, line = prompt.split(":", 2)
            lines = type(self).shared_files.setdefault(file_name, [])
            lines.append(line)
            self._last_text = f"wrote:{file_name}:{len(lines)}"
        elif prompt.startswith("read:"):
            _, file_name = prompt.split(":", 1)
            lines = type(self).shared_files.get(file_name, [])
            self._last_text = "|".join(lines)
        else:
            self._last_text = f"ok:{prompt}"

        self._session_counter += 1
        type(self).active_queries -= 1

    async def receive_response(self) -> AsyncGenerator[AssistantMessage | ResultMessage, None]:
        yield AssistantMessage(content=[TextBlock(text=self._last_text)], model="claude-soak")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=f"soak-session-{self._session_counter}",
            total_cost_usd=0.0,
        )

    async def interrupt(self) -> None:
        return

    async def disconnect(self) -> None:
        self.connected = False


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("RUN_NIGHTLY_SOAK") != "1",
    reason="Set RUN_NIGHTLY_SOAK=1 to run long soak test",
)
async def test_claude_agent_nightly_soak_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run a long, mixed workload loop and fail on deadlocks, leaks, or unexpected tool errors."""
    _NightlySoakSDKClient.instances = []
    _NightlySoakSDKClient.shared_files = {}
    _NightlySoakSDKClient.active_queries = 0
    _NightlySoakSDKClient.max_concurrent_queries = 0
    _NightlySoakSDKClient.forced_failures = 0

    manager = claude_agent_module.ClaudeSessionManager()
    monkeypatch.setattr(claude_agent_module.ClaudeAgentTools, "_session_manager", manager)
    monkeypatch.setattr(claude_agent_module, "ClaudeSDKClient", _NightlySoakSDKClient)

    tools = claude_agent_module.ClaudeAgentTools(
        api_key="sk-test",
        max_sessions=12,
        session_ttl_minutes=5,
    )
    run_context = RunContext(run_id="run-soak", session_id="soak-session")
    agent = SimpleNamespace(name="soak")

    session_labels = [f"seq-{i}" for i in range(6)]
    parallel_labels = [f"par-{i}" for i in range(4)]

    expected_failure_count = 0
    for turn in range(120):
        label = session_labels[turn % len(session_labels)]

        if turn % 24 == 0:
            expected_failure_count += 1
            failure = await asyncio.wait_for(
                tools.claude_send(
                    f"fail:turn-{turn}",
                    session_label=label,
                    run_context=run_context,
                    agent=agent,
                ),
                timeout=3,
            )
            assert "Claude session error:" in failure

            recovery = await asyncio.wait_for(
                tools.claude_send(
                    f"recover-{turn}",
                    session_label=label,
                    run_context=run_context,
                    agent=agent,
                ),
                timeout=3,
            )
            assert f"ok:recover-{turn}" in recovery
            continue

        if turn % 10 == 0:
            write = await asyncio.wait_for(
                tools.claude_send(
                    f"write:file-{turn % 3}:line-{turn}",
                    session_label=label,
                    run_context=run_context,
                    agent=agent,
                ),
                timeout=3,
            )
            assert "wrote:file-" in write

            read = await asyncio.wait_for(
                tools.claude_send(
                    f"read:file-{turn % 3}",
                    session_label=label,
                    run_context=run_context,
                    agent=agent,
                ),
                timeout=3,
            )
            assert f"line-{turn}" in read
            continue

        if turn % 15 == 0:
            burst_results = await asyncio.wait_for(
                asyncio.gather(
                    *[
                        tools.claude_send(
                            f"parallel-{turn}-{idx}",
                            session_label=parallel_labels[idx],
                            run_context=run_context,
                            agent=agent,
                        )
                        for idx in range(len(parallel_labels))
                    ],
                ),
                timeout=5,
            )
            for idx, result in enumerate(burst_results):
                assert f"ok:parallel-{turn}-{idx}" in result
            continue

        result = await asyncio.wait_for(
            tools.claude_send(
                f"turn-{turn}",
                session_label=label,
                run_context=run_context,
                agent=agent,
            ),
            timeout=3,
        )
        assert f"ok:turn-{turn}" in result
        assert "missing 1 required positional argument" not in result
        assert "RunContext is not defined" not in result

    assert _NightlySoakSDKClient.forced_failures == expected_failure_count
    assert _NightlySoakSDKClient.max_concurrent_queries >= 2

    for label in session_labels + parallel_labels:
        await tools.claude_end_session(session_label=label, run_context=run_context, agent=agent)

    assert not manager._sessions
