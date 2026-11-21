"""The ``claude_code`` route against a scripted Claude Code process, through the real Claude Agent SDK."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from claude_agent_sdk import ClaudeSDKClient, CLIConnectionError, RateLimitEvent, RateLimitInfo
from structlog.testing import capture_logs

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.llm.errors import (
    QUOTA_EXHAUSTED,
    AuthenticationFailed,
    ContextWindowExceeded,
    ProviderError,
    ProviderUnavailable,
)
from mindroom.llm.messages import QUEUED_MESSAGE_NOTICE_MARKER_KEY, Message, read_model_provenance
from mindroom.llm.provider import (
    ModelRequest,
    ReasoningDelta,
    RequestOverrides,
    TextDelta,
    ToolCallReady,
    ToolSpec,
    TurnCompleted,
    UsageReported,
    complete,
)
from mindroom.providers.claude_code import session as session_module
from mindroom.providers.claude_code.session import SESSIONS
from tests.providers.claude_code.support import (
    CONVERSATION,
    HOME,
    LOOKUP,
    call,
    fake_cli,
    left_behind,
    result,
    text_turn,
    user,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider, StreamEvent


_SYSTEM = Message(role="system", content="You are the test agent.")
_TWO_CALLS = [{"text": "Looking.", "tools": [call("toolu_a", "a"), call("toolu_b", "b")]}]
_OTHER = ToolSpec(name="other", description="Another tool.", parameters=LOOKUP.parameters, strict=None)


async def _events(provider: ModelProvider, request: ModelRequest) -> list[StreamEvent]:
    return [event async for event in provider.stream(request)]


def _turn(events: list[StreamEvent]) -> TurnCompleted:
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    return completed


async def _closed() -> None:
    await wait_for_background_tasks(owner=SESSIONS)


async def _resting_process_breaks() -> None:
    """Wait until the one resting process can no longer be continued."""
    [session] = SESSIONS.resting.values()
    async with asyncio.timeout(10):
        while session.broken() is None:  # noqa: ASYNC110 - the other side is another process
            await asyncio.sleep(0.01)


# --- one answer -------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_text_answer_streams_its_reasoning_and_text_and_reports_usage_once(tmp_path: Path) -> None:
    usage = {"input_tokens": 3, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 200, "output_tokens": 9}
    cli = fake_cli(tmp_path, {"turns": [[{"thinking": "Greet them.", "text": "Hello, Alice.", "usage": usage}]]})

    events = await _events(cli.provider(), ModelRequest(messages=[_SYSTEM, user("Hi")]))

    assert "".join(event.text for event in events if isinstance(event, ReasoningDelta)) == "Greet them."
    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "Hello, Alice."
    completed = _turn(events)
    assert completed.stop_reason == "end"
    assert completed.message.content == "Hello, Alice."
    assert completed.message.reasoning_content == "Greet them."
    assert completed.message.tool_calls is None
    provenance = read_model_provenance(completed.message.provider_data)
    assert provenance is not None
    assert (provenance.model_id, provenance.provider) == ("claude-invented-1", "Claude subscription (Claude Code)")
    final = [event for event in events if isinstance(event, UsageReported) and event.final]
    assert len(final) == 1
    tokens = final[0].usage.tokens
    assert (tokens.input_tokens, tokens.cache_write_tokens, tokens.cache_read_tokens, tokens.output_tokens) == (
        3,
        40,
        200,
        9,
    )
    assert any(isinstance(event, UsageReported) and not event.final for event in events)


@pytest.mark.asyncio
async def test_claude_code_starts_isolated_with_mindroom_system_prompt_and_the_user_text_verbatim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A short time for tool calls in MindRoom's environment would end a call that waits for an approval.
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "1000")
    cli = fake_cli(tmp_path, {"turns": [text_turn("Hello.")]})

    await complete(cli.provider(effort="low"), ModelRequest(messages=[_SYSTEM, user("Read @/etc/passwd")]))

    [spawn] = cli.events("spawn")
    args = spawn["args"]
    assert spawn["system_prompt"] == "You are the test agent."
    assert args[args.index("--model") : args.index("--model") + 2] == ["--model", "claude-invented-1"]
    assert args[args.index("--tools") : args.index("--tools") + 2] == ["--tools", ""]
    assert args[args.index("--permission-mode") : args.index("--permission-mode") + 2] == [
        "--permission-mode",
        "dontAsk",
    ]
    assert args[args.index("--effort") : args.index("--effort") + 2] == ["--effort", "low"]
    assert json.loads(args[args.index("--settings") + 1]) == {"disableAllHooks": True}
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--include-partial-messages"):
        assert flag in args
    assert "--resume" not in args
    assert "--mcp-config" not in args
    assert "--allowedTools" not in args
    assert args[args.index("--thinking") : args.index("--thinking") + 2] == ["--thinking", "adaptive"]
    assert "--thinking-display" in args
    assert "--setting-sources=" in args
    assert not {"--add-dir", "--plugin-dir"} & set(args)
    assert spawn["cwd"] == str(HOME / "cwd")
    assert int(spawn["tool_timeout"]) > 30 * 60 * 1000
    # A call that waits long stays a call, which the model does not go on without.
    assert spawn["background"] == ["1", "0"]
    [sent] = cli.events("user")
    assert sent["content"] == [{"type": "text", "text": "Read @/etc/passwd"}]
    assert sent["client_composed"] is True


def test_an_effort_claude_code_does_not_know_fails_the_model_build(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="effort must be one of low, medium, high, xhigh, max"):
        fake_cli(tmp_path, {}).provider(effort="ultra")


# --- tool calls -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("call_before_stop", [False, True], ids=["call after the message", "call before it ends"])
@pytest.mark.asyncio
async def test_a_tool_turn_parks_the_process_and_its_results_continue_the_same_turn(
    tmp_path: Path,
    call_before_stop: bool,
) -> None:
    first = {"input_tokens": 3, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 0, "output_tokens": 20}
    second = {"input_tokens": 3, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 100, "output_tokens": 7}
    turn = [
        {**_TWO_CALLS[0], "usage": first, "call_before_stop": call_before_stop},
        {"text": "{toolu_a}+{toolu_b}", "usage": second},
    ]
    cli = fake_cli(tmp_path, {"turns": [turn]})
    provider = cli.provider()
    messages = [_SYSTEM, user("Look up a and b.")]

    events = await _events(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    asked = _turn(events)
    assert asked.stop_reason == "tool_use"
    assert asked.message.content == "Looking."
    assert asked.message.tool_calls == [
        {"id": "toolu_a", "type": "function", "function": {"name": "lookup", "arguments": '{"key": "a"}'}},
        {"id": "toolu_b", "type": "function", "function": {"name": "lookup", "arguments": '{"key": "b"}'}},
    ]
    assert [event.call for event in events if isinstance(event, ToolCallReady)] == asked.message.tool_calls
    assert asked.message.metrics.tokens.output_tokens == 20
    assert cli.events("tool_result") == []

    messages += [asked.message, result("toolu_b", "LEMON-IBIS"), result("toolu_a", "LILAC-HERON")]
    answered = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    assert answered.content == "LILAC-HERON+LEMON-IBIS"
    assert answered.stop_reason == "end"
    assert (answered.usage.tokens.cache_read_tokens, answered.usage.tokens.output_tokens) == (100, 7)
    assert len(cli.events("spawn")) == 1
    assert [(entry["id"], entry["result"]["content"][0]["text"]) for entry in cli.events("tool_result")] == [
        ("toolu_a", "LILAC-HERON"),
        ("toolu_b", "LEMON-IBIS"),
    ]
    [listed] = cli.events("tools_list")
    [tool] = listed["tools"]
    assert (tool["name"], tool["description"], tool["inputSchema"]) == (
        "lookup",
        "Look up a marker by key.",
        dict(LOOKUP.parameters),
    )
    assert tool["_meta"]["anthropic/alwaysLoad"] is True
    args = cli.events("spawn")[0]["args"]
    assert args[args.index("--allowedTools") + 1] == "mcp__mindroom__*"


@pytest.mark.asyncio
async def test_a_failed_tool_result_reaches_claude_code_as_an_error(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [[{"tools": [call("toolu_a", "a")]}, {"text": "It failed."}]]})
    provider = cli.provider()
    messages = [user("Look up a.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    failed = Message(role="tool", content="no such key", tool_call_id="toolu_a", tool_call_error=True)
    await complete(provider, ModelRequest(messages=[*messages, asked.message, failed], tools=[LOOKUP]))

    [entry] = cli.events("tool_result")
    assert entry["result"]["isError"] is True
    assert entry["result"]["content"] == [{"type": "text", "text": "no such key"}]


@pytest.mark.asyncio
async def test_a_request_that_no_longer_matches_the_parked_turn_rebuilds_in_a_new_process(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [_TWO_CALLS]}, {"turns": [text_turn("Rebuilt.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    asked = await complete(provider, ModelRequest(messages=[user("Look up a and b.")], tools=[LOOKUP]))

    # A compaction replaced the history the parked process answered.
    compacted = [
        user("Summary: the user wants the markers for a and b."),
        asked.message,
        result("toolu_a", "LILAC-HERON"),
        result("toolu_b", "LEMON-IBIS"),
    ]
    answered = await complete(provider, ModelRequest(messages=compacted, tools=[LOOKUP]))
    await _closed()

    assert answered.content == "Rebuilt."
    assert len(cli.events("spawn")) == 2
    assert cli.events("tool_result") == []
    # The process that waited on those calls can never be continued, so it is closed at once.
    assert len(cli.events("exit")) == 1
    assert [session.emitted.content for session in SESSIONS.resting.values() if session.emitted] == ["Rebuilt."]
    rebuilt = cli.events("user")[1]["content"][0]["text"]
    assert rebuilt.startswith("<conversation-checkpoint>")
    assert '"output": "LILAC-HERON"' in rebuilt
    assert '"output": "LEMON-IBIS"' in rebuilt


@pytest.mark.asyncio
async def test_results_that_do_not_answer_every_parked_call_rebuild_and_retire_the_process(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [_TWO_CALLS]}, {"turns": [text_turn("Rebuilt.")]})
    provider = cli.provider()
    messages = [user("Look up a and b.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    partial = [*messages, asked.message, result("toolu_a", "LILAC-HERON")]
    answered = await complete(provider, ModelRequest(messages=partial, tools=[LOOKUP]))
    await _closed()

    assert answered.content == "Rebuilt."
    assert len(cli.events("spawn")) == 2
    assert cli.events("tool_result") == []
    assert len(cli.events("exit")) == 2


@pytest.mark.asyncio
async def test_a_parked_turn_edited_under_the_same_call_id_is_rebuilt_and_its_process_closed(tmp_path: Path) -> None:
    turn = [{"tools": [call("toolu_a", "a")]}, {"text": "never"}]
    cli = fake_cli(tmp_path, {"turns": [turn]}, {"turns": [text_turn("Rebuilt.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    messages = [user("Look up a.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    function = {"name": "lookup", "arguments": '{"key": "z"}'}
    edited = replace(asked.message, tool_calls=[{"id": "toolu_a", "type": "function", "function": function}])
    messages += [edited, result("toolu_a", "LILAC-HERON")]
    answered = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    await _closed()

    assert answered.content == "Rebuilt."
    assert cli.events("tool_result") == []
    assert len(cli.events("exit")) == 1
    assert '"input": {"key": "z"}' in cli.events("user")[1]["content"][0]["text"]


@pytest.mark.asyncio
async def test_a_request_that_replays_its_history_explicitly_starts_a_new_process(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]}, {"turns": [text_turn("Two.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    messages = [user("What is a?")]
    first = await complete(provider, ModelRequest(messages=messages))

    messages += [first.message, user("And b?")]
    second = await complete(provider, ModelRequest(messages=messages, portable_replay=True))
    await _closed()

    assert second.content == "Two."
    assert len(cli.events("spawn")) == 2
    assert len(cli.events("exit")) == 1


# --- the tool server ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "presented",
    [{"input": {"key": "a"}}, {"name": "other"}],
    ids=["with other arguments", "for another tool"],
)
@pytest.mark.asyncio
async def test_a_call_that_reaches_the_tool_server_unlike_the_model_made_it_gets_no_result(
    tmp_path: Path,
    presented: dict[str, Any],
) -> None:
    turn = [{"tools": [call("toolu_a", "a"), {**call("toolu_b", "b"), "mcp": presented}]}, {"text": "never"}]
    cli = fake_cli(tmp_path, {"turns": [turn]})
    provider = cli.provider()
    tools = [LOOKUP, _OTHER]
    messages = [user("Look up a and b.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=tools))

    messages += [asked.message, result("toolu_a", "LILAC-HERON"), result("toolu_b", "LEMON-IBIS")]
    with pytest.raises(ProviderUnavailable, match="toolu_b"):
        await complete(provider, ModelRequest(messages=messages, tools=tools))
    await _closed()

    delivered = cli.events("tool_result")
    assert delivered[0]["result"]["content"][0]["text"] == "LILAC-HERON"
    assert all(entry["result"]["isError"] for entry in delivered[1:])
    assert "LEMON-IBIS" not in cli.log.read_text()
    assert len(cli.events("exit")) == 1


@pytest.mark.asyncio
async def test_a_call_that_already_returned_is_not_answered_a_second_time(tmp_path: Path) -> None:
    turn = [{**_TWO_CALLS[0], "repeat": "toolu_a"}, {"text": "never"}]
    cli = fake_cli(tmp_path, {"turns": [turn]})
    provider = cli.provider()
    messages = [user("Look up a and b.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    messages += [asked.message, result("toolu_a", "LILAC-HERON"), result("toolu_b", "LEMON-IBIS")]
    with pytest.raises(ProviderUnavailable, match="toolu_a twice"):
        await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    await _closed()

    delivered = cli.events("tool_result")
    assert [entry["result"]["content"][0]["text"] for entry in delivered[:2]] == ["LILAC-HERON", "LEMON-IBIS"]
    assert all(entry["result"]["isError"] for entry in delivered[2:])
    assert cli.log.read_text().count("LILAC-HERON") == 1


@pytest.mark.asyncio
async def test_a_call_the_model_never_made_gets_no_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "_UNMADE_CALL_SECONDS", 0.05)
    turn = [{"tools": [call("toolu_a", "a"), {**call("toolu_x", "b"), "hidden": True}]}, {"text": "never"}]
    cli = fake_cli(tmp_path, {"turns": [turn]})
    provider = cli.provider()
    messages = [user("Look up a.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    assert [made["id"] for made in asked.message.tool_calls or ()] == ["toolu_a"]

    messages += [asked.message, result("toolu_a", "LILAC-HERON")]
    with pytest.raises(ProviderUnavailable, match="toolu_x"):
        await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    await _closed()

    assert all(entry["result"]["isError"] for entry in cli.events("tool_result")[1:])


@pytest.mark.asyncio
async def test_a_parked_process_with_a_call_the_model_never_made_is_rebuilt_at_once(tmp_path: Path) -> None:
    # Claude Code brings a call the model did not write to the tool server before the model message ends.
    step = {"tools": [{**call("toolu_x", "b"), "hidden": True}, call("toolu_a", "a")], "call_before_stop": True}
    cli = fake_cli(tmp_path, {"turns": [[step, {"text": "never"}]]}, {"turns": [text_turn("Rebuilt.")]})
    provider = cli.provider()
    messages = [user("Look up a.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    messages += [asked.message, result("toolu_a", "LILAC-HERON")]
    answered = await asyncio.wait_for(complete(provider, ModelRequest(messages=messages, tools=[LOOKUP])), 10)

    assert answered.content == "Rebuilt."
    assert cli.events("tool_result") == []


@pytest.mark.asyncio
async def test_a_result_for_a_call_claude_code_gave_up_meanwhile_is_dropped() -> None:
    bridge = session_module._ToolBridge(names={"lookup": "lookup"})
    bridge.made("toolu_a", "lookup", {"key": "a"})
    bridge.made("toolu_b", "lookup", {"key": "b"})
    bridge.calls["toolu_a"].result.cancel()

    bridge.resolve({"toolu_a": {"content": []}, "toolu_b": {"content": []}})

    assert bridge.calls["toolu_a"].result.cancelled()
    assert bridge.undelivered


@pytest.mark.parametrize(
    "turn",
    [
        [{"tools": [call("toolu_a", "a")], "abandon": True}, {"text": "An answer without the result."}],
        [{"tools": [call("toolu_a", "a")], "cancel": True}, {"wait": True}],
    ],
    ids=["it went on by itself", "it cancelled the call"],
)
@pytest.mark.asyncio
async def test_a_parked_process_that_did_not_wait_for_mindrooms_results_is_rebuilt_from_them(
    tmp_path: Path,
    turn: list[dict[str, Any]],
) -> None:
    cli = fake_cli(tmp_path, {"turns": [turn]}, {"turns": [text_turn("Rebuilt.")]})
    provider = cli.provider()
    messages = [user("Look up a.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    await _resting_process_breaks()

    messages += [asked.message, result("toolu_a", "LILAC-HERON")]
    answered = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    assert answered.content == "Rebuilt."
    assert len(cli.events("spawn")) == 2
    assert '"output": "LILAC-HERON"' in cli.events("user")[1]["content"][0]["text"]


@pytest.mark.asyncio
async def test_a_tool_call_without_claude_codes_call_id_fails_the_request_and_retires_the_process(
    tmp_path: Path,
) -> None:
    step = {
        "tools": [call("toolu_a", "a")],
        "omit_tool_use_id": True,
        "call_before_stop": True,
        "result_before_stop": True,
    }
    cli = fake_cli(tmp_path, {"turns": [[step, {"text": "never"}]]})

    with pytest.raises(ProviderUnavailable, match="call id"):
        await complete(cli.provider(), ModelRequest(messages=[user("Look up a.")], tools=[LOOKUP]))
    await _closed()

    assert len(cli.events("exit")) == 1
    assert SESSIONS.resting == {}


@pytest.mark.asyncio
async def test_a_parked_process_whose_call_came_without_a_call_id_is_rebuilt_not_continued(tmp_path: Path) -> None:
    step = {"tools": [call("toolu_a", "a")], "omit_tool_use_id": True}
    cli = fake_cli(tmp_path, {"turns": [[step, {"text": "never"}]]}, {"turns": [text_turn("Rebuilt.")]})
    provider = cli.provider()
    messages = [user("Look up a.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    while not cli.events("tool_result"):  # noqa: ASYNC110 - the other side is another process
        await asyncio.sleep(0.01)

    messages += [asked.message, result("toolu_a", "LILAC-HERON")]
    answered = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    assert answered.content == "Rebuilt."
    assert [entry["result"]["isError"] for entry in cli.events("tool_result")] == [True]
    assert '"output": "LILAC-HERON"' in cli.events("user")[1]["content"][0]["text"]


@pytest.mark.asyncio
async def test_a_request_without_tool_choice_offers_claude_code_no_tools(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("No tools.")]})
    provider = cli.provider(execution_identity=CONVERSATION)

    await complete(provider, ModelRequest(messages=[user("Hi")], tools=[LOOKUP], tool_choice="none"))
    await _closed()

    args = cli.events("spawn")[0]["args"]
    assert "--mcp-config" not in args
    assert "--allowedTools" not in args
    # Such a request is a one-off, such as a summary: nothing continues its process.
    assert SESSIONS.resting == {}
    assert len(cli.events("exit")) == 1


@pytest.mark.asyncio
async def test_a_summary_request_starts_claude_code_without_a_thinking_setting(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("Summary.")]})
    provider = cli.provider()
    plain = ModelRequest(messages=[user("Summarize.")])
    summary = replace(plain, overrides=RequestOverrides(omit_thinking_setting=True))

    await complete(provider, summary)

    args = cli.events("spawn")[0]["args"]
    assert not {"--thinking", "--thinking-display", "--max-thinking-tokens"} & set(args)
    assert provider.context_key(plain) != provider.context_key(summary)


@pytest.mark.asyncio
async def test_a_tool_description_claude_code_would_cut_is_cut_with_one_warning(tmp_path: Path) -> None:
    long = replace(LOOKUP, name="long_lookup", description="d" * 3000)
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]})
    provider = cli.provider()

    with capture_logs() as logs:
        await complete(provider, ModelRequest(messages=[user("One")], tools=[long]))
        await complete(provider, ModelRequest(messages=[user("Two")], tools=[long]))

    cut = [entry for entry in logs if entry["event"] == "claude_code_tool_description_cut"]
    assert [(entry["tool"], entry["length"], entry["log_level"]) for entry in cut] == [("long_lookup", 3000, "warning")]
    assert "ddd" not in str(cut)
    assert len(cli.events("tools_list")[0]["tools"][0]["description"]) == 2048


# --- what an answer holds ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_that_arrives_whole_is_answered_with_its_text_and_its_tokens(tmp_path: Path) -> None:
    usage = {"input_tokens": 3, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 200, "output_tokens": 9}
    cli = fake_cli(tmp_path, {"turns": [[{"text": "Hello, Alice.", "usage": usage, "complete_only": True}]]})

    answered = await complete(cli.provider(), ModelRequest(messages=[user("Hi")]))

    assert answered.content == "Hello, Alice."
    tokens = answered.usage.tokens
    assert (tokens.input_tokens, tokens.cache_write_tokens, tokens.cache_read_tokens, tokens.output_tokens) == (
        3,
        40,
        200,
        9,
    )


@pytest.mark.asyncio
async def test_text_that_arrives_with_the_start_of_its_block_is_kept(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [[{"text": "Hello, Alice.", "start_text": True}]]})

    events = await _events(cli.provider(), ModelRequest(messages=[user("Hi")]))

    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "Hello, Alice."
    assert _turn(events).message.content == "Hello, Alice."


@pytest.mark.asyncio
async def test_the_texts_of_two_model_messages_in_one_answer_are_kept_apart(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [[{"text": "Let me check."}, {"text": "Done."}]]})

    events = await _events(cli.provider(), ModelRequest(messages=[user("Hi")]))

    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "Let me check.\n\nDone."
    assert _turn(events).message.content == "Let me check.\n\nDone."
    assert _turn(events).message.metrics.tokens.output_tokens == 10


# --- the next turn ----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_idle_process_answers_the_next_user_message_of_its_conversation(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("It is LILAC-HERON."), text_turn("It is LEMON-IBIS.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    messages = [_SYSTEM, user("What is a?")]
    first = await complete(provider, ModelRequest(messages=messages))

    messages += [first.message, user("And b?")]
    second = await complete(provider, ModelRequest(messages=messages))

    assert second.content == "It is LEMON-IBIS."
    assert len(cli.events("spawn")) == 1
    assert cli.events("user")[1]["content"] == [{"type": "text", "text": "And b?"}]


@pytest.mark.asyncio
async def test_a_changed_system_prompt_rebuilds_instead_of_continuing(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]}, {"turns": [text_turn("Two.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    first = await complete(provider, ModelRequest(messages=[_SYSTEM, user("What is a?")]))

    changed = [Message(role="system", content="New rules."), user("What is a?"), first.message, user("And b?")]
    second = await complete(provider, ModelRequest(messages=changed))

    assert second.content == "Two."
    assert [spawn["system_prompt"] for spawn in cli.events("spawn")] == ["You are the test agent.", "New rules."]


@pytest.mark.asyncio
async def test_an_answer_outside_a_conversation_leaves_no_process_behind(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]})

    await complete(cli.provider(), ModelRequest(messages=[user("Hi")]))
    await _closed()

    assert SESSIONS.resting == {}
    assert len(cli.events("exit")) == 1


@pytest.mark.parametrize("same_storage", [True, False], ids=["in the same storage", "in another storage"])
@pytest.mark.asyncio
async def test_a_conversation_is_not_continued_by_another_claude_executable(tmp_path: Path, same_storage: bool) -> None:
    one = fake_cli(tmp_path / "one", {"turns": [text_turn("OK.")]})
    two = fake_cli(tmp_path / "two", {"turns": [text_turn("From two.")]})
    if same_storage:
        two = replace(two, storage_root=one.storage_root)
    messages = [user("Hi")]
    first = await complete(one.provider(execution_identity=CONVERSATION), ModelRequest(messages=messages))

    messages += [first.message, user("More?")]
    second = await complete(two.provider(execution_identity=CONVERSATION), ModelRequest(messages=messages))

    assert second.content == "From two."
    assert len(one.events("user")) == 1


@pytest.mark.asyncio
async def test_an_idle_process_that_died_is_replaced_without_failing_the_next_request(tmp_path: Path) -> None:
    dies = [{"text": "One."}, {"die_after_result": True}]
    cli = fake_cli(tmp_path, {"turns": [dies]}, {"turns": [text_turn("Two.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    messages = [user("What is a?")]
    first = await complete(provider, ModelRequest(messages=messages))
    await _resting_process_breaks()

    second = await complete(provider, ModelRequest(messages=[*messages, first.message, user("And b?")]))

    assert second.content == "Two."
    assert len(cli.events("spawn")) == 2


# --- lifetime ---------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stream_that_is_abandoned_closes_its_process(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("Hello, Alice.")]})

    stream = cli.provider().stream(ModelRequest(messages=[user("Hi")]))
    async for event in stream:
        if isinstance(event, TextDelta):
            break
    await stream.aclose()
    await _closed()

    assert len(cli.events("exit")) == 1
    assert SESSIONS.resting == {}


@pytest.mark.asyncio
async def test_a_process_past_its_time_to_live_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = fake_cli(tmp_path, {"turns": [_TWO_CALLS]}, {"turns": [text_turn("Idle.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    await complete(provider, ModelRequest(messages=[user("Look up a and b.")], tools=[LOOKUP]))
    await complete(provider, ModelRequest(messages=[user("Hi")]))
    assert len(SESSIONS.resting) == 2

    monkeypatch.setattr(session_module, "_IDLE_TTL_SECONDS", 0.0)
    await SESSIONS.close_expired()
    assert [session.emitted.tool_call_ids for session in SESSIONS.resting.values()] == [("toolu_a", "toolu_b")]

    monkeypatch.setattr(session_module, "_PARKED_TTL_SECONDS", 0.0)
    await SESSIONS.close_expired()
    assert SESSIONS.resting == {}
    assert len(cli.events("exit")) == 2


@pytest.mark.asyncio
async def test_a_new_process_past_the_limit_evicts_an_idle_one_before_a_parked_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "_MAX_PROCESSES", 2)
    cli = fake_cli(tmp_path, {"turns": [_TWO_CALLS]}, {"turns": [text_turn("Idle.")]}, {"turns": [text_turn("New.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    await complete(provider, ModelRequest(messages=[user("Look up a and b.")], tools=[LOOKUP]))
    await complete(provider, ModelRequest(messages=[user("Hi")]))

    elsewhere = replace(CONVERSATION, thread_id="$other", resolved_thread_id="$other", session_id="session-2")
    await complete(cli.provider(execution_identity=elsewhere), ModelRequest(messages=[user("Another conversation")]))
    await _closed()

    resting = sorted(session.emitted.content or "" for session in SESSIONS.resting.values())
    assert resting == ["Looking.", "New."]
    assert (len(SESSIONS.live), SESSIONS.starting) == (2, 0)


@pytest.mark.asyncio
async def test_a_request_past_the_process_limit_fails_as_retryable_instead_of_starting_one_more(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "_MAX_PROCESSES", 1)
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]})
    provider = cli.provider()

    outcomes = await asyncio.gather(
        complete(provider, ModelRequest(messages=[user("First")])),
        complete(provider, ModelRequest(messages=[user("Second")])),
        return_exceptions=True,
    )
    await _closed()

    refused = [outcome for outcome in outcomes if isinstance(outcome, ProviderUnavailable)]
    assert len(refused) == 1
    assert "1 Claude Code processes" in str(refused[0])
    assert len(cli.events("spawn")) == 1
    assert (SESSIONS.live, SESSIONS.starting) == (set(), 0)


@pytest.mark.asyncio
async def test_a_system_prompt_is_private_to_the_user_and_gone_once_claude_code_started(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("Hello.")]})
    umask = os.umask(0o022)
    try:
        await complete(cli.provider(execution_identity=CONVERSATION), ModelRequest(messages=[_SYSTEM, user("Hi")]))
    finally:
        os.umask(umask)

    [spawn] = cli.events("spawn")
    assert spawn["system_prompt"] == "You are the test agent."
    assert spawn["prompt_modes"] == [0o600, 0o700]
    # The process still rests, and its prompt is no longer on disk.
    assert len(SESSIONS.resting) == 1
    assert left_behind() == []


@pytest.fixture
def held_start(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold each start where it opens Claude Code: the first event says one got there, the second lets it go on."""
    reached, released = asyncio.Event(), asyncio.Event()
    open_client = session_module.open_client

    async def held(*args: Any) -> asyncio.Task[None]:  # noqa: ANN401
        reached.set()
        await released.wait()
        return await open_client(*args)

    monkeypatch.setattr(session_module, "open_client", held)
    return reached, released


