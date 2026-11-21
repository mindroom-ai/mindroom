"""The ``claude_code`` route against a scripted Claude Code process, through the real Claude Agent SDK."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from claude_agent_sdk import ClaudeSDKClient, CLIConnectionError, RateLimitEvent, RateLimitInfo
from structlog.testing import capture_logs

from mindroom.agent_loop.tools import media_follow_up
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.llm.errors import (
    QUOTA_EXHAUSTED,
    AuthenticationFailed,
    ContextWindowExceeded,
    ProviderError,
    ProviderUnavailable,
)
from mindroom.llm.media import Image
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
from mindroom.providers.claude_code import adapter as adapter_module
from mindroom.providers.claude_code import bridge as bridge_module
from mindroom.providers.claude_code import session as session_module
from mindroom.providers.claude_code.prompt import rebuild_input
from mindroom.providers.claude_code.session import SESSIONS
from mindroom.providers.names import ROUTE_PROVIDERS
from tests.fakes.scripted_model import tool_call
from tests.providers.bare_adapter import completed as completed_turn
from tests.providers.bare_adapter import drain, drain_into
from tests.providers.claude_code.support import (
    CONVERSATION,
    LOOKUP,
    PNG,
    call,
    fake_cli,
    left_behind,
    result,
    tagged_json,
    text_turn,
    user,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.provider import StreamEvent


pytestmark = pytest.mark.asyncio

_SYSTEM = Message(role="system", content="You are the test agent.")
_TWO_CALLS = [{"text": "Looking.", "tools": [call("toolu_a", "a"), call("toolu_b", "b")]}]
_OTHER = ToolSpec(name="other", description="Another tool.", parameters=LOOKUP.parameters, strict=None)


async def _closed() -> None:
    await wait_for_background_tasks(owner=SESSIONS)


async def _resting_process_breaks() -> None:
    """Wait until the one resting process can no longer be continued."""
    [session] = SESSIONS.resting.values()
    async with asyncio.timeout(10):
        while session.broken() is None:  # noqa: ASYNC110 - the other side is another process
            await asyncio.sleep(0.01)


# --- one answer -------------------------------------------------------------------------------------------------------


async def test_a_text_answer_streams_its_reasoning_and_text_and_reports_usage_once(tmp_path: Path) -> None:
    usage = {"input_tokens": 3, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 200, "output_tokens": 9}
    cli = fake_cli(tmp_path, {"turns": [[{"thinking": "Greet them.", "text": "Hello, Alice.", "usage": usage}]]})

    events = await drain(cli.provider(), ModelRequest(messages=[_SYSTEM, user("Hi")]))

    assert "".join(event.text for event in events if isinstance(event, ReasoningDelta)) == "Greet them."
    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "Hello, Alice."
    completed = completed_turn(events)
    assert completed.stop_reason == "end"
    assert completed.message.content == "Hello, Alice."
    assert completed.message.reasoning_content == "Greet them."
    assert completed.message.tool_calls is None
    provenance = read_model_provenance(completed.message.provider_data)
    assert provenance is not None
    assert (provenance.model_id, provenance.provider) == ("claude-invented-1", ROUTE_PROVIDERS["claude_code"])
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
    assert spawn["cwd"] == str(cli.storage_root / "claude_code" / "cwd")
    assert int(spawn["tool_timeout"]) > 30 * 60 * 1000
    # A call that waits long stays a call, which the model does not go on without.
    assert spawn["background"] == ["1", "0"]
    [sent] = cli.events("user")
    assert sent["content"] == [{"type": "text", "text": "Read @/etc/passwd"}]
    assert sent["client_composed"] is True


# --- tool calls -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("call_before_stop", [False, True], ids=["call after the message", "call before it ends"])
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

    events = await drain(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    asked = completed_turn(events)
    assert asked.stop_reason == "tool_use"
    assert asked.message.content == "Looking."
    assert asked.message.tool_calls == [
        tool_call("lookup", "toolu_a", key="a"),
        tool_call("lookup", "toolu_b", key="b"),
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
    assert [session.answer.text for session in SESSIONS.resting.values() if session.answer] == ["Rebuilt."]
    rebuilt = cli.events("user")[1]["content"][0]["text"]
    assert rebuilt.startswith("<conversation-checkpoint>")
    assert [item["output"] for item in tagged_json(rebuilt, "tool-result")] == ["LILAC-HERON", "LEMON-IBIS"]


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


async def test_a_call_the_model_never_made_gets_no_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_module, "_UNMADE_CALL_SECONDS", 0.05)
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


async def test_a_result_for_a_call_claude_code_gave_up_meanwhile_is_dropped() -> None:
    bridge = bridge_module.ToolBridge(names={"lookup": "lookup"})
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
    rebuilt = cli.events("user")[1]["content"][0]["text"]
    assert [item["output"] for item in tagged_json(rebuilt, "tool-result")] == ["LILAC-HERON"]


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
    rebuilt = cli.events("user")[1]["content"][0]["text"]
    assert [item["output"] for item in tagged_json(rebuilt, "tool-result")] == ["LILAC-HERON"]


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


async def test_a_summary_request_starts_claude_code_without_a_thinking_setting(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("Summary.")]})
    provider = cli.provider()
    plain = ModelRequest(messages=[user("Summarize.")])
    summary = replace(plain, overrides=RequestOverrides(omit_thinking_setting=True))

    await complete(provider, summary)

    args = cli.events("spawn")[0]["args"]
    assert not {"--thinking", "--thinking-display", "--max-thinking-tokens"} & set(args)
    assert provider.context_key(plain) != provider.context_key(summary)


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


async def test_text_that_arrives_with_the_start_of_its_block_is_kept(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [[{"text": "Hello, Alice.", "start_text": True}]]})

    events = await drain(cli.provider(), ModelRequest(messages=[user("Hi")]))

    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "Hello, Alice."
    assert completed_turn(events).message.content == "Hello, Alice."


async def test_the_texts_of_two_model_messages_in_one_answer_are_kept_apart(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [[{"text": "Let me check."}, {"text": "Done."}]]})

    events = await drain(cli.provider(), ModelRequest(messages=[user("Hi")]))

    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "Let me check.\n\nDone."
    assert completed_turn(events).message.content == "Let me check.\n\nDone."
    assert completed_turn(events).message.metrics.tokens.output_tokens == 10


# --- the next turn ----------------------------------------------------------------------------------------------------


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


async def test_a_changed_system_prompt_rebuilds_instead_of_continuing(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]}, {"turns": [text_turn("Two.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    first = await complete(provider, ModelRequest(messages=[_SYSTEM, user("What is a?")]))

    changed = [Message(role="system", content="New rules."), user("What is a?"), first.message, user("And b?")]
    second = await complete(provider, ModelRequest(messages=changed))

    assert second.content == "Two."
    assert [spawn["system_prompt"] for spawn in cli.events("spawn")] == ["You are the test agent.", "New rules."]


async def test_an_answer_outside_a_conversation_leaves_no_process_behind(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]})

    await complete(cli.provider(), ModelRequest(messages=[user("Hi")]))
    await _closed()

    assert SESSIONS.resting == {}
    assert len(cli.events("exit")) == 1


@pytest.mark.parametrize("same_storage", [True, False], ids=["in the same storage", "in another storage"])
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


async def test_a_process_past_its_time_to_live_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = fake_cli(tmp_path, {"turns": [_TWO_CALLS]}, {"turns": [text_turn("Idle.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    await complete(provider, ModelRequest(messages=[user("Look up a and b.")], tools=[LOOKUP]))
    await complete(provider, ModelRequest(messages=[user("Hi")]))
    assert len(SESSIONS.resting) == 2

    monkeypatch.setattr(session_module, "_IDLE_TTL_SECONDS", 0.0)
    await SESSIONS.close_expired()
    assert [key[2] for key in SESSIONS.resting] == [("toolu_a", "toolu_b")]

    monkeypatch.setattr(session_module, "_PARKED_TTL_SECONDS", 0.0)
    await SESSIONS.close_expired()
    assert SESSIONS.resting == {}
    assert len(cli.events("exit")) == 2


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

    resting = sorted(session.answer.text or "" for session in SESSIONS.resting.values() if session.answer)
    assert resting == ["Looking.", "New."]
    assert (len(SESSIONS.live), SESSIONS.starting) == (2, 0)


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


async def test_a_system_prompt_is_private_to_the_user_and_gone_once_claude_code_started(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("Hello.")]})
    home_path = cli.storage_root / "claude_code"
    cwd = home_path / "cwd"
    cwd.mkdir(parents=True)
    for path in (home_path, cwd):
        path.chmod(0o755)
    umask = os.umask(0o022)
    try:
        await complete(cli.provider(execution_identity=CONVERSATION), ModelRequest(messages=[_SYSTEM, user("Hi")]))
    finally:
        os.umask(umask)

    [spawn] = cli.events("spawn")
    assert spawn["system_prompt"] == "You are the test agent."
    assert spawn["prompt_modes"] == [0o600, 0o700]
    assert spawn["cwd"] == str(cwd)
    assert stat.S_IMODE(cwd.stat().st_mode) == 0o700
    assert set(home_path.iterdir()) == {cwd, home_path / "exec-claude"}
    assert not list(cwd.iterdir())
    # The process still rests, and its prompt is no longer on disk.
    assert len(SESSIONS.resting) == 1
    assert left_behind(cli) == []


@pytest.mark.parametrize("part", ["claude_code", "cwd"])
async def test_claude_code_refuses_linked_runtime_directories(tmp_path: Path, part: str) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("never")]})
    home_path = cli.storage_root / "claude_code"
    link = home_path if part == "claude_code" else home_path / "cwd"
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    link.parent.mkdir(parents=True)
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ProviderError, match="symlink") as caught:
        await complete(cli.provider(), ModelRequest(messages=[_SYSTEM, user("Hi")]))

    assert not caught.value.retryable
    assert str(link) in str(caught.value)
    assert cli.events("spawn") == []
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not list(target.iterdir())


async def test_claude_code_refuses_a_nonempty_working_directory(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {})
    cwd = cli.storage_root / "claude_code" / "cwd"
    cwd.mkdir(parents=True)
    (cwd / "existing").touch()

    with pytest.raises(ProviderError, match="not empty") as caught:
        await complete(cli.provider(), ModelRequest(messages=[_SYSTEM, user("Hi")]))

    assert not caught.value.retryable
    assert str(cwd) in str(caught.value)
    assert cli.events("spawn") == []


async def test_a_failed_prompt_write_leaves_no_file(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {})
    # A lone surrogate cannot be written to the UTF-8 prompt file.
    system = Message(role="system", content="cannot encode \ud800")

    with pytest.raises(ProviderUnavailable) as caught:
        await complete(cli.provider(), ModelRequest(messages=[system, user("Hi")]))

    assert isinstance(caught.value.__cause__, UnicodeEncodeError)
    assert cli.events("spawn") == []
    assert left_behind(cli) == []


@pytest.fixture
def held_start(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold each start where it opens Claude Code: the first event says one got there, the second lets it go on."""
    reached, released = asyncio.Event(), asyncio.Event()
    open_process = session_module.ClaudeProcess.open

    async def held(*args: Any, **kwargs: Any) -> session_module.ClaudeProcess:  # noqa: ANN401
        reached.set()
        await released.wait()
        return await open_process(*args, **kwargs)

    monkeypatch.setattr(session_module.ClaudeProcess, "open", staticmethod(held))
    return reached, released


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
    assert left_behind(cli) == []


