"""OpenRouter wire requests carry the Claude cache ladder only for Anthropic-routed models."""

from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest
from agno.models.message import Message

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.hooks.enrichment import render_transient_context
from mindroom.model_loading import get_model_instance
from mindroom.openai_models import MindRoomOpenRouter
from mindroom.system_prompt import render_session_context
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

_ONE_HOUR = {"type": "ephemeral", "ttl": "1h"}
_SHARED = "Shared agent instructions.\n"
_TOOLS = [
    {"type": "function", "function": {"name": "first", "description": "First.", "parameters": {"type": "object"}}},
    {"type": "function", "function": {"name": "second", "description": "Second.", "parameters": {"type": "object"}}},
]
_COMPLETION = {
    "id": "gen-test",
    "object": "chat.completion",
    "created": 1,
    "model": "anthropic/claude-haiku-4.5",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {
        "prompt_tokens": 5000,
        "completion_tokens": 1,
        "total_tokens": 5001,
        "prompt_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 900},
    },
}


def _model(model_id: str = "anthropic/claude-haiku-4.5", **kwargs: object) -> tuple[MindRoomOpenRouter, list[dict]]:
    """Return an OpenRouter model whose HTTP requests are captured instead of sent."""
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_COMPLETION)

    model = MindRoomOpenRouter(
        id=model_id,
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        **kwargs,
    )
    return model, requests


def _send(
    model: MindRoomOpenRouter,
    requests: list[dict],
    messages: list[Message],
    tools: list[dict] | None = None,
) -> dict:
    model.response(messages=messages, tools=tools)
    return requests[-1]


def _conversation(day: str = "Monday") -> list[Message]:
    return [
        Message(role="system", content=_SHARED + render_session_context(f"Current day: {day}.")),
        Message(role="user", content="Look something up."),
        Message(
            role="assistant",
            content="",
            tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "first", "arguments": "{}"}}],
        ),
        Message(role="tool", content="Lookup result.", tool_call_id="call_1"),
        Message(role="assistant", content="Found it."),
        Message(role="user", content="Summarize it."),
    ]


