"""Tests for the Letta runtime adapter's durable identity seam."""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.letta_runtime import LettaRuntimeAdapter, LettaTurn
from mindroom.tool_system.events import ToolTraceEntry


def test_letta_agent_requires_persistent_agent_id() -> None:
    """A Letta-backed Matrix identity must name the Letta agent it represents."""
    with pytest.raises(ValidationError, match="letta_agent_id is required"):
        AgentConfig(display_name="Globalia", runtime="letta")


def test_agno_remains_the_default_runtime() -> None:
    """Existing MindRoom agents keep their Agno behavior unless explicitly changed."""
    agent = AgentConfig(display_name="Mind")

    assert agent.runtime == "agno"
    assert agent.letta_agent_id is None


def test_bridge_source_is_part_of_the_mindroom_package(tmp_path: Path) -> None:
    """Installed MindRoom artifacts retain the TypeScript bridge entrypoint."""
    assert LettaRuntimeAdapter(tmp_path)._bridge_path().is_file()


def test_matrix_thread_maps_to_one_persisted_letta_conversation(tmp_path: Path) -> None:
    """The adapter retains the Letta conversation selected for one Matrix thread."""
    adapter = LettaRuntimeAdapter(tmp_path)
    turn = LettaTurn(
        entity_name="globalia",
        agent_id="agent-globalia",
        room_id="!room:example.org",
        thread_id="$thread",
        prompt="hello",
        cwd=tmp_path,
    )
    key = adapter._conversation_key(turn)

    adapter._save_conversation(key, "conv-one")

    assert adapter._load_conversation(key) == "conv-one"
    assert adapter._load_conversation("other") is None


def test_different_letta_agents_get_different_conversation_keys(tmp_path: Path) -> None:
    """Changing a Matrix entity's Letta identity cannot resume the old agent's conversation."""
    turn = LettaTurn(
        entity_name="assistant",
        agent_id="agent-one",
        room_id="!room:example.org",
        thread_id="$thread",
        prompt="hello",
        cwd=tmp_path,
    )

    replacement = LettaTurn(
        entity_name=turn.entity_name,
        agent_id="agent-two",
        room_id=turn.room_id,
        thread_id=turn.thread_id,
        prompt=turn.prompt,
        cwd=turn.cwd,
    )

    assert LettaRuntimeAdapter._conversation_key(turn) != LettaRuntimeAdapter._conversation_key(replacement)


def test_letta_agents_are_rejected_inside_agno_teams() -> None:
    """Mixed runtime teams fail at configuration time instead of silently using Agno."""
    with pytest.raises(ValidationError, match="Letta runtime agents, which are not yet supported"):
        Config(
            agents={
                "globalia": AgentConfig(
                    display_name="Globalia",
                    runtime="letta",
                    letta_agent_id="agent-globalia",
                ),
            },
            teams={
                "helpers": TeamConfig(
                    display_name="Helpers",
                    role="Help",
                    agents=["globalia"],
                ),
            },
        )


def test_tool_result_completes_the_matching_trace_entry() -> None:
    """Letta tool results update their matching MindRoom tool card slot."""
    trace = [
        ToolTraceEntry(
            type="tool_call_started",
            tool_name="fetch_webpage",
            tool_call_id="call-one",
        ),
    ]

    LettaRuntimeAdapter._complete_tool_trace(
        trace,
        {"toolCallId": "call-one", "content": "Fetched page"},
    )

    assert trace[0].type == "tool_call_completed"
    assert trace[0].result_preview == "Fetched page"


def test_streamed_tool_call_deltas_update_one_trace_entry() -> None:
    """Repeated SDK deltas for one tool call retain one Matrix tool-card slot."""
    trace: list[ToolTraceEntry] = []

    LettaRuntimeAdapter._update_tool_trace(
        trace,
        {"toolCallId": "call-one", "toolName": "fetch_webpage", "toolInput": {}},
    )
    LettaRuntimeAdapter._update_tool_trace(
        trace,
        {
            "toolCallId": "call-one",
            "toolName": "fetch_webpage",
            "toolInput": {"url": "https://example.com"},
        },
    )

    assert len(trace) == 1
    assert trace[0].args_preview == '{"url": "https://example.com"}'


@pytest.mark.asyncio
async def test_stuck_bridge_is_forcefully_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation escalates when a bridge ignores the graceful termination signal."""
    monkeypatch.setattr("mindroom.letta_runtime._PROCESS_TERMINATION_TIMEOUT_SECONDS", 0.01)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('READY', flush=True); time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert await process.stdout.readline() == b"READY\n"

    await LettaRuntimeAdapter._terminate_process(process)

    assert process.returncode is not None


@pytest.mark.asyncio
async def test_conversation_identity_is_saved_before_a_turn_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled partial turn still resumes the conversation created for its Matrix thread."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_nub = fake_bin / "nub"
    fake_nub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'type': 'session', 'conversationId': 'conv-created'}), flush=True)\n"
        "print(json.dumps({'type': 'assistant', 'content': 'partial'}), flush=True)\n"
        "time.sleep(60)\n",
    )
    fake_nub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    adapter = LettaRuntimeAdapter(tmp_path)
    turn = LettaTurn(
        entity_name="globalia",
        agent_id="agent-globalia",
        room_id="!room:example.org",
        thread_id="$thread",
        prompt="hello",
        cwd=tmp_path,
    )
    stream = adapter.stream(turn)

    chunk = await anext(stream)
    assert chunk.content == "partial"
    await stream.aclose()

    assert adapter._load_conversation(adapter._conversation_key(turn)) == "conv-created"
