"""A run's checkpoints, and the recovery of a run a restart or its user cut short (``history.run_checkpoint``)."""

from __future__ import annotations

import asyncio
import html
import json
import shutil
import sqlite3
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom import loop_hooks
from mindroom import reply_lifecycle as rl
from mindroom.ai import ai_response
from mindroom.cancellation import request_task_cancel
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import CONTINUED_RUNS_METADATA_KEY, resolve_runtime_paths
from mindroom.dispatch_source import ScheduledHistoryBudget
from mindroom.history.interrupted_replay import is_interrupted_replay_run
from mindroom.history.run_checkpoint import (
    INTERRUPTED_CALL_RESULT,
    STOPPED_BY_USER_NOTE,
    is_recovered_run,
    link_response_event,
    recover_interrupted_run,
    recover_interrupted_turn,
    save_run_checkpoint,
)
from mindroom.history.storage import new_scope_session, remove_history_of_redacted_events
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message, model_provenance
from mindroom.llm.provider import TextDelta, ToolCallReady, TurnCompleted, UsageReported
from mindroom.llm.request_identity import request_prefixes
from mindroom.llm.runs import RunRecord, RunStatus, ToolExecution
from mindroom.llm.usage import RunMetrics
from mindroom.providers.anthropic.wire import format_claude_messages
from mindroom.providers.claude_wire_blocks import CLAUDE_BLOCKS_KEY
from mindroom.reply_turn.enrichment import interrupted_attempt_model_prompt
from mindroom.response_turn import TurnSinks, build_matrix_run_metadata
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.summary_rows import Fold, append_summary
from mindroom.tool_system.events import ToolTraceEntry
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.usage.storage import run_usage_snapshot
from tests.access_schema_support import with_responder_access
from tests.conftest import bind_runtime_paths, install_model_providers, make_turn_context, runtime_paths_for
from tests.delegation_helpers import ScriptedAgents, _runtime_paths, native_delegation_config, respond
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call, usage
from tests.history_helpers import compact_runs
from tests.identity_helpers import entity_ids
from tests.scripted_agent_helpers import PROBE_TOOL, ProbeTools, registered_probe_tools, stored_runs

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Sequence
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


async def _respond(
    config: Config,
    prompt: str = "Do the work",
    event_id: str = _EVENT,
    *,
    model_prompt: str | None = None,
    scheduled_history_budget: ScheduledHistoryBudget | None = None,
    sinks: TurnSinks | None = None,
) -> None:
    ctx = replace(
        make_turn_context(
            "general",
            session_id=_SESSION,
            matrix_run_metadata=build_matrix_run_metadata(event_id, []),
            scheduled_history_budget=scheduled_history_budget,
        ),
        prompt=prompt,
        model_prompt=model_prompt,
    )
    await ai_response(ctx, runtime_paths_for(config), config, sinks=sinks)


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


def _recover(config: Config, *, natively: bool = True) -> list[str]:
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

    assert _recover(crashed) == [checkpoint.run_id]

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
    assert _recover(crashed) == [checkpoint.run_id]
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
async def test_a_replay_s_prompt_lists_the_recovered_calls_the_agent_s_history_limit_leaves_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovered run is history like any run, so the replay's prompt lists its calls whatever history keeps."""
    await _crash_mid_call(tmp_path, monkeypatch, _crashing_turns())
    crashed = _config(tmp_path / "crashed")
    crashed.agents["general"].max_tool_calls_from_history = 0
    assert _recover(crashed)
    # The account a replay's prompt gets from the reply's records, which hold each call as a preview.
    account = interrupted_attempt_model_prompt(
        "Do the work",
        span_kind=rl.SpanKind.REPLAY,
        unfinished=None,
        recorded=(
            ToolTraceEntry(type="tool_call_completed", tool_name="read_file", result_preview="contents of a"),
            ToolTraceEntry(type="tool_call_started", tool_name="run_shell_command", args_preview="cmd=sleep 60"),
        ),
    )
    model = ScriptedModel([answer("Finished")])
    install_model_providers(monkeypatch, {"default": model})

    await _respond(crashed, model_prompt=account)

    [request] = model.requests
    assert not [message for message in request.messages if message.tool_calls or message.role == "tool"]
    prompt = html.unescape(str(request.messages[-1].content))
    assert 'The `read_file` tool finished with output preview "contents of a".' in prompt
    assert 'The `run_shell_command` tool was still running with input preview "cmd=sleep 60"' in prompt
    assert "their effects stand: do not call a finished tool with side effects again" in prompt


