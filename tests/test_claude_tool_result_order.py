"""Claude requests keep tool results ahead of the media follow-up a sibling tool produced."""

from __future__ import annotations

import base64
import json
from itertools import takewhile
from typing import TYPE_CHECKING, Any

import httpx
from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.media import Image
from agno.models.anthropic import Claude
from agno.models.message import Message
from agno.run.base import RunStatus
from agno.tools import tool
from agno.tools.function import ToolResult

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.model_loading import get_model_instance
from mindroom.vertex_claude_compat import MindroomVertexAIClaude
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
)
# Agno appends this user message after a tool batch whose results carried media.
_MEDIA_FOLLOW_UP = "The tool call above generated the attached media."


def _message_body(content: list[dict[str, Any]], *, stop_reason: str) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _capturing_http_client(responses: list[dict[str, Any]]) -> tuple[httpx.Client, list[dict[str, Any]]]:
    """Return an SDK HTTP client that records each request body sent on the wire."""
    bodies: list[dict[str, Any]] = []
    pending = iter(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=next(pending))

    return httpx.Client(transport=httpx.MockTransport(handle)), bodies


def _loaded_claude(tmp_path: Path, http_client: httpx.Client) -> Claude:
    config = bind_runtime_paths(
        Config(
            models={
                "claude": ModelConfig(provider="anthropic", id="claude-opus-5-5", extra_kwargs={"api_key": "dummy"}),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    model = get_model_instance(config, runtime_paths_for(config), "claude")
    assert isinstance(model, Claude)
    model.http_client = http_client
    return model


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return wire blocks as dicts; pre-send payloads keep Agno's SDK objects."""
    return [block if isinstance(block, dict) else block.model_dump() for block in message["content"]]


def _assert_every_tool_use_answered_first(body: dict[str, Any]) -> None:
    """Anthropic only pairs tool_result blocks that open the next user turn."""
    messages = body["messages"]
    for index, message in enumerate(messages):
        if message["role"] != "assistant":
            continue
        tool_use_ids = {block["id"] for block in _blocks(message) if block["type"] == "tool_use"}
        if not tool_use_ids:
            continue
        next_turn = messages[index + 1]
        assert next_turn["role"] == "user"
        leading = takewhile(lambda block: block["type"] == "tool_result", _blocks(next_turn))
        assert {block["tool_use_id"] for block in leading} == tool_use_ids


def _media_follow_up_blocks(body: dict[str, Any]) -> list[str]:
    return [
        block["type"]
        for message in body["messages"]
        for block in message["content"]
        if block["type"] == "image" or block.get("text") == _MEDIA_FOLLOW_UP
    ]


def test_approved_tool_result_leads_after_sibling_media(tmp_path: Path) -> None:
    """Resuming an approval sends the approved result with its siblings, before their media."""
    http_client, bodies = _capturing_http_client(
        [
            _message_body(
                [
                    {"type": "tool_use", "id": "toolu_view", "name": "view_image", "input": {}},
                    {"type": "tool_use", "id": "toolu_send", "name": "send_report", "input": {}},
                ],
                stop_reason="tool_use",
            ),
            _message_body([{"type": "text", "text": "Done."}], stop_reason="end_turn"),
        ],
    )

    def view_image() -> ToolResult:
        """Show the chart."""
        return ToolResult(content="chart ready", images=[Image(content=_PNG, mime_type="image/png")])

    @tool(requires_confirmation=True)
    def send_report() -> str:
        """Send the report."""
        return "report sent"

    agent = Agent(model=_loaded_claude(tmp_path, http_client), tools=[view_image, send_report], db=InMemoryDb())
    paused = agent.run("Show the chart and send the report.")
    assert paused.status == RunStatus.paused
    [requirement] = paused.requirements or []
    requirement.confirm()

    resumed = agent.continue_run(run_response=paused, requirements=[requirement])

    assert resumed.content == "Done."
    # The stored run keeps Agno's order: the media follow-up precedes the approved result.
    assert [message.role for message in resumed.messages or []][-4:] == ["tool", "user", "tool", "assistant"]
    _assert_every_tool_use_answered_first(bodies[1])
    assert _media_follow_up_blocks(bodies[1]) == ["text", "image"]


def _stored_responses_thread() -> list[Message]:
    """A thread answered by an OpenAI Responses model whose approved call resumed after sibling media."""
    calls = [
        {
            "id": f"fc_{name}",
            "call_id": f"call_{name}",
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }
        for name in ("view_image", "run_subagent")
    ]
    return [
        Message(role="user", content="Show the chart and summarize it.", from_history=True),
        Message(role="assistant", tool_calls=calls, from_history=True),
        Message(role="tool", tool_call_id="fc_view_image", tool_name="view_image", content="{}", from_history=True),
        Message(role="user", content=_MEDIA_FOLLOW_UP, from_history=True),
        Message(role="tool", tool_call_id="fc_run_subagent", tool_name="run_subagent", content="ok", from_history=True),
        Message(role="assistant", content="Here is the summary.", from_history=True),
        Message(role="user", content="Thanks, what next?"),
    ]


def test_stored_responses_thread_replays_to_claude_with_results_first(tmp_path: Path) -> None:
    """A thread stored by another provider keeps working after the agent switches to Claude."""
    http_client, bodies = _capturing_http_client(
        [_message_body([{"type": "text", "text": "Next steps."}], stop_reason="end_turn")],
    )
    model = _loaded_claude(tmp_path, http_client)
    messages = _stored_responses_thread()

    model.response(messages=messages, compression_manager=None)

    _assert_every_tool_use_answered_first(bodies[0])
    assert _media_follow_up_blocks(bodies[0]) == ["text"]
    assert [message.role for message in messages[:6]] == ["user", "assistant", "tool", "user", "tool", "assistant"]


def test_vertex_claude_request_payload_puts_tool_results_first() -> None:
    """Vertex shares the Claude request preparation, including its token-count payload."""
    model = MindroomVertexAIClaude(id="claude-opus-5-5", project_id="demo-project", region="us-central1")

    payload = model._request_input_kwargs(
        _stored_responses_thread(),
        tools=None,
        response_format=None,
        compress_tool_results=False,
    )

    _assert_every_tool_use_answered_first(payload)