@pytest.mark.asyncio
async def test_a_request_cancelled_while_claude_code_starts_keeps_its_slot_until_the_start_ended(
    tmp_path: Path,
    held_start: tuple[asyncio.Event, asyncio.Event],
) -> None:
    reached, released = held_start
    cli = fake_cli(tmp_path, {"turns": [text_turn("never")]})
    request = asyncio.create_task(complete(cli.provider(), ModelRequest(messages=[_SYSTEM, user("Hi")])))
    await reached.wait()

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    # The start goes on without the request, and holds its slot until its process is gone.
    assert (SESSIONS.live, SESSIONS.starting) == (set(), 1)
    released.set()
    await _closed()
    assert (SESSIONS.live, SESSIONS.starting) == (set(), 0)
    assert len(cli.events("spawn")) == len(cli.events("exit")) == 1
    assert cli.events("user") == []
    assert left_behind() == []


@pytest.mark.asyncio
async def test_a_slot_is_not_free_before_the_process_that_held_it_is_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "_MAX_PROCESSES", 1)
    gone = asyncio.Event()
    close = session_module._close

    async def slow_close(session: session_module.LiveSession) -> None:
        await asyncio.wait_for(gone.wait(), 5)
        await close(session)

    monkeypatch.setattr(session_module, "_close", slow_close)
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]})
    await complete(cli.provider(execution_identity=CONVERSATION), ModelRequest(messages=[user("First")]))
    # The second request takes the slot of the resting process, whose close does not end, and is cancelled.
    second = asyncio.create_task(complete(cli.provider(), ModelRequest(messages=[user("Second")])))
    async with asyncio.timeout(5):
        while SESSIONS.resting:  # noqa: ASYNC110 - the request digests its messages on a thread first
            await asyncio.sleep(0.01)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second

    with pytest.raises(ProviderUnavailable, match="limit of 1 Claude Code processes"):
        await complete(cli.provider(), ModelRequest(messages=[user("Third")]))

    assert len(cli.events("spawn")) == 1
    gone.set()
    await _closed()
    assert (SESSIONS.live, SESSIONS.starting) == (set(), 0)
    assert len(cli.events("spawn")) == len(cli.events("exit")) == 2


