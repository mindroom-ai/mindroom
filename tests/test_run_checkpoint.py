"""A run's checkpoints, and the recovery of a run a restart cut short (``mindroom.history.run_checkpoint``)."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom import loop_hooks
from mindroom.ai import ai_response
from mindroom.cancellation import request_task_cancel
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.history.run_checkpoint import (
    INTERRUPTED_CALL_RESULT,
    recover_interrupted_run,
    recover_interrupted_turn,
)
from mindroom.history.run_visibility import is_recovered_run
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.provider import ToolCallReady, TurnCompleted, UsageReported
from mindroom.llm.request_identity import request_prefixes
from mindroom.llm.runs import RunStatus
from mindroom.providers.anthropic.wire import format_claude_messages
from mindroom.providers.claude_wire_blocks import CLAUDE_BLOCKS_KEY
from mindroom.response_turn import build_matrix_run_metadata
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.conftest import bind_runtime_paths, install_model_providers, make_turn_context, runtime_paths_for
from tests.delegation_helpers import ScriptedAgents, _runtime_paths, native_delegation_config, respond
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call, usage
from tests.identity_helpers import entity_ids
from tests.scripted_agent_helpers import PROBE_TOOL, ProbeTools, registered_probe_tools, stored_runs

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.llm.provider import ModelRequest
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore

_SCOPE = HistoryScope(kind="agent", scope_id="general")
_SESSION = "session-1"
_EVENT = "$question"


@pytest.fixture(autouse=True)
def _probe_tools() -> Iterator[None]:
    with registered_probe_tools():
        yield


def _config(storage: Path) -> Config:
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", tools=[PROBE_TOOL])},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        resolve_runtime_paths(config_path=storage / "config.yaml", storage_path=storage),
    )
    config.memory.backend = "none"
    return config


async def _respond(config: Config, prompt: str = "Do the work", event_id: str = _EVENT) -> None:
    ctx = replace(
        make_turn_context("general", session_id=_SESSION, matrix_run_metadata=build_matrix_run_metadata(event_id, [])),
        prompt=prompt,
    )
    await ai_response(ctx, runtime_paths_for(config), config)


def _thinking_call(call: dict[str, Any], *, signature: str) -> list[Any]:
    """Return a Claude turn that thinks, with a signed thinking block, and then requests ``call``."""
    function = call["function"]
    blocks = [
        {"type": "thinking", "thinking": "I should look first.", "signature": signature},
        {"type": "tool_use", "id": call["id"], "name": function["name"], "input": json.loads(function["arguments"])},
    ]
    metrics = usage(10, 5)
    message = Message(
        role="assistant",
        reasoning_content="I should look first.",
        tool_calls=[call],
        provider_data={CLAUDE_BLOCKS_KEY: blocks},
        metrics=metrics,
    )
    return [ToolCallReady(call), UsageReported(metrics), TurnCompleted(message=message, stop_reason="tool_use")]


def _crashing_turns(read_path: str = "a") -> list[Sequence[Any]]:
    """Return a turn's script: a finished read, then a shell call that keeps running."""
    return [
        _thinking_call(tool_call("read_file", "read", path=read_path), signature="signature-1"),
        _thinking_call(tool_call("run_shell_command", "shell", cmd="sleep 60"), signature="signature-2"),
    ]


async def _crash_mid_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, turns: list[Sequence[Any]]) -> ModelRequest:
    """Run a turn until its shell call keeps running and copy its storage there, as a kill leaves it.

    Returns the last request the turn sent; ``tmp_path / "crashed"`` holds the copy.
    """
    config = _config(tmp_path / "live")
    model = ScriptedModel(turns)
    install_model_providers(monkeypatch, {"default": model})
    turn = asyncio.create_task(_respond(config))
    await asyncio.wait_for(ProbeTools.running.wait(), 10)
    shutil.copytree(tmp_path / "live", tmp_path / "crashed")
    turn.cancel()
    with suppress(asyncio.CancelledError):
        await turn
    return model.requests[-1]


def _store(config: Config) -> ConversationStore:
    return create_session_storage("general", config, runtime_paths_for(config), execution_identity=None)