async def test_a_slot_is_not_free_before_the_process_that_held_it_is_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "_MAX_PROCESSES", 1)
    gone = asyncio.Event()
    close = session_module.ClaudeProcess.close

    async def slow_close(process: session_module.ClaudeProcess) -> None:
        await asyncio.wait_for(gone.wait(), 5)
        await close(process)

    monkeypatch.setattr(session_module.ClaudeProcess, "close", slow_close)
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
    assert left_behind(cli) == []
    return raised.value


async def test_a_signed_out_claude_code_says_how_to_sign_in_and_sends_nothing(tmp_path: Path) -> None:
    error = await _failure(tmp_path, {"account": {"tokenSource": "none", "apiProvider": "firstParty"}}, asked=False)

    assert isinstance(error, AuthenticationFailed)
    assert str(error) == ("Claude Code is not signed in. Run `claude auth login` as the user that runs MindRoom.")


async def test_an_api_key_reported_by_claude_code_is_refused_by_name(tmp_path: Path) -> None:
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
        {"subscriptionType": "Claude Pro Preview", "apiProvider": "firstParty"},
    ],
    ids=[
        "a token from the environment",
        "another API provider",
        "no subscription",
        "an API account",
        "a plan MindRoom does not know",
        "a plan whose name only starts like a known one",
        "a known plan with something after its name",
    ],
)
async def test_an_account_that_is_not_a_plain_subscription_sign_in_is_refused(
    tmp_path: Path,
    account: dict[str, str],
) -> None:
    error = await _failure(tmp_path, {"account": account, "turns": [text_turn("never")]}, asked=False)

    assert isinstance(error, AuthenticationFailed)