# --- failures ---------------------------------------------------------------------------------------------------------


async def _failure(
    tmp_path: Path,
    process: dict[str, Any],
    *,
    asked: bool = True,
    tool_choice: str | None = None,
) -> ProviderError:
    """Return the error of a first request to ``process``, which got the request only when ``asked``."""
    cli = fake_cli(tmp_path, process)
    request = ModelRequest(messages=[user("Hi")], tools=[LOOKUP], tool_choice=tool_choice)
    with pytest.raises(ProviderError) as raised:
        await complete(cli.provider(), request)
    await _closed()
    assert SESSIONS.resting == {}
    assert len(cli.events("exit")) == len(cli.events("spawn"))
    assert bool(cli.events("user")) == asked
    assert left_behind() == []
    return raised.value


@pytest.mark.asyncio
async def test_a_signed_out_claude_code_says_how_to_sign_in_and_sends_nothing(tmp_path: Path) -> None:
    error = await _failure(tmp_path, {"account": {"tokenSource": "none", "apiProvider": "firstParty"}}, asked=False)

    assert isinstance(error, AuthenticationFailed)
    assert str(error) == ("Claude Code is not signed in. Run `claude auth login` as the user that runs MindRoom.")


@pytest.mark.asyncio
async def test_an_api_key_from_the_environment_is_refused_by_name(tmp_path: Path) -> None:
    account = {"tokenSource": "claude.ai", "apiKeySource": "ANTHROPIC_API_KEY", "apiProvider": "firstParty"}

    error = await _failure(tmp_path, {"account": account, "turns": [text_turn("billed to the key")]}, asked=False)

    assert isinstance(error, AuthenticationFailed)
    assert "ANTHROPIC_API_KEY" in str(error)
    assert "provider: anthropic" in str(error)


