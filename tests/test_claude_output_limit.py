"""A Claude response cut off at its output limit never ends a run silently or runs a partial tool call."""

from __future__ import annotations

import json
from itertools import chain, repeat
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from structlog.testing import capture_logs

from mindroom.ai import ai_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.providers import model_loading
from tests.conftest import bind_runtime_paths, make_turn_context, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from agno.models.base import Model

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

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


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """A Claude coding agent whose file tool writes into the test directory."""
    # Without a configured workspace the file tool writes relative to the working directory.
    monkeypatch.chdir(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"coder": AgentConfig(display_name="Coder", model="claude", tools=["file"])},
            models={
                "claude": ModelConfig(
                    provider="anthropic",
                    id="claude-sonnet-5-5",
                    extra_kwargs={"api_key": "dummy", "max_tokens": _OUTPUT_LIMIT},
                ),
            },
            memory="none",
        ),
        test_runtime_paths(tmp_path),
    )


def _fake_claude(monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Serve scripted Messages API responses to every Claude model MindRoom loads; return the request bodies.

    The last response repeats for every further request, as a model that keeps doing the same thing would.
    """
    bodies: list[dict[str, Any]] = []
    pending = chain(responses, repeat(responses[-1]))

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        message = next(pending)
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_stream(message))
        return httpx.Response(200, json=message)

    load_model = model_loading.get_model_instance

    def get_model_instance(
        config: Config,
        runtime_paths: RuntimePaths,
        model_name: str = "default",
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> Model:
        model = load_model(config, runtime_paths, model_name, execution_identity)
        model.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        return model

    monkeypatch.setattr(model_loading, "get_model_instance", get_model_instance)
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