@pytest.mark.parametrize("plan", ["Claude Pro", "Claude Max", "Claude Max 20x", "Claude Team", "Claude Enterprise"])
async def test_each_claude_subscription_plan_is_answered(tmp_path: Path, plan: str) -> None:
    account = {"subscriptionType": plan, "apiProvider": "firstParty"}
    cli = fake_cli(tmp_path, {"account": account, "turns": [text_turn("Hello.")]})

    assert (await complete(cli.provider(), ModelRequest(messages=[user("Hi")]))).content == "Hello."


async def test_inherited_claude_settings_are_removed_only_from_the_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inherited = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_BG_AUTH_SNAPSHOT_PATH",
        "CLAUDE_AUTO_BACKGROUND_TASKS",
    }
    kept = {"CLAUDE_CODE_ENTRYPOINT", *session_module._ISOLATION_ENV}
    for name in inherited | kept | {"CLAUDE_CONFIG_DIR"}:
        monkeypatch.setenv(name, "inherited")
    cli = fake_cli(tmp_path, {"turns": [text_turn("Hello.")]})

    assert (await complete(cli.provider(), ModelRequest(messages=[user("Hi")]))).content == "Hello."

    [spawn] = cli.events("spawn")
    child_names = set(spawn["env_names"])
    assert child_names.isdisjoint(inherited)
    assert child_names.issuperset(kept | {"CLAUDE_CONFIG_DIR"})
    assert not any(name.startswith("MINDROOM_CLAUDE_CODE_") for name in child_names)
    assert all(os.environ[name] == "inherited" for name in inherited | kept)


