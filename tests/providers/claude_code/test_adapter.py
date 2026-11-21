"""The ``claude_code`` route against a scripted Claude Code process, through the real Claude Agent SDK."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.llm.errors import (
    QUOTA_EXHAUSTED,
    AuthenticationFailed,
    ContextWindowExceeded,
    ProviderError,
    ProviderUnavailable,
)
from mindroom.llm.messages import Message, read_model_provenance
from mindroom.llm.provider import (
    ModelRequest,
    ReasoningDelta,
    TextDelta,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
    complete,
)
from mindroom.providers.claude_code import session as session_module
from mindroom.providers.claude_code.session import SESSIONS
from tests.providers.claude_code.support import LOOKUP, call, fake_cli, result, text_turn, user

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider, StreamEvent

pytestmark = pytest.mark.asyncio

_SYSTEM = Message(role="system", content="You are the test agent.")
_TWO_CALLS = [{"text": "Looking.", "tools": [call("toolu_a", "a"), call("toolu_b", "b")]}]


@pytest_asyncio.fixture(autouse=True)
async def _no_processes_left() -> AsyncIterator[None]:
    yield
    await SESSIONS.close_all()


async def _events(provider: ModelProvider, request: ModelRequest) -> list[StreamEvent]:
    return [event async for event in provider.stream(request)]


def _turn(events: list[StreamEvent]) -> TurnCompleted:
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    return completed


async def _closed() -> None:
    await wait_for_background_tasks(owner=SESSIONS)


# --- one answer -------------------------------------------------------------------------------------------------------


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


async def test_claude_code_starts_isolated_with_mindroom_system_prompt_and_the_user_text_verbatim(
    tmp_path: Path,
) -> None:
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
    assert spawn["cwd"].startswith(str(cli.storage_root / "claude_code"))
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
    provider = cli.provider()
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
    rebuilt = cli.events("user")[1]["content"][0]["text"]
    assert rebuilt.startswith("<conversation-checkpoint>")
    assert '"output": "LILAC-HERON"' in rebuilt
    assert '"output": "LEMON-IBIS"' in rebuilt


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
    assert len(cli.events("exit")) == 1


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
    assert '"output": "LILAC-HERON"' in cli.events("user")[1]["content"][0]["text"]


async def test_a_request_without_tool_choice_offers_claude_code_no_tools(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("No tools.")]})

    await complete(cli.provider(), ModelRequest(messages=[user("Hi")], tools=[LOOKUP], tool_choice="none"))

    args = cli.events("spawn")[0]["args"]
    assert "--mcp-config" not in args
    assert "--allowedTools" not in args


# --- the next turn ----------------------------------------------------------------------------------------------------


async def test_an_idle_process_answers_the_next_user_message_of_its_conversation(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("It is LILAC-HERON."), text_turn("It is LEMON-IBIS.")]})
    provider = cli.provider()
    messages = [_SYSTEM, user("What is a?")]
    first = await complete(provider, ModelRequest(messages=messages))

    messages += [first.message, user("And b?")]
    second = await complete(provider, ModelRequest(messages=messages))

    assert second.content == "It is LEMON-IBIS."
    assert len(cli.events("spawn")) == 1
    assert cli.events("user")[1]["content"] == [{"type": "text", "text": "And b?"}]


async def test_a_changed_system_prompt_rebuilds_instead_of_continuing(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {"turns": [text_turn("One.")]}, {"turns": [text_turn("Two.")]})
    provider = cli.provider()
    first = await complete(provider, ModelRequest(messages=[_SYSTEM, user("What is a?")]))

    changed = [Message(role="system", content="New rules."), user("What is a?"), first.message, user("And b?")]
    second = await complete(provider, ModelRequest(messages=changed))

    assert second.content == "Two."
    assert [spawn["system_prompt"] for spawn in cli.events("spawn")] == ["You are the test agent.", "New rules."]


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
    provider = cli.provider()
    await complete(provider, ModelRequest(messages=[user("Look up a and b.")], tools=[LOOKUP]))
    await complete(provider, ModelRequest(messages=[user("Hi")]))
    assert len(SESSIONS.resting) == 2

    monkeypatch.setattr(session_module, "IDLE_TTL_SECONDS", 0.0)
    await SESSIONS.close_expired()
    assert [session.emitted.tool_call_ids for session in SESSIONS.resting.values()] == [("toolu_a", "toolu_b")]

    monkeypatch.setattr(session_module, "PARKED_TTL_SECONDS", 0.0)
    await SESSIONS.close_expired()
    assert SESSIONS.resting == {}
    assert len(cli.events("exit")) == 2


async def test_a_new_process_past_the_limit_evicts_an_idle_one_before_a_parked_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "MAX_PROCESSES", 2)
    cli = fake_cli(tmp_path, {"turns": [_TWO_CALLS]}, {"turns": [text_turn("Idle.")]}, {"turns": [text_turn("New.")]})
    provider = cli.provider()
    await complete(provider, ModelRequest(messages=[user("Look up a and b.")], tools=[LOOKUP]))
    await complete(provider, ModelRequest(messages=[user("Hi")]))

    await complete(provider, ModelRequest(messages=[user("Another conversation")]))
    await _closed()

    resting = sorted(session.emitted.content or "" for session in SESSIONS.resting.values())
    assert resting == ["Looking.", "New."]


# --- failures ---------------------------------------------------------------------------------------------------------


async def _failure(tmp_path: Path, process: dict[str, Any], **extra_kwargs: object) -> ProviderError:
    cli = fake_cli(tmp_path, process)
    with pytest.raises(ProviderError) as raised:
        await complete(cli.provider(**extra_kwargs), ModelRequest(messages=[user("Hi")], tools=[LOOKUP]))
    await _closed()
    assert SESSIONS.resting == {}
    assert len(cli.events("exit")) == len(cli.events("spawn"))
    return raised.value


async def test_a_signed_out_claude_code_says_how_to_sign_in_and_sends_nothing(tmp_path: Path) -> None:
    error = await _failure(tmp_path, {"account": {"tokenSource": "none", "apiProvider": "firstParty"}})

    assert isinstance(error, AuthenticationFailed)
    assert str(error) == ("Claude Code is not signed in. Run `claude auth login` as the user that runs MindRoom.")


async def test_an_api_key_from_the_environment_is_refused_by_name(tmp_path: Path) -> None:
    account = {"tokenSource": "claude.ai", "apiKeySource": "ANTHROPIC_API_KEY", "apiProvider": "firstParty"}

    error = await _failure(tmp_path, {"account": account, "turns": [text_turn("billed to the key")]})

    assert isinstance(error, AuthenticationFailed)
    assert "ANTHROPIC_API_KEY" in str(error)
    assert "provider: anthropic" in str(error)


@pytest.mark.parametrize(
    "account",
    [
        {"tokenSource": "CLAUDE_CODE_OAUTH_TOKEN", "subscriptionType": "Claude Max", "apiProvider": "firstParty"},
        {"subscriptionType": "Claude Max", "apiProvider": "bedrock"},
        {"apiProvider": "firstParty"},
    ],
    ids=["a token from the environment", "another API provider", "no subscription"],
)
async def test_an_account_that_is_not_a_plain_subscription_sign_in_is_refused(
    tmp_path: Path,
    account: dict[str, str],
) -> None:
    error = await _failure(tmp_path, {"account": account, "turns": [text_turn("never")]})

    assert isinstance(error, AuthenticationFailed)


async def test_a_claude_code_too_old_to_send_prompts_verbatim_is_refused(tmp_path: Path) -> None:
    error = await _failure(tmp_path, {"version": "2.1.247", "turns": [text_turn("never")]})

    assert isinstance(error, AuthenticationFailed)
    assert "2.1.248" in str(error)
    assert "claude update" in str(error)


async def test_a_missing_claude_binary_says_how_to_install_it(tmp_path: Path) -> None:
    cli = fake_cli(tmp_path, {})

    with pytest.raises(AuthenticationFailed, match="Claude Code was not found"):
        await complete(cli.provider(cli_path=str(tmp_path / "nowhere")), ModelRequest(messages=[user("Hi")]))


@pytest.mark.parametrize(
    "process",
    [{"extra_tools": ["Bash"]}, {"mcp_status": "failed"}],
    ids=["a tool MindRoom did not offer", "the tool server is not connected"],
)
async def test_a_process_that_is_not_isolated_to_mindrooms_tools_fails_closed(
    tmp_path: Path,
    process: dict[str, Any],
) -> None:
    error = await _failure(tmp_path, {**process, "turns": [text_turn("never")]})

    assert not error.retryable
    assert "MindRoom's tools" in str(error)


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


async def test_a_prompt_that_is_too_long_asks_mindroom_to_compact(tmp_path: Path) -> None:
    turn = [
        {"text": "Prompt is too long", "error": "invalid_request"},
        {"result": {"is_error": True, "api_error_status": 400}},
    ]

    assert isinstance(await _failure(tmp_path, {"turns": [turn]}), ContextWindowExceeded)


async def test_a_sign_in_that_expired_mid_conversation_says_how_to_sign_in(tmp_path: Path) -> None:
    turn = [
        {"text": "Invalid API key · Please run /login", "error": "authentication_failed"},
        {"result": {"is_error": True}},
    ]

    error = await _failure(tmp_path, {"turns": [turn]})

    assert isinstance(error, AuthenticationFailed)
    assert "claude auth login" in str(error)


async def test_a_server_error_claude_code_already_retried_is_final(tmp_path: Path) -> None:
    turn = [
        {"text": "API Error: 529 Overloaded", "error": "server_error"},
        {"result": {"is_error": True, "api_error_status": 529}},
    ]

    error = await _failure(tmp_path, {"turns": [turn]})

    assert not error.retryable
    assert error.status_code == 529


async def test_a_process_that_dies_mid_turn_is_a_retryable_failure(tmp_path: Path) -> None:
    error = await _failure(tmp_path, {"turns": [[{"exit": 1}]]})

    assert isinstance(error, ProviderUnavailable)


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