@pytest.mark.parametrize(
    "account",
    [
        {"tokenSource": "CLAUDE_CODE_OAUTH_TOKEN", "subscriptionType": "Claude Max", "apiProvider": "firstParty"},
        {"subscriptionType": "Claude Max", "apiProvider": "bedrock"},
        {"apiProvider": "firstParty"},
        {"subscriptionType": "Claude API", "apiProvider": "firstParty"},
        {"subscriptionType": "Claude Console", "apiProvider": "firstParty"},
        {"subscriptionType": "Claude Prototype", "apiProvider": "firstParty"},
    ],
    ids=[
        "a token from the environment",
        "another API provider",
        "no subscription",
        "an API account",
        "a plan MindRoom does not know",
        "a plan whose name only starts like a known one",
    ],
)
@pytest.mark.asyncio
async def test_an_account_that_is_not_a_plain_subscription_sign_in_is_refused(
    tmp_path: Path,
    account: dict[str, str],
) -> None:
    error = await _failure(tmp_path, {"account": account, "turns": [text_turn("never")]}, asked=False)

    assert isinstance(error, AuthenticationFailed)


@pytest.mark.parametrize("plan", ["Claude Pro", "Claude Max 20x", "Claude Team", "Claude Enterprise"])
@pytest.mark.asyncio
async def test_each_claude_subscription_plan_is_answered(tmp_path: Path, plan: str) -> None:
    account = {"subscriptionType": plan, "apiProvider": "firstParty"}
    cli = fake_cli(tmp_path, {"account": account, "turns": [text_turn("Hello.")]})

    assert (await complete(cli.provider(), ModelRequest(messages=[user("Hi")]))).content == "Hello."


