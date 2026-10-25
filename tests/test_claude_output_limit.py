"""A Claude response cut off at its output limit never ends a run silently or runs a partial tool call."""

from __future__ import annotations

import json
from itertools import chain, repeat
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from structlog.testing import capture_logs

from mindroom.agent_loop.output_limit import CUT_OFF_TOOL_CALL_RESULT, output_limit_policy
from mindroom.ai import ai_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, DefaultsConfig, ModelConfig
from mindroom.llm.errors import ModelOutputLimitError
from mindroom.llm.messages import Message
from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT
from tests.conftest import bind_runtime_paths, make_turn_context, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_OUTPUT_LIMIT = 32768
# Anthropic streams tool input one complete key and value at a time, so a call cut off before its last
# argument ends with the arguments finished before the cut, which parse into a runnable call.
_CUT_OFF_INPUT_JSON = '{"file_name": "big.py", "contents": "print(1)\\n"'


def _message(content: list[dict[str, Any]], *, stop_reason: str, output_tokens: int = 10) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": output_tokens},
    }


def _cut_off_write() -> dict[str, Any]:
    """The model spent its whole output budget inside one large file write."""
    return _message(
        [
            {"type": "text", "text": "Writing the script now."},
            {
                "type": "tool_use",
                "id": "toolu_cut",
                "name": "save_file",
                "input": {"file_name": "big.py", "contents": "print(1)\n"},
            },
        ],
        stop_reason="max_tokens",
        output_tokens=_OUTPUT_LIMIT,
    )


def _small_write() -> dict[str, Any]:
    """The model splits the work and writes the first part in a call of normal size."""
    return _message(
        [
            {
                "type": "tool_use",
                "id": "toolu_part",
                "name": "save_file",
                "input": {"file_name": "part1.py", "contents": "print(1)\n"},
            },
        ],
        stop_reason="tool_use",
    )


def _answer(text: str) -> dict[str, Any]:
    return _message([{"type": "text", "text": text}], stop_reason="end_turn")


def _event(kind: str, **fields: object) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **fields})}\n\n"


def _stream(message: dict[str, Any]) -> str:
    """Render a Messages API response as the SSE events Anthropic streams for it."""
    events = _event(
        "message_start",
        message={**message, "content": [], "stop_reason": None, "usage": {**message["usage"], "output_tokens": 1}},
    )
    for index, block in enumerate(message["content"]):
        if block["type"] == "text":
            events += _event("content_block_start", index=index, content_block={"type": "text", "text": ""})
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            events += _event("content_block_start", index=index, content_block={**block, "input": {}})
            cut_off = message["stop_reason"] == "max_tokens" and index == len(message["content"]) - 1
            delta = {
                "type": "input_json_delta",
                "partial_json": _CUT_OFF_INPUT_JSON if cut_off else json.dumps(block["input"]),
            }
        events += _event("content_block_delta", index=index, delta=delta)
        events += _event("content_block_stop", index=index)
    events += _event(
        "message_delta",
        delta={"stop_reason": message["stop_reason"], "stop_sequence": None},
        usage={"output_tokens": message["usage"]["output_tokens"]},
    )
    return events + _event("message_stop")