def _recover(config: Config, *, natively: bool = True) -> bool:
    return recover_interrupted_turn(_store(config), _SESSION, _SCOPE, {_EVENT}, natively=natively)


def _only_run(config: Config) -> AnyRunRecord:
    [run] = stored_runs(config, runtime_paths_for(config), _SESSION)
    return run


def _wire(request: ModelRequest) -> list[str]:
    """Return the Claude request turns ``request`` sends, each as the bytes of its JSON."""
    formatted = format_claude_messages(
        request.messages,
        append_trailing_user_message=False,
        earlier_thinking=True,
        provider="anthropic",
    )
    return [json.dumps(turn, sort_keys=True) for turn in formatted.messages]


@pytest.mark.asyncio
async def test_a_killed_turn_recovers_its_finished_calls_and_answers_the_one_in_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The storage a kill leaves mid-call recovers as a turn of the run's own messages, every call answered."""
    await _crash_mid_call(tmp_path, monkeypatch, _crashing_turns())
    crashed = _config(tmp_path / "crashed")
    checkpoint = _only_run(crashed)
    assert checkpoint.status is RunStatus.cancelled

    assert _recover(crashed)

    run = _only_run(crashed)
    assert run.status is RunStatus.completed
    assert is_recovered_run(run)
    assert [(message.role, message.tool_call_id, message.content) for message in run.messages] == [
        ("user", None, "Do the work"),
        ("assistant", None, None),
        ("tool", "read", "contents of a"),
        ("assistant", None, None),
        ("tool", "shell", INTERRUPTED_CALL_RESULT),
    ]
    assert run.messages[-1].tool_call_error is True
    # Recovery left the run's own messages as the checkpoint stored them, signed reasoning included.
    assert run.messages[:4] == checkpoint.messages
    assert [tool.result for tool in run.tools or ()] == ["contents of a", INTERRUPTED_CALL_RESULT]
    # A second recovery finds nothing to do, and the turn stays in history.
    assert _recover(crashed)
    assert _only_run(crashed).messages == run.messages


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "event_id"),
    [("Do the work", _EVENT), ("Carry on", "$next")],
    ids=["the turn's replay", "a later turn"],
)
async def test_a_recovered_turn_replays_the_bytes_the_crashed_run_sent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
    event_id: str,
) -> None:
    """The next request starts with the crashed run's last request, turn for turn, then its last call and answer."""
    last_sent = await _crash_mid_call(tmp_path, monkeypatch, _crashing_turns())
    crashed = _config(tmp_path / "crashed")
    assert _recover(crashed)
    model = ScriptedModel([answer("Finished")])
    install_model_providers(monkeypatch, {"default": model})

    await _respond(crashed, prompt=prompt, event_id=event_id)

    [resumed] = model.requests
    sent, replayed = _wire(last_sent), _wire(resumed)
    # The crashed run's last request: the prompt, its first response with signed thinking, and that call's result.
    assert len(sent) == 3
    assert "signature-1" in sent[1]
    assert replayed[: len(sent)] == sent
    assert [message.content for message in resumed.messages if message.role == "system"] == [
        message.content for message in last_sent.messages if message.role == "system"
    ]
    # The request identity the loop sizes by reads the replayed messages as the crashed run's request too.
    assert request_prefixes(model, resumed)[: len(last_sent.messages) + 1] == request_prefixes(model, last_sent)
    # What follows is the response the kill cut off after its call was dispatched, then the call's answer and the
    # new request's prompt, which Claude takes as one user turn.
    after = [json.loads(turn) for turn in replayed[len(sent) :]]
    assert [turn["role"] for turn in after] == ["assistant", "user"]
    assert after[0]["content"][0] == {
        "type": "thinking",
        "thinking": "I should look first.",
        "signature": "signature-2",
    }
    assert after[1]["content"][0]["content"] == INTERRUPTED_CALL_RESULT
    assert prompt in after[1]["content"][-1]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit",
    [{"max_tool_calls_from_history": 0}, {"num_history_messages": 1}, {"num_history_runs": 1}],
    ids=lambda limit: next(iter(limit)),
)
async def test_no_history_limit_trims_the_recovered_turn_its_replay_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: dict[str, int],
) -> None:
    """The agent's limits on earlier history leave the turn's own recovered calls and results in its request."""
    last_sent = await _crash_mid_call(tmp_path, monkeypatch, _crashing_turns())
    crashed = _config(tmp_path / "crashed")
    for name, value in limit.items():
        setattr(crashed.agents["general"], name, value)
    assert _recover(crashed)
    model = ScriptedModel([answer("Finished")])
    install_model_providers(monkeypatch, {"default": model})

    await _respond(crashed)

    [resumed] = model.requests
    assert [(message.role, message.tool_call_id) for message in resumed.messages if message.role != "system"] == [
        ("user", None),
        ("assistant", None),
        ("tool", "read"),
        ("assistant", None),
        ("tool", "shell"),
        ("user", None),
    ]
    assert _wire(resumed)[:3] == _wire(last_sent)