@pytest.mark.parametrize(
    "variable",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_UNIX_SOCKET",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_GATEWAY",
        "ANTHROPIC_PROFILE",
        "CLAUDE_BG_AUTH_SNAPSHOT_PATH",
        "CLAUDE_AUTO_BACKGROUND_TASKS",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
        "CLAUDE_CODE_EXTRA_BODY",
        "ANTHROPIC_A_VARIABLE_OF_A_LATER_CLAUDE_CODE",
    ],
)
@pytest.mark.asyncio
async def test_an_inherited_variable_that_replaces_the_sign_in_or_where_it_is_sent_is_refused_by_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    monkeypatch.setenv(variable, "https://gateway.invented.example/invented-value")
    # The account Claude Code reports still looks like a plain subscription sign-in.
    cli = fake_cli(tmp_path, {"turns": [text_turn("sent elsewhere")]})

    with pytest.raises(AuthenticationFailed) as raised:
        await complete(cli.provider(), ModelRequest(messages=[user("Hi")]))

    assert variable in str(raised.value)
    assert "invented-value" not in str(raised.value)
    assert not raised.value.retryable
    # Claude Code never started, so it was sent nothing.
    assert cli.events("spawn") == []


@pytest.mark.asyncio
async def test_a_variable_that_is_set_for_claude_code_anyway_is_not_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The SDK sets the first for the process and MindRoom the others, whatever the environment held.
    for variable in ("CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "MCP_TOOL_TIMEOUT"):
        monkeypatch.setenv(variable, "0")
    cli = fake_cli(tmp_path, {"turns": [text_turn("Hello.")]})

    assert (await complete(cli.provider(), ModelRequest(messages=[user("Hi")]))).content == "Hello."


@pytest.mark.asyncio
async def test_a_claude_code_too_old_to_send_prompts_verbatim_is_refused(tmp_path: Path) -> None:
    error = await _failure(tmp_path, {"version": "2.1.247", "turns": [text_turn("never")]}, asked=False)

    assert isinstance(error, AuthenticationFailed)
    assert "2.1.248" in str(error)
    assert "claude update" in str(error)


@pytest.mark.asyncio
async def test_a_missing_claude_binary_says_how_to_install_it(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {})

    with pytest.raises(AuthenticationFailed, match="Claude Code was not found"):
        await complete(cli.provider(cli_path=str(tmp_path / "nowhere")), ModelRequest(messages=[user("Hi")]))


@pytest.mark.parametrize(
    ("process", "tool_choice"),
    [
        ({"mcp_status": "failed"}, None),
        ({"status": [{"name": "other", "status": "connected", "tools": []}]}, "none"),
        ({"status": None}, "none"),
    ],
    ids=["its tool server did not connect", "another tool server is connected", "it does not say which are connected"],
)
@pytest.mark.asyncio
async def test_a_process_whose_tool_servers_are_not_exactly_mindrooms_gets_no_request(
    tmp_path: Path,
    process: dict[str, Any],
    tool_choice: str | None,
) -> None:
    scripted = {**process, "turns": [text_turn("never")]}

    error = await _failure(tmp_path, scripted, asked=False, tool_choice=tool_choice)

    assert not error.retryable
    assert "MindRoom's tools" in str(error)


@pytest.mark.parametrize(
    ("init", "tool_choice"),
    [
        ({"tools": ["mcp__mindroom__lookup", "Bash"]}, None),
        ({"tools": []}, None),
        ({"tools": None}, None),
        (None, None),
        ({"mcp_servers": [{"name": "other", "status": "connected"}]}, "none"),
    ],
    ids=[
        "a tool MindRoom did not offer",
        "an offered tool is missing",
        "no list of tools",
        "no word on what it started with",
        "another tool server without any tool offered",
    ],
)
@pytest.mark.asyncio
async def test_a_turn_that_does_not_start_with_exactly_mindrooms_tools_is_stopped(
    tmp_path: Path,
    init: dict[str, Any] | None,
    tool_choice: str | None,
) -> None:
    error = await _failure(tmp_path, {"init": init, "turns": [text_turn("never")]}, tool_choice=tool_choice)

    assert not error.retryable
    assert "MindRoom's tools" in str(error)
    assert "stopped" in str(error)


@pytest.mark.asyncio
async def test_a_plan_limit_is_a_final_quota_error_with_its_reset_time(tmp_path: Path) -> None:
    rejected = {"status": "rejected", "resetsAt": 1900000000, "rateLimitType": "five_hour"}
    turn = [
        {"rate_limit": rejected},
        {"text": "You've hit your limit", "error": "rate_limit"},
        {"result": {"is_error": True, "api_error_status": 429}},
    ]

    error = await _failure(tmp_path, {"turns": [turn]})

    assert type(error) is ProviderError
    assert error.code == QUOTA_EXHAUSTED
    assert not error.retryable
    assert str(error) == "Claude subscription limit reached (five hour). Resets at 2030-03-17T17:46:40+00:00."


@pytest.mark.asyncio
async def test_a_prompt_that_is_too_long_asks_mindroom_to_compact(tmp_path: Path) -> None:
    turn = [
        {"text": "Prompt is too long", "error": "invalid_request"},
        {"result": {"is_error": True, "api_error_status": 400}},
    ]

    assert isinstance(await _failure(tmp_path, {"turns": [turn]}), ContextWindowExceeded)


@pytest.mark.asyncio
async def test_a_sign_in_that_expired_mid_conversation_says_how_to_sign_in(tmp_path: Path) -> None:
    turn = [
        {"text": "Invalid API key · Please run /login", "error": "authentication_failed"},
        {"result": {"is_error": True}},
    ]

    error = await _failure(tmp_path, {"turns": [turn]})

    assert isinstance(error, AuthenticationFailed)
    assert "claude auth login" in str(error)


@pytest.mark.asyncio
async def test_a_server_error_claude_code_already_retried_is_final(tmp_path: Path) -> None:
    turn = [
        {"text": "API Error: 529 Overloaded", "error": "server_error"},
        {"result": {"is_error": True, "api_error_status": 529}},
    ]

    error = await _failure(tmp_path, {"turns": [turn]})

    assert not error.retryable
    assert error.status_code == 529


@pytest.mark.asyncio
async def test_a_process_that_dies_mid_turn_is_a_retryable_failure(tmp_path: Path) -> None:
    error = await _failure(tmp_path, {"turns": [[{"exit": 1}]]})

    assert isinstance(error, ProviderUnavailable)


@pytest.mark.asyncio
async def test_a_process_that_cannot_take_the_request_is_a_retryable_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def gone(*_args: object, **_kwargs: object) -> None:
        message = "ProcessTransport is not ready for writing"
        raise CLIConnectionError(message)

    monkeypatch.setattr(ClaudeSDKClient, "query", gone)

    error = await _failure(tmp_path, {"turns": [text_turn("never")]}, asked=False)

    assert isinstance(error, ProviderUnavailable)
    assert "not ready for writing" in str(error)


@pytest.mark.asyncio
async def test_a_tool_call_in_a_message_that_did_not_stream_ends_the_step(tmp_path: Path) -> None:
    whole = {"tools": [call("toolu_a", "a")], "complete_only": True}
    cli = fake_cli(tmp_path, {"turns": [[whole]]}, {"turns": [text_turn("Rebuilt.")]})
    provider = cli.provider()
    messages = [user("Look up a.")]

    # No end of a model message follows, so without the call the request would wait for ever.
    asked = await asyncio.wait_for(complete(provider, ModelRequest(messages=messages, tools=[LOOKUP])), 10)

    assert [made["id"] for made in asked.message.tool_calls or ()] == ["toolu_a"]
    # That process ended its turn without the result, which a new one gets as history.
    await _resting_process_breaks()
    messages += [asked.message, result("toolu_a", "LILAC-HERON")]
    assert (await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))).content == "Rebuilt."
    assert "LILAC-HERON" in json.dumps(cli.events("user")[1]["content"])