def _coder_config(
    tmp_path: Path,
    *,
    context_window: int | None = None,
    compaction: CompactionConfig | None = None,
) -> Config:
    """A Claude coding agent whose file tool writes into its workspace below the test directory."""
    return bind_runtime_paths(
        Config(
            agents={
                "coder": AgentConfig(display_name="Coder", model="claude", tools=["file"], memory_backend="file"),
            },
            models={
                "claude": ModelConfig(
                    provider="anthropic",
                    id="claude-sonnet-5-5",
                    context_window=context_window,
                    extra_kwargs={"api_key": "dummy", "max_tokens": _OUTPUT_LIMIT},
                ),
            },
            defaults=DefaultsConfig(compaction=compaction),
            memory="none",
        ),
        test_runtime_paths(tmp_path),
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A Claude coding agent whose file tool writes into its workspace below the test directory."""
    return _coder_config(tmp_path)


@pytest.fixture
def compacting_config(tmp_path: Path) -> Config:
    """The same agent with a context window that one cut-off response's output overfills, so its retry is compacted.

    Naming the summary model keeps compaction in MindRoom instead of Claude's native compaction.
    """
    return _coder_config(
        tmp_path,
        context_window=30_000,
        compaction=CompactionConfig(model="claude", reserve_tokens=4096),
    )


def _http_response(body: dict[str, Any], message: dict[str, Any]) -> httpx.Response:
    """Answer a Messages API request with ``message``, streamed when the request asked for a stream."""
    if body.get("stream"):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_stream(message))
    return httpx.Response(200, json=message)


def _fake_claude(monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Serve scripted Messages API responses to every Claude model MindRoom builds; return the request bodies.

    The last response repeats for every further request, as a model that keeps doing the same thing would.
    Only compaction asks for a summary without tools, so such a request gets a summary instead.
    """
    bodies: list[dict[str, Any]] = []
    pending = chain(responses, repeat(responses[-1]))

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        message = next(pending) if "tools" in body else _answer("The agent started writing big.py in one call.")
        return _http_response(body, message)

    monkeypatch.setattr(MODEL_HTTP_CLIENT, "send", httpx.AsyncClient(transport=httpx.MockTransport(handle)).send)
    return bodies


def _tool_results(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [block for block in body["messages"][-1]["content"] if block["type"] == "tool_result"]


async def _reply(config: Config, prompt: str, *, stream: bool) -> str:
    """Answer one prompt the way the OpenAI-compatible endpoint does, or through the streaming path chat uses."""
    return await ai_response(
        make_turn_context("coder", session_id="session-1"),
        prompt=prompt,
        runtime_paths=runtime_paths_for(config),
        config=config,
        include_interactive_questions=False,
        include_openai_compat_guidance=True,
        show_tool_calls=False,
        collect_streamed_response=stream,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_cut_off_tool_call_is_answered_instead_of_run(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """The cut-off call never runs; the model is told why and gets another request instead of the run ending."""
    bodies = _fake_claude(monkeypatch, [_cut_off_write(), _answer("Wrote big.py in two parts.")])

    response = await _reply(config, "Write the full simulation script to big.py.", stream=stream)

    assert not list(tmp_path.rglob("big.py"))
    assert "Wrote big.py in two parts." in response
    assert len(bodies) == 2
    [result] = _tool_results(bodies[1])
    assert result["tool_use_id"] == "toolu_cut"
    assert "hit the output limit" in json.dumps(result["content"])


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_run_that_keeps_cutting_off_its_tool_calls_fails_visibly(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """Two corrective retries per run, then a clear error instead of a silent or endless run."""
    bodies = _fake_claude(monkeypatch, [_cut_off_write()])

    response = await _reply(config, "Write the full simulation script to big.py.", stream=stream)

    assert not list(tmp_path.rglob("big.py"))
    assert len(bodies) == 3
    assert "⚠️" in response
    assert "output limit" in response
    assert "smaller steps" in response


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_complete_responses_between_cut_offs_do_not_renew_the_retries(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """A model that keeps returning to its oversized call between small ones still ends at its third cut-off."""
    bodies = _fake_claude(
        monkeypatch,
        [_cut_off_write(), _small_write(), _cut_off_write(), _small_write(), _cut_off_write(), _answer("Wrote it.")],
    )

    response = await _reply(config, "Write the full simulation script to big.py.", stream=stream)

    assert len(bodies) == 5
    assert "Wrote it." not in response
    assert "output limit" in response
    assert [path.name for path in tmp_path.rglob("*.py")] == ["part1.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_compaction_between_cut_offs_does_not_renew_the_retries(
    compacting_config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """Compaction folds the earlier cut-off responses into its summary, and the third one still ends the run."""
    bodies = _fake_claude(
        monkeypatch,
        [_cut_off_write(), _cut_off_write(), _cut_off_write(), _answer("Wrote big.py in two parts.")],
    )

    response = await _reply(compacting_config, "Write the full simulation script to big.py.", stream=stream)

    requests = [body for body in bodies if "tools" in body]
    assert len(requests) == 3
    # Both retries were compacted first, so neither carried an earlier cut-off response.
    assert len(bodies) - len(requests) == 2
    assert not [
        block
        for body in requests[1:]
        for message in body["messages"]
        for block in message["content"]
        if block["type"] == "tool_use"
    ]
    assert not list(tmp_path.rglob("big.py"))
    assert "Wrote big.py in two parts." not in response
    assert "output limit" in response
    assert "smaller steps" in response


def _cut_off_call() -> Message:
    call = {"id": "toolu_cut", "type": "function", "function": {"name": "save_file", "arguments": "{}"}}
    return Message(role="assistant", tool_calls=[call])


def test_each_run_gets_its_own_cut_off_retries() -> None:
    """A loop that continues a finished loop's messages, as the skill review's fork does, has two retries of its own.

    Two runs that share one model count their cut-offs apart, so neither spends the other's retries.
    """
    first, second = output_limit_policy(), output_limit_policy()
    for _ in range(2):
        [result] = first(_cut_off_call(), "output_limit") or []
        assert (result.tool_call_id, result.content, result.tool_call_error) == (
            "toolu_cut",
            CUT_OFF_TOOL_CALL_RESULT,
            True,
        )
        assert second(_cut_off_call(), "output_limit")
    assert first(_cut_off_call(), "tool_use") is None
    with pytest.raises(ModelOutputLimitError, match="smaller steps"):
        first(_cut_off_call(), "output_limit")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_cut_off_text_is_delivered_as_is_and_logged(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """Text cut off at the output limit is already visible, so it is kept without another request."""
    cut_off_text = _message(
        [{"type": "text", "text": "Chapter one of the report, which goes on and"}],
        stop_reason="max_tokens",
        output_tokens=_OUTPUT_LIMIT,
    )
    bodies = _fake_claude(monkeypatch, [cut_off_text])

    with capture_logs() as logs:
        response = await _reply(config, "Write the whole report inline.", stream=stream)

    assert response == "Chapter one of the report, which goes on and"
    assert len(bodies) == 1
    assert [log for log in logs if log["event"] == "claude_output_limit_reached"] == [
        {
            "event": "claude_output_limit_reached",
            "log_level": "warning",
            "model_id": "claude-sonnet-5-5",
            "stop_reason": "max_tokens",
            "tool_calls": 0,
        },
    ]