@pytest.mark.asyncio
async def test_checkpoints_leave_a_turn_that_finishes_and_the_turn_after_it_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that ends on its own stores, and the next turn sends, exactly what they do without checkpoints."""

    async def converse(storage: Path) -> tuple[list[ModelRequest], list[AnyRunRecord]]:
        config = _config(storage)
        model = ScriptedModel(
            [
                _thinking_call(tool_call("read_file", "read", path="a"), signature="signature-1"),
                calls(tool_call("run_shell_command", "shell", cmd="pwd")),
                answer("First answer"),
                answer("Second answer"),
            ],
        )
        install_model_providers(monkeypatch, {"default": model})
        await _respond(config)
        await _respond(config, prompt="And now?", event_id="$next")
        return model.requests, stored_runs(config, runtime_paths_for(config), _SESSION)

    saves: list[RunStatus] = []
    save_checkpoint = loop_hooks.checkpoint_hooks.__globals__["save_run_checkpoint"]

    async def counting(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        await save_checkpoint(*args, **kwargs)
        saves.append(args[2].status)

    monkeypatch.setitem(loop_hooks.checkpoint_hooks.__globals__, "save_run_checkpoint", counting)
    requests, runs = await converse(tmp_path / "checkpointed")
    # Each tool batch saved before it ran, and each later request of its run saved what it sends.
    assert saves == [RunStatus.cancelled] * 4
    monkeypatch.setattr(loop_hooks, "checkpoint_hooks", lambda _storage: None)
    plain_requests, plain_runs = await converse(tmp_path / "plain")

    assert [_wire(request) for request in requests] == [_wire(request) for request in plain_requests]
    assert [[message.content for message in request.messages] for request in requests] == [
        [message.content for message in request.messages] for request in plain_requests
    ]
    stored = [[(m.role, m.content, m.tool_calls, m.provider_data) for m in run.messages] for run in runs]
    assert stored == [[(m.role, m.content, m.tool_calls, m.provider_data) for m in run.messages] for run in plain_runs]
    assert [(run.status, is_recovered_run(run)) for run in runs] == [(RunStatus.completed, False)] * 2
    assert [run.metrics.tokens for run in runs] == [run.metrics.tokens for run in plain_runs]


@pytest.mark.asyncio
async def test_a_turn_that_keeps_being_interrupted_stops_continuing_from_its_recovered_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the limit nothing is recovered and what was recovered leaves history, so the turn starts over."""
    await _crash_mid_call(tmp_path, monkeypatch, _crashing_turns())
    crashed = _config(tmp_path / "crashed")
    assert _recover(crashed)

    assert not _recover(crashed, natively=False)

    run = _only_run(crashed)
    assert run.status is RunStatus.error
    assert "kept being interrupted" in str(run.error)
    # A later recovery does not bring the run back.
    assert not _recover(crashed)
    model = ScriptedModel([answer("Started over")])
    install_model_providers(monkeypatch, {"default": model})
    await _respond(crashed)
    [request] = model.requests
    assert [message.role for message in request.messages if message.role != "system"] == ["user"]