@pytest.mark.asyncio
async def test_a_scheduled_turn_without_history_replays_nothing_of_another_turn_s_recovered_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovered run follows the limits of history, so a turn that replays none sends none of it."""
    await _crash_mid_call(tmp_path, monkeypatch, _crashing_turns("the-other-task"))
    crashed = _config(tmp_path / "crashed")
    assert _recover(crashed)
    model = ScriptedModel([answer("Scheduled answer")])
    install_model_providers(monkeypatch, {"default": model})

    await _respond(
        crashed,
        prompt="An independent scheduled task",
        event_id="$scheduled",
        scheduled_history_budget=ScheduledHistoryBudget(limit=0, source_event_id="$scheduled"),
    )

    [request] = model.requests
    assert [message.role for message in request.messages if message.role != "system"] == ["user"]
    assert not any(message.from_history for message in request.messages)
    assert "the-other-task" not in str([message.content for message in request.messages])


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
    [recovered] = _recover(crashed)

    # The run stays linked to the turn's answer, which a redaction removes it with.
    assert _recover(crashed, natively=False) == [recovered]

    run = _only_run(crashed)
    assert run.status is RunStatus.error
    assert "kept being interrupted" in str(run.error)
    # A later recovery does not bring the run back.
    _recover(crashed)
    assert _only_run(crashed).status is RunStatus.error
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
    """A crashed turn nothing recovers, as one past the limit, replays none of its messages; its answer links it."""
    await _crash_mid_call(tmp_path, monkeypatch, _crashing_turns())
    crashed = _config(tmp_path / "crashed")

    assert _recover(crashed, natively=False) == [_only_run(crashed).run_id]

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


def _stored_attempt(run_id: str, status: RunStatus, continued: Sequence[str] = ()) -> RunRecord:
    """Return an attempt at ``_EVENT``'s turn as its run stores it: cut short after a call, or finished."""
    call = tool_call("read_file", f"{run_id}-read", path="private.txt")
    work = [
        Message(role="assistant", tool_calls=[call]),
        Message(role="tool", tool_call_id=call["id"], tool_name="read_file", content="PRIVATE_RESULT"),
    ]
    answered = [Message(role="assistant", content="Finished")]
    return RunRecord(
        run_id=run_id,
        agent_id="general",
        session_id=_SESSION,
        status=status,
        metadata=build_matrix_run_metadata(_EVENT, [], extra_metadata={CONTINUED_RUNS_METADATA_KEY: list(continued)}),
        messages=[Message(role="user", content="Read it"), *(work if status is RunStatus.cancelled else answered)],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("second_attempt_stored", [True, False], ids=["second attempt stored", "second attempt lost"])
@pytest.mark.parametrize("natively", [True, False], ids=["recovered again", "past the recovery limit"])
async def test_redacting_a_reply_removes_the_recovered_attempt_a_compaction_folded(
    tmp_path: Path,
    second_attempt_stored: bool,
    natively: bool,
) -> None:
    """A recovered attempt a later attempt's compaction folded is still linked to the reply that finishes the turn."""
    config = _config(tmp_path)
    store = _store(config)
    session = new_scope_session(session_id=_SESSION, scope=_SCOPE)
    store.save_run(session, _stored_attempt("attempt-1", RunStatus.cancelled), record_usage=False)
    assert _recover(config) == ["attempt-1"]
    # The second attempt's compaction folds the first before it is cut short too, with or without a checkpoint.
    session = store.load_session(_SESSION)
    assert session is not None
    compact_runs(store, session, ["attempt-1"], "The first attempt read PRIVATE_RESULT")
    if second_attempt_stored:
        store.save_run(session, _stored_attempt("attempt-2", RunStatus.cancelled, ["attempt-1"]), record_usage=False)

    continued = _recover(config, natively=natively)

    assert continued == ["attempt-1", *(["attempt-2"] if second_attempt_stored else [])]
    session = store.load_session(_SESSION)
    assert session is not None
    store.save_run(session, _stored_attempt("final", RunStatus.completed, continued), record_usage=False)
    link_response_event(store, _SESSION, "final", "$reply")

    async def redacted(event_ids: Collection[str]) -> dict[str, None]:
        return {"$reply": None} if "$reply" in event_ids else {}

    assert await remove_history_of_redacted_events(store, session, redacted) == ["$reply"]
    after = store.load_session(_SESSION)
    assert after is not None
    assert after.summary is None
    assert "PRIVATE_RESULT" not in str([run.messages for run in after.runs if run.status is RunStatus.completed])


@pytest.mark.asyncio
@pytest.mark.parametrize("natively", [True, False], ids=["recovered", "past the recovery limit"])
async def test_redacting_a_reply_removes_an_attempt_whose_calls_a_mid_turn_fold_all_took(
    tmp_path: Path,
    natively: bool,
) -> None:
    """An attempt cut short after a mid-turn fold took all its calls is recovered and stays linked to the reply."""
    config = _config(tmp_path)
    store = _store(config)
    session = new_scope_session(session_id=_SESSION, scope=_SCOPE)
    run = _stored_attempt("crashed", RunStatus.cancelled)
    run.tools = [ToolExecution(tool_call_id="crashed-read", tool_name="read_file", result="PRIVATE_RESULT")]
    await save_run_checkpoint(store, session, run)
    fold = Fold(run=run, messages=tuple(run.messages[1:]), member_run_ids=frozenset())
    assert append_summary(
        store,
        session_id=_SESSION,
        scope_key=_SCOPE.key,
        based_on=None,
        covered=[],
        summary="PRIVATE_RESULT from this turn",
        fold=fold,
        session=session,
    )
    # The checkpoint before the next request keeps only the prompt; the fold's summary holds the calls.
    await save_run_checkpoint(store, session, replace(run, messages=run.messages[:1]))

    continued = _recover(config)
    if not natively:
        # The turn keeps being interrupted; the run leaves history, but its fold's summary stays.
        continued = _recover(config, natively=False)

    assert continued == ["crashed"]
    session = store.load_session(_SESSION)
    assert session is not None
    store.save_run(session, _stored_attempt("final", RunStatus.completed, continued), record_usage=False)
    link_response_event(store, _SESSION, "final", "$reply")

    async def redacted(event_ids: Collection[str]) -> dict[str, None]:
        return {"$reply": None} if "$reply" in event_ids else {}

    linked = store.load_session(_SESSION)
    assert linked is not None
    assert await remove_history_of_redacted_events(store, linked, redacted) == ["$reply"]
    after = store.load_session(_SESSION)
    assert after is not None
    assert after.summary is None


def test_a_recovered_run_keeps_the_usage_of_a_cut_off_response(tmp_path: Path) -> None:
    """The empty message a response cut off mid-stream left goes, and the run keeps that request's usage."""
    config = _config(tmp_path)
    store = _store(config)
    session = new_scope_session(session_id=_SESSION, scope=_SCOPE)
    run = _stored_attempt("cancelled", RunStatus.cancelled)
    run.model, run.model_provider, run.metrics = "test-model", "openai", RunMetrics()
    cut_off = Message(role="assistant")
    run.messages.append(cut_off)
    for message in (run.messages[1], cut_off):
        message.metrics = usage(10, 5)
        message.provider_data = model_provenance(run.model, run.model_provider)
        run.metrics.add(message.metrics, model_id=run.model, provider=run.model_provider)
    store.save_run(session, run, record_usage=True)

    assert recover_interrupted_run(store, session, store.get_run(run.run_id))

    recovered = store.get_run(run.run_id)
    assert cut_off.id not in {message.id for message in recovered.messages}
    requests = run_usage_snapshot(recovered)["requests"]
    assert sum(request["metrics"]["total_tokens"] for request in requests) == 30


def _hanging(reached: asyncio.Event) -> Await:
    """Return a scripted step at which a response sets ``reached`` and then keeps streaming nothing."""

    async def hang() -> None:
        reached.set()
        await asyncio.Event().wait()

    return Await(hang)


async def _stop(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    turns: list[Sequence[Any]],
    *,
    reached: asyncio.Event | None = None,
    by_user: bool = True,
    shutdown: bool = False,
) -> ModelRequest:
    """Run a turn until its shell call keeps running, or ``reached`` is set, and cancel it; return its last request.

    The turn's reply shows as ``$reply``. Its user's Stop cancels it, unless ``by_user`` is off, as when the reply
    moves on without the turn, or ``shutdown`` is on, as when the process shuts down.
    """
    model = ScriptedModel(turns)
    install_model_providers(monkeypatch, {"default": model})
    sinks = TurnSinks(response_event_id=lambda: "$reply", stopped_by_user=lambda: by_user)
    turn = asyncio.create_task(_respond(config, sinks=sinks))
    await asyncio.wait_for((reached or ProbeTools.running).wait(), 10)
    request_task_cancel(turn, process_shutdown=shutdown)
    with suppress(asyncio.CancelledError):
        await turn
    return model.requests[-1]


def _recorded_usage(storage: Path) -> list[int]:
    """Return the total tokens of each run the usage ledger of the agent stored under ``storage`` holds."""
    with sqlite3.connect(storage / "agents/general/sessions/general.db") as connection:
        rows = connection.execute("SELECT usage_data FROM general_sessions_usage").fetchall()
    return [json.loads(row[0])["metrics"]["total_tokens"] for row in rows]


@pytest.mark.asyncio
async def test_a_reply_its_user_stops_mid_call_is_stored_as_its_own_messages_with_a_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stopped reply's run finishes as its own messages, the call in flight answered, then the note of the stop."""
    config = _config(tmp_path)

    await _stop(config, monkeypatch, _crashing_turns())

    run = _only_run(config)
    assert run.status is RunStatus.completed
    assert not is_interrupted_replay_run(run)
    assert [(message.role, message.tool_call_id, message.content) for message in run.messages] == [
        ("user", None, "Do the work"),
        ("assistant", None, None),
        ("tool", "read", "contents of a"),
        ("assistant", None, None),
        ("tool", "shell", INTERRUPTED_CALL_RESULT),
        ("assistant", None, STOPPED_BY_USER_NOTE),
    ]
    assert "stopped by the user" in STOPPED_BY_USER_NOTE
    assert [message.reasoning_content for message in run.messages if message.tool_calls] == ["I should look first."] * 2
    # The run's usage, which its own cancelled record already recorded, counts once.
    assert _recorded_usage(tmp_path) == [30]


@pytest.mark.asyncio
async def test_the_turn_after_a_stopped_reply_replays_the_bytes_the_stopped_run_sent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next request starts with the stopped run's last request, then its last call, that answer, and the note."""
    config = _config(tmp_path)
    last_sent = await _stop(config, monkeypatch, _crashing_turns())
    model = ScriptedModel([answer("Finished")])
    install_model_providers(monkeypatch, {"default": model})

    await _respond(config, prompt="Carry on", event_id="$next")

    [following] = model.requests
    sent, replayed = _wire(last_sent), _wire(following)
    assert len(sent) == 3
    assert "signature-1" in sent[1]
    assert replayed[: len(sent)] == sent
    assert request_prefixes(model, following)[: len(last_sent.messages) + 1] == request_prefixes(model, last_sent)
    after = [json.loads(turn) for turn in replayed[len(sent) :]]
    assert [turn["role"] for turn in after] == ["assistant", "user", "assistant", "user"]
    assert after[0]["content"][0]["signature"] == "signature-2"
    assert after[1]["content"][0]["content"] == INTERRUPTED_CALL_RESULT
    # The model reads that its user stopped the reply, after everything the stopped run sent and before the new prompt.
    assert STOPPED_BY_USER_NOTE in json.dumps(after[2]["content"])
    assert "Carry on" in json.dumps(after[3]["content"])


@pytest.mark.asyncio
async def test_a_reply_stopped_mid_response_keeps_the_text_it_had_streamed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The text of the response a stop cut off, which the reply showed, stays in the run before the note."""
    config = _config(tmp_path)
    streaming = asyncio.Event()
    turns = [
        _thinking_call(tool_call("read_file", "read", path="a"), signature="signature-1"),
        [TextDelta("The file "), TextDelta("says"), _hanging(streaming)],
    ]

    last_sent = await _stop(config, monkeypatch, turns, reached=streaming)

    run = _only_run(config)
    assert [(message.role, message.content) for message in run.messages] == [
        ("user", "Do the work"),
        ("assistant", None),
        ("tool", "contents of a"),
        ("assistant", f"The file says\n\n{STOPPED_BY_USER_NOTE}"),
    ]
    model = ScriptedModel([answer("Finished")])
    install_model_providers(monkeypatch, {"default": model})
    await _respond(config, prompt="Carry on", event_id="$next")
    sent, replayed = _wire(last_sent), _wire(model.requests[0])
    assert replayed[: len(sent)] == sent
    assert "The file says" in replayed[len(sent)]


@pytest.mark.asyncio
async def test_redacting_a_stopped_reply_removes_its_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stopped run is linked to the message that shows the reply, so redacting the message removes it."""
    config = _config(tmp_path)
    await _stop(config, monkeypatch, _crashing_turns())
    store = _store(config)
    session = store.load_session(_SESSION)
    assert session is not None
    assert [run.status for run in session.runs] == [RunStatus.completed]

    async def redacted(event_ids: Collection[str]) -> dict[str, None]:
        return {"$reply": None} if "$reply" in event_ids else {}

    assert await remove_history_of_redacted_events(store, session, redacted) == ["$reply"]
    after = store.load_session(_SESSION)
    assert after is not None
    assert after.runs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [(), (TextDelta("Let me "), TextDelta("look"))], ids=["no response", "text only"])