@pytest.mark.parametrize(
    ("status", "seconds"),
    [({"status_error": "No such request"}, 30.0), ({"status_stall": True}, 0.3), ({"mcp_status": "pending"}, 0.3)],
    ids=["it rejects the question", "it never answers the question", "its tool server never connects"],
)
@pytest.mark.asyncio
async def test_a_claude_code_that_does_not_say_which_tools_it_connected_is_a_retryable_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: dict[str, Any],
    seconds: float,
) -> None:
    monkeypatch.setattr(session_module, "_READY_SECONDS", seconds)

    error = await _failure(tmp_path, {**status, "turns": [text_turn("never")]}, asked=False)

    assert isinstance(error, ProviderUnavailable)
    assert "did not start" in str(error)


@pytest.mark.asyncio
async def test_a_call_that_got_its_result_leaves_only_its_id_which_no_later_call_may_use(tmp_path: Path) -> None:
    turn = [_TWO_CALLS[0], {"tools": [call("toolu_a", "again")]}, {"text": "never"}]
    cli = fake_cli(tmp_path, {"turns": [turn]})
    provider = cli.provider()
    messages = [user("Look up a and b.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    [parked] = SESSIONS.resting.values()

    messages += [asked.message, result("toolu_a", "LILAC-HERON" * 1000), result("toolu_b", "LEMON-IBIS")]
    with pytest.raises(ProviderError, match="toolu_a"):
        await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    # Neither the arguments nor the results of the calls that are done are kept.
    assert (parked.bridge.calls, parked.bridge.unconfirmed) == ({}, set())
    assert parked.bridge.spent == {"toolu_a", "toolu_b"}
    assert len(cli.events("tool_result")) == 2


# --- a process that goes on by itself --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "how",
    ["abandon", "denied", "unused"],
    ids=["it gave the call up", "it never made the call", "it dropped the result it was handed"],
)
@pytest.mark.asyncio
async def test_an_answer_that_did_not_take_mindrooms_results_is_discarded_and_the_turn_rebuilt(
    tmp_path: Path,
    how: str,
) -> None:
    gate = tmp_path / "gate"
    turn = [
        {"tools": [{**call("toolu_a", "a"), how: True}], "abandon": how == "abandon"},
        {"text": "An answer without the result.", "after_file": str(gate)},
    ]
    cli = fake_cli(tmp_path, {"turns": [turn]}, {"turns": [text_turn("Rebuilt.")]})
    provider = cli.provider()
    messages = [user("Look up a.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    [parked] = SESSIONS.resting.values()
    modes = []

    async def on_send(body: Any) -> None:  # noqa: ANN401
        modes.append(body["mode"])
        gate.touch()
        # The first case holds the request until the process went on; the others let it go on only afterwards.
        while how == "abandon" and len(modes) == 1 and parked.bridge.messages.empty():  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    messages += [asked.message, result("toolu_a", "LILAC-HERON")]
    events = await _events(provider, ModelRequest(messages=messages, tools=[LOOKUP], on_send=on_send))
    await _closed()

    assert _turn(events).message.content == "Rebuilt."
    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "Rebuilt."
    assert modes == ["continue", "rebuild"]
    # A process that had gone on before the results were ready never got them.
    assert how != "abandon" or parked.bridge.calls["toolu_a"].result.cancelled()
    assert "LILAC-HERON" in json.dumps(cli.events("user")[1]["content"])
    assert cli.events("tool_result") == []
    assert len(cli.events("spawn")) == 2


@pytest.mark.asyncio
async def test_a_later_turn_of_a_process_must_start_with_exactly_mindrooms_tools_again(tmp_path: Path) -> None:
    process = {"turns": [text_turn("One."), text_turn("never")], "later_init": None}
    cli = fake_cli(tmp_path, process)
    provider = cli.provider(execution_identity=CONVERSATION)
    messages = [user("Hi")]
    first = await complete(provider, ModelRequest(messages=messages))

    messages += [first.message, user("More?")]
    with pytest.raises(ProviderError, match="exactly MindRoom's tools"):
        await complete(provider, ModelRequest(messages=messages))

    assert len(cli.events("spawn")) == 1


@pytest.mark.parametrize(
    "tools",
    [
        [{"id": "toolu_x", "raw_name": "Bash", "input": {"command": "id"}, "denied": True}],
        [call("toolu_a", "a"), call("toolu_a", "b")],
    ],
    ids=["a tool MindRoom did not offer", "a call id the model used before"],
)
@pytest.mark.asyncio
async def test_a_call_that_is_not_one_new_call_of_a_mindroom_tool_fails_before_any_call_is_handed_over(
    tmp_path: Path,
    tools: list[dict[str, Any]],
) -> None:
    cli = fake_cli(tmp_path, {"turns": [[{"tools": tools}, {"text": "never"}]]})
    events: list[StreamEvent] = []

    async def read() -> None:
        async for event in cli.provider().stream(ModelRequest(messages=[user("Hi")], tools=[LOOKUP])):
            events.append(event)  # noqa: PERF401

    with pytest.raises(ProviderError, match="not one new call of a MindRoom tool") as raised:
        await read()
    await _closed()

    assert not raised.value.retryable
    assert not any(isinstance(event, ToolCallReady | TurnCompleted) for event in events)
    assert len(cli.events("exit")) == 1


# --- which process a request may continue or close --------------------------------------------------------------------


def _notice(text: str) -> Message:
    return Message(role="user", content=text, provider_data={QUEUED_MESSAGE_NOTICE_MARKER_KEY: True})


@pytest.mark.asyncio
async def test_a_process_that_read_a_queued_message_notice_is_not_continued_once_the_notice_is_gone(
    tmp_path: Path,
) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]}, {"turns": [text_turn("Two.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    first = await complete(provider, ModelRequest(messages=[user("Hi"), _notice("A newer message waits.")]))

    # The stored conversation no longer holds the notice, which the first process has read.
    second = await complete(provider, ModelRequest(messages=[user("Hi"), first.message, user("More?")]))
    await _closed()

    assert second.content == "Two."
    assert len(cli.events("spawn")) == 2
    assert len(cli.events("exit")) == 1


@pytest.mark.asyncio
async def test_a_user_message_after_the_results_of_a_parked_turn_rebuilds_as_a_user_message(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [[{"tools": [call("toolu_a", "a")]}]]}, {"turns": [text_turn("Rebuilt.")]})
    provider = cli.provider()
    messages = [user("Look up a.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    messages += [asked.message, result("toolu_a", "LILAC-HERON"), user("Stop, look up c instead.")]
    answer = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    assert answer.content == "Rebuilt."
    assert cli.events("tool_result") == []
    # It follows the history as the user's own message, not inside a tool result.
    assert cli.events("user")[1]["content"][-1]["text"].endswith("</conversation-checkpoint>\nStop, look up c instead.")


@pytest.mark.asyncio
async def test_a_conversation_that_starts_over_closes_its_idle_process_at_once(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]}, {"turns": [text_turn("Two.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    await complete(provider, ModelRequest(messages=[user("Hi")]))

    await complete(provider, ModelRequest(messages=[user("Something else, after a compaction")]))
    await _closed()

    assert [session.emitted.content for session in SESSIONS.resting.values()] == ["Two."]
    assert len(cli.events("exit")) == 1


@pytest.mark.asyncio
async def test_history_copied_to_another_conversation_does_not_close_the_process_parked_on_its_calls(
    tmp_path: Path,
) -> None:
    turn = [{"tools": [call("toolu_a", "a")]}, {"text": "{toolu_a}"}]
    cli = fake_cli(tmp_path, {"turns": [turn]}, {"turns": [text_turn("Copied.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    messages = [user("Look up a.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    elsewhere = replace(CONVERSATION, thread_id="$other", resolved_thread_id="$other", session_id="session-2")

    copied = [*messages, asked.message, result("toolu_a", "FORGED"), user("And now?")]
    await complete(cli.provider(execution_identity=elsewhere), ModelRequest(messages=copied, tools=[LOOKUP]))
    await _closed()

    messages += [asked.message, result("toolu_a", "LILAC-HERON")]
    assert (await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))).content == "LILAC-HERON"


@pytest.mark.asyncio
async def test_a_parked_turn_is_not_continued_or_closed_from_another_storage(tmp_path: Path) -> None:
    turn = [{"tools": [call("toolu_a", "a")]}, {"text": "{toolu_a}"}]
    cli = fake_cli(tmp_path, {"turns": [turn]}, {"turns": [text_turn("Elsewhere.")]})
    messages = [user("Look up a.")]
    asked = await complete(cli.provider(), ModelRequest(messages=messages, tools=[LOOKUP]))
    elsewhere = replace(cli, storage_root=tmp_path / "other_data")

    messages += [asked.message, result("toolu_a", "LILAC-HERON")]
    answer = await complete(elsewhere.provider(), ModelRequest(messages=messages, tools=[LOOKUP]))
    await _closed()

    assert answer.content == "Elsewhere."
    assert cli.events("tool_result") == []
    assert [session.emitted.tool_call_ids for session in SESSIONS.resting.values()] == [("toolu_a",)]


@pytest.mark.asyncio
async def test_a_preflight_closes_only_the_process_it_started(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    await complete(provider, ModelRequest(messages=[user("Hi")]))

    await provider.preflight()

    assert len(SESSIONS.resting) == len(SESSIONS.live) == 1
    assert len(cli.events("spawn")) == 2
    assert len(cli.events("exit")) == 1


# --- usage ------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_what_a_turn_used_beyond_its_model_messages_is_added_once(tmp_path: Path) -> None:
    message = {"input_tokens": 3, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 200, "output_tokens": 9}
    # The whole turn used more input and output than its one model message, and reports fewer cache writes.
    whole = {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 200, "output_tokens": 12}
    cli = fake_cli(tmp_path, {"turns": [[{"text": "Hello.", "usage": message}, {"result": {"usage": whole}}]]})

    answer = await complete(cli.provider(), ModelRequest(messages=[user("Hi")]))

    tokens = answer.usage.tokens
    assert (tokens.input_tokens, tokens.cache_write_tokens, tokens.cache_read_tokens, tokens.output_tokens) == (
        10,
        40,
        200,
        12,
    )
    # The context the answer held is still that of its model message.
    assert answer.usage.context.input_tokens == 3


@pytest.mark.asyncio
async def test_what_a_later_turn_of_a_process_used_is_not_measured_against_its_earlier_turns(tmp_path: Path) -> None:
    whole = {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 5}
    turns = [text_turn("One."), [{"text": "Two."}, {"result": {"usage": whole}}]]
    cli = fake_cli(tmp_path, {"turns": turns})
    provider = cli.provider(execution_identity=CONVERSATION)
    messages = [user("Hi")]
    first = await complete(provider, ModelRequest(messages=messages))

    second = await complete(provider, ModelRequest(messages=[*messages, first.message, user("More?")]))

    # Each model message used 3 input tokens, and the second turn 10 in all.
    assert (first.usage.tokens.input_tokens, second.usage.tokens.input_tokens) == (3, 10)
    assert len(cli.events("spawn")) == 1


@pytest.mark.asyncio
async def test_a_plan_limit_reported_while_the_process_rested_still_gives_its_reset_time(tmp_path: Path) -> None:
    refused = [{"text": "You've hit your limit", "error": "rate_limit"}, {"result": {"is_error": True}}]
    cli = fake_cli(tmp_path, {"turns": [[{"tools": [call("toolu_a", "a")]}, *refused]]})
    provider = cli.provider()
    messages = [user("Look up a.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    [parked] = SESSIONS.resting.values()
    limit = RateLimitInfo(status="rejected", resets_at=1900000000, rate_limit_type="five_hour")
    parked.bridge.messages.put_nowait(RateLimitEvent(rate_limit_info=limit, uuid="invented", session_id="invented"))

    messages += [asked.message, result("toolu_a", "LILAC-HERON")]
    with pytest.raises(ProviderError, match=r"limit reached \(five hour\). Resets at 2030-03-17T17:46:40"):
        await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))


@pytest.mark.asyncio
async def test_two_requests_with_the_same_history_never_share_a_parked_process(tmp_path: Path) -> None:
    turn = [*_TWO_CALLS, {"text": "{toolu_a}+{toolu_b}"}]
    cli = fake_cli(tmp_path, {"turns": [turn]}, {"turns": [text_turn("Rebuilt.")]})
    provider = cli.provider()
    messages = [user("Look up a and b.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    messages += [asked.message, result("toolu_a", "LILAC-HERON"), result("toolu_b", "LEMON-IBIS")]

    first, second = await asyncio.gather(
        complete(provider, ModelRequest(messages=messages, tools=[LOOKUP])),
        complete(provider, ModelRequest(messages=messages, tools=[LOOKUP])),
    )

    assert sorted([first.content, second.content]) == ["LILAC-HERON+LEMON-IBIS", "Rebuilt."]
    assert len(cli.events("spawn")) == 2