async def test_two_starts_reuse_the_same_private_launcher(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("Hello.")]})
    launcher = cli.storage_root / "claude_code" / "exec-claude"
    request = ModelRequest(messages=[user("Hi")])

    await complete(cli.provider(), request)
    first = launcher.stat()
    await complete(cli.provider(), request)
    second = launcher.stat()

    assert len(cli.events("spawn")) == 2
    assert stat.S_IMODE(second.st_mode) == 0o700
    assert (first.st_ino, first.st_mtime_ns) == (second.st_ino, second.st_mtime_ns)


async def test_a_non_executable_launcher_explains_why_claude_code_cannot_start(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("Hello.")]})
    request = ModelRequest(messages=[user("Hi")])
    await complete(cli.provider(), request)
    (cli.storage_root / "claude_code" / "exec-claude").chmod(0o600)

    with pytest.raises(ProviderUnavailable, match="launcher is not executable"):
        await complete(cli.provider(), request)

    assert len(cli.events("spawn")) == 1


async def test_a_claude_code_older_than_the_one_this_route_was_run_against_gets_no_prompt(tmp_path: Path) -> None:
    # 2.1.294 still let the model reach tools beyond the ones it was started with.
    error = await _failure(tmp_path, {"version": "2.1.294", "turns": [text_turn("never")]}, asked=False)

    assert isinstance(error, AuthenticationFailed)
    assert "2.1.328" in str(error)
    assert "claude update" in str(error)