async def test_a_reply_stopped_before_it_called_a_tool_is_stored_as_an_account_of_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streamed: tuple[TextDelta, ...],
) -> None:
    """A stopped run that called no tool holds no work to keep, so the turn stores the prose record of what it showed."""
    config = _config(tmp_path)
    streaming = asyncio.Event()

    await _stop(config, monkeypatch, [[*streamed, _hanging(streaming)]], reached=streaming)

    run = _only_run(config)
    assert is_interrupted_replay_run(run)
    assert not is_recovered_run(run)
    assert [message.role for message in run.messages] == ["user", "assistant"]
    assert run.messages[-1].content == "\n\n".join(
        part for part in ("".join(delta.text for delta in streamed), "(turn stopped before completion)") if part
    )


@pytest.mark.asyncio
async def test_a_cancellation_that_is_no_stop_of_its_user_is_stored_as_an_account_of_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply that moves on without its turn, as one whose delivery failed, is no user's stop: its record is prose."""
    config = _config(tmp_path)

    await _stop(config, monkeypatch, _crashing_turns(), by_user=False)

    run = _only_run(config)
    assert is_interrupted_replay_run(run)
    assert STOPPED_BY_USER_NOTE not in str(run.messages[-1].content)
    assert "(turn stopped before completion; 1 tool call(s) had finished" in str(run.messages[-1].content)


@pytest.mark.asyncio
async def test_a_process_shutdown_mid_response_stores_no_stop_and_recovers_without_the_cut_off_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shutdown leaves the run cancelled for its replay, whose recovery adds no note and keeps no cut-off text."""
    config = _config(tmp_path)
    streaming = asyncio.Event()
    turns = [
        _thinking_call(tool_call("read_file", "read", path="a"), signature="signature-1"),
        [TextDelta("The file "), TextDelta("says"), _hanging(streaming)],
    ]

    await _stop(config, monkeypatch, turns, reached=streaming, shutdown=True)

    cancelled = _only_run(config)
    assert cancelled.status is RunStatus.cancelled
    assert _recover(config) == [cancelled.run_id]
    assert [(message.role, message.content) for message in _only_run(config).messages] == [
        ("user", "Do the work"),
        ("assistant", None),
        ("tool", "contents of a"),
    ]