@pytest.mark.asyncio
async def test_a_checkpoint_never_recovered_stays_out_of_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed turn nothing recovers, as one past the limit, replays none of its messages."""
    await _crash_mid_call(tmp_path, monkeypatch, _crashing_turns())
    crashed = _config(tmp_path / "crashed")

    assert not _recover(crashed, natively=False)

    assert _only_run(crashed).status is RunStatus.cancelled
    model = ScriptedModel([answer("Started over")])
    install_model_providers(monkeypatch, {"default": model})
    await _respond(crashed)
    assert [message.role for message in model.requests[0].messages if message.role != "system"] == ["user"]


@pytest.mark.asyncio
async def test_a_run_that_continued_after_a_subagent_answered_keeps_checkpointing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without tool jobs a parent pauses for its subagent and continues as a resumed leg, which checkpoints too."""
    paths = _runtime_paths(tmp_path)
    config = with_responder_access(
        native_delegation_config(
            {
                "leader": AgentConfig(display_name="Leader", delegate_to=["child"], tools=[PROBE_TOOL]),
                "child": AgentConfig(display_name="Child"),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        "child",
        users=["@alice:example.org"],
    )
    entity_ids(config, paths)
    leader = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate", agent_name="child", task="Help")),
            calls(tool_call("read_file", "read", path="a")),
            calls(tool_call("run_shell_command", "shell", cmd="sleep 60")),
        ],
    )
    ScriptedAgents({"leader": leader, "child": ScriptedModel([answer("helped")])}).install(monkeypatch)
    identity = ToolExecutionIdentity(
        "matrix",
        "leader",
        "@alice:example.org",
        "!room:example.org",
        None,
        None,
        "parent",
    )
    turn = asyncio.create_task(
        respond("leader", "Do work", config=config, runtime_paths=paths, identity=identity, run_id="parent"),
    )
    try:
        await asyncio.wait_for(ProbeTools.running.wait(), 10)
        store = create_session_storage("leader", config, paths, execution_identity=identity)
        checkpoint = store.get_run("parent")
    finally:
        # A restart stops the turn, which stores no interrupted record for a turn its replay continues.
        request_task_cancel(turn, process_shutdown=True)
        with suppress(asyncio.CancelledError):
            await turn

    assert checkpoint is not None
    assert checkpoint.status is RunStatus.cancelled
    assert [call["id"] for message in checkpoint.messages for call in message.tool_calls or ()] == [
        "delegate",
        "read",
        "shell",
    ]
    session = store.load_session("parent")
    assert session is not None
    assert recover_interrupted_run(store, session, checkpoint)
    recovered = store.get_run("parent")
    assert recovered is not None
    assert [(message.role, message.tool_call_id) for message in recovered.messages] == [
        ("user", None),
        ("assistant", None),
        ("tool", "delegate"),
        ("assistant", None),
        ("tool", "read"),
        ("assistant", None),
        ("tool", "shell"),
    ]
    assert "helped" in str(recovered.messages[2].content)
    assert recovered.messages[-1].content == INTERRUPTED_CALL_RESULT


@pytest.mark.asyncio
async def test_a_large_checkpoint_recovers_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run holding megabytes checkpoints and recovers every byte; a tool result that large is stored as a file."""
    path = "p" * 4_000_000
    await _crash_mid_call(tmp_path, monkeypatch, _crashing_turns(path))
    crashed = _config(tmp_path / "crashed")

    assert _recover(crashed)

    run = _only_run(crashed)
    assert json.loads(run.messages[1].tool_calls[0]["function"]["arguments"]) == {"path": path}
    assert [tool.tool_args for tool in run.tools or ()] == [{"path": path}, {"cmd": "sleep 60"}]
    assert run.messages[-1].content == INTERRUPTED_CALL_RESULT


@pytest.mark.asyncio
async def test_a_recovered_run_counts_the_usage_its_checkpoints_left_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkpoint records no usage; recovering the run records what its finished requests used, once."""
    await _crash_mid_call(tmp_path, monkeypatch, _crashing_turns())
    crashed = _config(tmp_path / "crashed")

    def recorded() -> list[int]:
        with sqlite3.connect(tmp_path / "crashed/agents/general/sessions/general.db") as connection:
            rows = connection.execute("SELECT usage_data FROM general_sessions_usage").fetchall()
        return [json.loads(row[0])["metrics"]["total_tokens"] for row in rows]

    assert recorded() == []
    assert _recover(crashed)
    assert recorded() == [30]
    assert _recover(crashed)
    assert recorded() == [30]