@pytest.mark.parametrize("exists", [False, True], ids=["missing", "not executable"])
async def test_an_unavailable_configured_binary_says_how_to_install_claude(tmp_path: Path, exists: bool) -> None:
    cli = fake_cli(tmp_path, {})
    path = tmp_path / "unavailable"
    if exists:
        path.touch(mode=0o600)

    with pytest.raises(AuthenticationFailed, match="Claude Code was not found") as raised:
        await complete(cli.provider(cli_path=str(path)), ModelRequest(messages=[user("Hi")]))

    assert str(raised.value) == session_module._NOT_FOUND
    assert cli.events("spawn") == []


@pytest.mark.parametrize(
    ("process", "tool_choice"),
    [
        ({"mcp_status": "failed"}, None),
        ({"status": [{"name": "other", "status": "connected", "tools": []}]}, "none"),
        ({"status": None}, "none"),
    ],
    ids=["its tool server did not connect", "another tool server is connected", "it does not say which are connected"],
)
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
async def test_a_turn_that_does_not_start_with_exactly_mindrooms_tools_is_stopped(
    tmp_path: Path,
    init: dict[str, Any] | None,
    tool_choice: str | None,
) -> None:
    error = await _failure(tmp_path, {"init": init, "turns": [text_turn("never")]}, tool_choice=tool_choice)

    assert not error.retryable
    assert "MindRoom's tools" in str(error)
    assert "stopped" in str(error)


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


@pytest.mark.parametrize(
    ("steps", "error_type", "retryable", "status"),
    [
        pytest.param(
            [
                {"text": "Prompt is too long", "error": "invalid_request"},
                {"result": {"is_error": True, "api_error_status": 400}},
            ],
            ContextWindowExceeded,
            False,
            400,
            id="prompt too long",
        ),
        pytest.param(
            [
                {"text": "Invalid API key · Please run /login", "error": "authentication_failed"},
                {"result": {"is_error": True}},
            ],
            AuthenticationFailed,
            False,
            401,
            id="expired sign-in",
        ),
        pytest.param(
            [
                {"text": "API Error: 529 Overloaded", "error": "server_error"},
                {"result": {"is_error": True, "api_error_status": 529}},
            ],
            ProviderError,
            False,
            529,
            id="server error already retried by Claude Code",
        ),
        pytest.param([{"exit": 1}], ProviderUnavailable, True, None, id="process died"),
    ],
)
async def test_claude_code_turn_failures_keep_their_error_shape(
    tmp_path: Path,
    steps: list[dict[str, Any]],
    error_type: type[ProviderError],
    retryable: bool,
    status: int | None,
) -> None:
    error = await _failure(tmp_path, {"turns": [steps]})

    assert type(error) is error_type
    assert (error.retryable, error.status_code) == (retryable, status)
    if error_type is AuthenticationFailed:
        assert "claude auth login" in str(error)


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


@pytest.mark.parametrize(
    ("status", "seconds"),
    [({"status_error": "No such request"}, 30.0), ({"status_stall": True}, 0.3), ({"mcp_status": "pending"}, 0.3)],
    ids=["it rejects the question", "it never answers the question", "its tool server never connects"],
)
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