def _markers(payload: dict) -> list[dict]:
    """Collect every cache_control marker anywhere in one request body."""
    found: list[dict] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            if "cache_control" in value:
                found.append(value["cache_control"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return found


def test_anthropic_route_places_the_claude_ladder() -> None:
    """System prefix, two newest message parts, and the last tool carry one TTL within the limit."""
    model, requests = _model()

    payload = _send(model, requests, _conversation(), tools=deepcopy(_TOOLS))
    messages = payload["messages"]

    assert messages[0]["content"] == [
        {"type": "text", "text": _SHARED, "cache_control": _ONE_HOUR},
        {"type": "text", "text": render_session_context("Current day: Monday.")},
    ]
    assert messages[-1]["content"] == [{"type": "text", "text": "Summarize it.", "cache_control": _ONE_HOUR}]
    assert messages[-2]["content"] == [{"type": "text", "text": "Found it.", "cache_control": _ONE_HOUR}]
    # Unmarked messages keep their original shape.
    assert messages[1]["content"] == "Look something up."
    assert messages[2]["content"] == ""
    assert messages[3]["content"] == "Lookup result."
    assert "cache_control" not in payload["tools"][0]
    assert payload["tools"][1]["cache_control"] == _ONE_HOUR
    assert _markers(payload) == [_ONE_HOUR] * 4


def test_tool_loop_request_extends_the_previous_boundary() -> None:
    """A tool result takes the newest rung so the next loop iteration reads the previous prefix."""
    model, requests = _model()
    messages = _conversation()[:4]

    payload = _send(model, requests, messages)

    assert payload["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": [{"type": "text", "text": "Lookup result.", "cache_control": _ONE_HOUR}],
    }
    assert payload["messages"][1]["content"] == [
        {"type": "text", "text": "Look something up.", "cache_control": _ONE_HOUR},
    ]
    assert "cache_control" not in json.dumps(payload["messages"][2])


def test_shared_prefix_is_independent_of_session_context() -> None:
    """Changing the date leaves the marked shared system part byte-identical."""
    model, requests = _model()

    first = _send(model, requests, _conversation("Monday"))
    second = _send(model, requests, _conversation("Tuesday"))

    assert first["messages"][0]["content"][0] == second["messages"][0]["content"][0]
    assert first["messages"][0]["content"][1] != second["messages"][0]["content"][1]


def test_transient_context_moves_after_the_durable_prompt() -> None:
    """Per-request context must stay out of the prefix that later turns replay."""
    model, requests = _model()
    transient = render_transient_context(["Current memory."])
    messages = [
        Message(role="system", content=_SHARED),
        Message(role="user", content=transient),
        Message(role="user", content="Durable prompt."),
    ]

    payload = _send(model, requests, messages)

    assert payload["messages"][1:] == [
        {"role": "user", "content": [{"type": "text", "text": "Durable prompt.", "cache_control": _ONE_HOUR}]},
        {"role": "user", "content": transient},
    ]


def test_multimodal_user_content_marks_the_text_part() -> None:
    """Image parts stay unmarked while the newest text part in the message takes the rung."""
    model, requests = _model()
    content = [
        {"type": "text", "text": "Describe this."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    messages = [Message(role="system", content=_SHARED), Message(role="user", content=content)]

    payload = _send(model, requests, messages)

    assert payload["messages"][1]["content"] == [
        {"type": "text", "text": "Describe this.", "cache_control": _ONE_HOUR},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    assert content[0] == {"type": "text", "text": "Describe this."}


@pytest.mark.parametrize(
    "model_id",
    ["deepseek/deepseek-v4.1-flash", "openai/gpt-6-astra", "z-ai/glm-5.3", "google/gemini-3.8-flash"],
)
def test_non_anthropic_routes_are_unchanged(model_id: str) -> None:
    """Implicitly cached providers keep the plain request format."""
    model, requests = _model(model_id)
    messages = _conversation()

    payload = _send(model, requests, messages, tools=deepcopy(_TOOLS))

    assert _markers(payload) == []
    assert payload["messages"][0]["content"] == messages[0].content


def test_cache_ladder_can_be_disabled() -> None:
    """The direct Claude opt-out also leaves OpenRouter Claude requests unmarked."""
    model, requests = _model(cache_system_prompt=False)

    payload = _send(model, requests, _conversation(), tools=deepcopy(_TOOLS))

    assert _markers(payload) == []


def test_five_minute_lifetime_can_be_selected() -> None:
    """``extended_cache_time: false`` sends the default ephemeral lifetime."""
    model, requests = _model("~anthropic/claude-sonnet-latest", extended_cache_time=False)

    payload = _send(model, requests, _conversation(), tools=deepcopy(_TOOLS))

    assert _markers(payload) == [{"type": "ephemeral"}] * 4


def test_history_and_tool_definitions_are_not_mutated() -> None:
    """Markers are added to request copies, never to persisted messages or reusable tool schemas."""
    model, requests = _model()
    messages = _conversation()
    tools = deepcopy(_TOOLS)
    snapshot = deepcopy((messages, tools))

    _send(model, requests, messages, tools=tools)

    assert (messages[:-1], tools) == (snapshot[0], snapshot[1])


def test_cached_usage_is_reported() -> None:
    """OpenRouter cache reads and writes reach MindRoom's usage metrics."""
    model, requests = _model()
    messages = _conversation()

    _send(model, requests, messages)

    assert messages[-1].metrics is not None
    assert messages[-1].metrics.cache_read_tokens == 4000
    assert messages[-1].metrics.cache_write_tokens == 900


def test_model_loading_enables_caching_with_config_opt_out(tmp_path: object) -> None:
    """Configured OpenRouter models cache by default and honor the documented extra_kwargs."""
    config = bind_runtime_paths(
        Config(
            models={
                "default": ModelConfig(
                    provider="openrouter",
                    id="anthropic/claude-sonnet-5",
                    extra_kwargs={"api_key": "dummy-key"},
                ),
                "opted_out": ModelConfig(
                    provider="openrouter",
                    id="anthropic/claude-sonnet-5",
                    extra_kwargs={"api_key": "dummy-key", "cache_system_prompt": False},
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    default = get_model_instance(config, runtime_paths_for(config), "default")
    opted_out = get_model_instance(config, runtime_paths_for(config), "opted_out")

    assert isinstance(default, MindRoomOpenRouter)
    assert isinstance(opted_out, MindRoomOpenRouter)
    assert (default.cache_system_prompt, default.extended_cache_time) == (True, True)
    assert opted_out.cache_system_prompt is False