async def test_a_process_that_starts_over_holds_the_slot_of_the_one_it_replaces_all_along(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "_MAX_PROCESSES", 1)
    turn = [{"tools": [{**call("toolu_a", "a"), "unused": True}]}, {"text": "An answer without the result."}]
    cli = fake_cli(tmp_path, {"turns": [turn]}, {"turns": [text_turn("Rebuilt.")]})
    provider = cli.provider()
    messages = [user("Look up a.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    rendering, rendered = threading.Event(), threading.Event()

    def held(*args: Any) -> Any:  # noqa: ANN401
        if not rendering.is_set():
            rendering.set()
            rendered.wait(10)
        return rebuild_input(*args)

    monkeypatch.setattr(adapter_module, "rebuild_input", held)
    messages += [asked.message, result("toolu_a", "LILAC-HERON")]
    request = asyncio.create_task(complete(provider, ModelRequest(messages=messages, tools=[LOOKUP])))
    # The process went on without the result, and the request that starts over is held where it renders its history.
    await asyncio.to_thread(rendering.wait, 10)
    await _closed()

    other = cli.provider(execution_identity=CONVERSATION)
    with pytest.raises(ProviderUnavailable, match="limit of 1"):
        await complete(other, ModelRequest(messages=[user("Another conversation")], tool_choice="none"))
    rendered.set()

    assert (await request).content == "Rebuilt."
    started = [json.loads(line)["event"] for line in cli.log.read_text().splitlines()]
    assert [event for event in started if event in {"spawn", "exit"}] == ["spawn", "exit", "spawn"]


async def test_a_process_that_is_gone_gives_its_slot_to_no_other(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "_MAX_PROCESSES", 1)
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    await complete(provider, ModelRequest(messages=[user("Hi")]))
    [gone] = SESSIONS.resting.values()
    SESSIONS.resting.clear()
    await SESSIONS.retire(gone)
    await complete(provider, ModelRequest(messages=[user("Hi again")]))

    # The one slot is held by another process now, which the new one must take as any start would.
    new = await SESSIONS.start(
        provider="invented",
        storage_root=cli.storage_root,
        replaces=gone,
        model="invented",
        cli_path=str(cli.path),
    )
    await _closed()

    assert SESSIONS.live == {new}
    assert SESSIONS.resting == {}


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
async def test_an_answer_that_did_not_take_mindrooms_results_is_discarded_and_the_turn_rebuilt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    # The process that starts over takes the slot of the one it replaces, so one slot is enough.
    monkeypatch.setattr(session_module, "_MAX_PROCESSES", 1)
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
        while how == "abandon" and len(modes) == 1 and parked.process.messages.empty():  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    messages += [asked.message, result("toolu_a", "LILAC-HERON")]
    events = await drain(provider, ModelRequest(messages=messages, tools=[LOOKUP], on_send=on_send))
    await _closed()

    assert completed_turn(events).message.content == "Rebuilt."
    assert "".join(event.text for event in events if isinstance(event, TextDelta)) == "Rebuilt."
    assert modes == ["continue", "rebuild"]
    # A process that had gone on before the results were ready never got them.
    assert how != "abandon" or parked.bridge.calls["toolu_a"].result.cancelled()
    assert "LILAC-HERON" in json.dumps(cli.events("user")[1]["content"])
    assert cli.events("tool_result") == []
    assert len(cli.events("spawn")) == 2


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
    ("steps", "error_type", "said"),
    [
        pytest.param(
            [{"tools": [call("toolu_a", "a"), call("toolu_b", "b")], "complete_only": True}],
            ProviderUnavailable,
            "did not stream",
            id="calls arrive without a final streamed boundary",
        ),
        pytest.param(
            [{"tools": [call("toolu_a", "a")], "stop_reason": None}, {"text": "never"}],
            ProviderUnavailable,
            "gave up the model message",
            id="a retracted message",
        ),
        pytest.param(
            [
                {"tools": [{"id": "toolu_x", "raw_name": "Bash", "input": {"command": "id"}, "denied": True}]},
                {"text": "never"},
            ],
            ProviderUnavailable,
            "Bash",
            id="an unoffered tool",
        ),
        pytest.param(
            [{"tools": [call("toolu_a", "a"), call("toolu_a", "b")]}, {"text": "never"}],
            ProviderError,
            "toolu_a twice",
            id="a reused call id",
        ),
    ],
)
async def test_an_unusable_call_fails_before_any_call_is_handed_over(
    tmp_path: Path,
    steps: list[dict[str, Any]],
    error_type: type[ProviderError],
    said: str,
) -> None:
    cli = fake_cli(tmp_path, {"turns": [steps]})
    events: list[StreamEvent] = []
    request = ModelRequest(messages=[user("Look up the markers.")], tools=[LOOKUP])

    with pytest.raises(error_type, match=said) as raised:
        await asyncio.wait_for(drain_into(events, cli.provider(), request), 10)
    await _closed()

    assert raised.value.retryable == (error_type is ProviderUnavailable)
    assert not any(isinstance(event, ToolCallReady | TurnCompleted) for event in events)
    assert len(cli.events("exit")) == 1


# --- which process a request may continue or close --------------------------------------------------------------------


def _notice(text: str) -> Message:
    return Message(role="user", content=text, provider_data={QUEUED_MESSAGE_NOTICE_MARKER_KEY: True})


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


async def test_the_images_a_tool_made_reach_the_parked_process_with_its_result(tmp_path: Path) -> None:
    turn = [{"tools": [call("toolu_a", "a")]}, {"text": "I see it."}]
    cli = fake_cli(tmp_path, {"turns": [turn]})
    provider = cli.provider()
    messages = [user("Take a screenshot.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    made = replace(result("toolu_a", "Captured."), images=[Image(content=PNG, format="png")])
    # The agent loop moves a tool's media off its result, onto a user message of its own after the results.
    follow_up = media_follow_up([made])
    assert follow_up is not None

    messages += [asked.message, made, follow_up]
    answer = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    assert answer.content == "I see it."
    assert len(cli.events("spawn")) == 1
    [delivered] = cli.events("tool_result")
    assert [block["type"] for block in delivered["result"]["content"]] == ["text", "image"]
    assert delivered["result"]["content"][1]["data"] == base64.b64encode(PNG).decode()


async def test_the_pooled_media_of_a_batch_of_several_calls_reach_a_new_process_as_their_own_turn(
    tmp_path: Path,
) -> None:
    cli = fake_cli(tmp_path, {"turns": [_TWO_CALLS]}, {"turns": [text_turn("Rebuilt.")]})
    provider = cli.provider()
    messages = [user("Take a screenshot and look up b.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    shot = replace(result("toolu_a", "Captured."), images=[Image(content=PNG, format="png")])
    failed = replace(result("toolu_b", "No such key."), tool_call_error=True)
    # Nothing says which call of the batch made the image, so no call's result may carry it.
    follow_up = media_follow_up([shot, failed])
    assert follow_up is not None

    messages += [asked.message, shot, failed, follow_up]
    answer = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))

    assert answer.content == "Rebuilt."
    assert cli.events("tool_result") == []
    *_history, said, image = cli.events("user")[1]["content"]
    assert said["text"].endswith("</conversation-checkpoint>\nThe tool call above generated the attached media.")
    assert image["type"] == "image"


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


async def test_a_conversation_that_starts_over_gives_the_slot_of_its_idle_process_to_the_new_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "_MAX_PROCESSES", 1)
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]}, {"turns": [text_turn("Two.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    await complete(provider, ModelRequest(messages=[user("Hi")]))

    await complete(provider, ModelRequest(messages=[user("Something else, after a compaction")]))
    await _closed()

    assert [session.answer.text for session in SESSIONS.resting.values() if session.answer] == ["Two."]
    assert len(cli.events("exit")) == 1


async def test_a_request_that_may_call_no_tool_leaves_the_idle_process_of_its_conversation_alone(
    tmp_path: Path,
) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("One."), text_turn("Two.")]}, {"turns": [text_turn("Respond.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    messages = [user("Hi")]
    first = await complete(provider, ModelRequest(messages=messages))

    # A decision about the conversation is asked of the same model, with other instructions and no tools.
    judged = [Message(role="system", content="Should the agent reply?"), *messages, first.message, user("More?")]
    decision = await complete(provider, ModelRequest(messages=judged, tool_choice="none"))
    second = await complete(provider, ModelRequest(messages=[*messages, first.message, user("More?")]))

    assert (decision.content, second.content) == ("Respond.", "Two.")
    assert len(cli.events("spawn")) == 2
    assert cli.events("user")[2]["content"] == [{"type": "text", "text": "More?"}]


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
    assert [key[2] for key in SESSIONS.resting] == [("toolu_a",)]


async def test_a_preflight_closes_only_the_process_it_started(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]})
    provider = cli.provider(execution_identity=CONVERSATION)
    await complete(provider, ModelRequest(messages=[user("Hi")]))

    await provider.preflight()

    assert len(SESSIONS.resting) == len(SESSIONS.live) == 1
    assert len(cli.events("spawn")) == 2
    assert len(cli.events("exit")) == 1


# --- usage ------------------------------------------------------------------------------------------------------------


def _ledger(**counters: int) -> dict[str, int]:
    """Return one model's entry in the ledger a result carries."""
    return {"inputTokens": 0, "cacheCreationInputTokens": 0, "cacheReadInputTokens": 0, "outputTokens": 0, **counters}


async def test_what_a_turn_used_beyond_its_model_messages_is_added_once(tmp_path: Path) -> None:
    message = {"input_tokens": 3, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 200, "output_tokens": 9}
    # Claude Code also asked another model something for itself, which no model message of the turn shows.
    ledger = {
        "claude-invented-1": _ledger(
            inputTokens=3,
            cacheCreationInputTokens=40,
            cacheReadInputTokens=200,
            outputTokens=9,
        ),
        "claude-invented-small": _ledger(inputTokens=7, outputTokens=3),
    }
    cli = fake_cli(tmp_path, {"turns": [[{"text": "Hello.", "usage": message}, {"result": {"modelUsage": ledger}}]]})

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


async def test_what_a_later_turn_used_is_what_the_ledger_of_its_process_grew_by(tmp_path: Path) -> None:
    # The ledger counts both turns: 3 input tokens each for the model messages, and 7 more in the second turn.
    ledger = {"claude-invented-1": _ledger(inputTokens=6, outputTokens=10), "small": _ledger(inputTokens=7)}
    turns = [text_turn("One."), [{"text": "Two."}, {"result": {"modelUsage": ledger}}]]
    cli = fake_cli(tmp_path, {"turns": turns})
    provider = cli.provider(execution_identity=CONVERSATION)
    messages = [user("Hi")]
    first = await complete(provider, ModelRequest(messages=messages))

    second = await complete(provider, ModelRequest(messages=[*messages, first.message, user("More?")]))

    assert (first.usage.tokens.input_tokens, second.usage.tokens.input_tokens) == (3, 10)
    assert (first.usage.tokens.output_tokens, second.usage.tokens.output_tokens) == (5, 5)
    assert len(cli.events("spawn")) == 1


async def test_a_plan_limit_reported_while_the_process_rested_still_gives_its_reset_time(tmp_path: Path) -> None:
    refused = [{"text": "You've hit your limit", "error": "rate_limit"}, {"result": {"is_error": True}}]
    cli = fake_cli(tmp_path, {"turns": [[{"tools": [call("toolu_a", "a")]}, *refused]]})
    provider = cli.provider()
    messages = [user("Look up a.")]
    asked = await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))
    [parked] = SESSIONS.resting.values()
    limit = RateLimitInfo(status="rejected", resets_at=1900000000, rate_limit_type="five_hour")
    parked.process.messages.put_nowait(RateLimitEvent(rate_limit_info=limit, uuid="invented", session_id="invented"))

    messages += [asked.message, result("toolu_a", "LILAC-HERON")]
    with pytest.raises(ProviderError, match=r"limit reached \(five hour\). Resets at 2030-03-17T17:46:40"):
        await complete(provider, ModelRequest(messages=messages, tools=[LOOKUP]))


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
