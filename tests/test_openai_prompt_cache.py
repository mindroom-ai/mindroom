"""OpenAI wire requests retain cacheable instructions across changing sessions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import httpx
import pytest
from agno.models.message import Message
from openai import AsyncOpenAI, OpenAI

from mindroom.codex_model import CodexResponses
from mindroom.openai_models import MindRoomOpenAIResponses
from mindroom.system_prompt import render_session_context


@pytest.fixture(autouse=True)
def clear_endpoint_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep provider selection independent of the developer's local API proxy."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


def _messages(day: str) -> list[Message]:
    return [
        Message(role="system", content="Shared agent instructions.\n" + render_session_context(f"Current day: {day}.")),
        Message(role="user", content="Write an introduction."),
    ]


@pytest.mark.parametrize("model_type", [MindRoomOpenAIResponses, CodexResponses])
def test_system_prefix_is_independent_of_session_context(model_type: type[MindRoomOpenAIResponses]) -> None:
    """A changed date must leave the complete first developer message unchanged."""
    model = model_type(id="gpt-6-astra", api_key="test-key", store=False)
    original = _messages("Monday")
    snapshot = deepcopy(original)

    first = model._format_messages(original)
    second = model._format_messages(_messages("Tuesday"))

    assert first[0] == second[0]
    assert first[0]["role"] == first[1]["role"] == "developer"
    assert first[0]["content"][0]["text"] == "Shared agent instructions.\n"
    assert "Current day: Monday." in first[1]["content"]
    assert "Current day: Tuesday." in second[1]["content"]
    assert first[0]["content"][0]["text"] + first[1]["content"] == original[0].content
    assert first[2] == {"role": "user", "content": "Write an introduction."}
    assert original == snapshot
    if model_type is CodexResponses:
        assert "prompt_cache_breakpoint" not in first[0]["content"][0]
    else:
        assert first[0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}


@pytest.mark.parametrize(
    ("model_id", "supported"),
    [
        ("gpt-6-astra", True),
        ("gpt-5.6", True),
        ("gpt-5.6-2026-07-01", True),
        ("gpt-5.5", False),
        ("gpt-5", False),
        ("o3", False),
    ],
)
def test_native_breakpoints_require_supported_models(model_id: str, *, supported: bool) -> None:
    """Legacy models must never receive an unsupported explicit-cache field."""
    model = MindRoomOpenAIResponses(id=model_id, api_key="test-key", store=False)
    first = model._format_messages(_messages("Monday"))[0]

    assert ("prompt_cache_breakpoint" in first["content"][0]) is supported


@pytest.mark.parametrize("route", ["base_url", "client_params", "environment", "sync_client", "async_client"])
def test_custom_endpoints_do_not_receive_native_breakpoints(route: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI-compatible proxies need not accept OpenAI's cache extension."""
    endpoint = "https://proxy.example.test/v1"
    kwargs: dict[str, Any] = {}
    if route == "environment":
        monkeypatch.setenv("OPENAI_BASE_URL", endpoint)
    elif route == "client_params":
        kwargs["client_params"] = {"base_url": endpoint}
    elif route == "sync_client":
        kwargs["client"] = OpenAI(api_key="test-key", base_url=endpoint)
    elif route == "async_client":
        kwargs["async_client"] = AsyncOpenAI(api_key="test-key", base_url=endpoint)
    else:
        kwargs["base_url"] = httpx.URL(endpoint)
    model = MindRoomOpenAIResponses(id="gpt-6-astra", api_key="test-key", store=False, **kwargs)

    first = model._format_messages(_messages("Monday"))[0]

    assert "prompt_cache_breakpoint" not in first["content"][0]


def test_cache_boundary_can_be_disabled() -> None:
    """An explicit opt-out must preserve the unsplit wire prompt."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", api_key="test-key", cache_system_prompt=False)
    messages = _messages("Monday")

    assert model._format_messages(messages) == [
        {"role": "developer", "content": messages[0].content},
        {"role": "user", "content": messages[1].content},
    ]


def test_user_boundary_literal_and_custom_content_are_untouched() -> None:
    """Only MindRoom's initial text system message owns an automatic cache boundary."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", api_key="test-key", store=False)
    custom = [{"type": "input_text", "text": render_session_context("Custom content.")}]
    messages = [
        Message(role="system", content=custom),
        Message(role="user", content=render_session_context("User text.")),
    ]

    assert model._format_messages(messages) == [
        {"role": "developer", "content": custom},
        {"role": "user", "content": messages[1].content},
    ]


def test_stored_response_continuation_does_not_reinsert_system_prompt() -> None:
    """Response-ID chaining must retain only the continuation items selected by Agno."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", api_key="test-key", store=True)
    messages = [
        *_messages("Monday"),
        Message(role="assistant", content="Introduction.", provider_data={"response_id": "resp_previous"}),
        Message(role="user", content="Continue."),
    ]

    assert model._format_messages(messages) == [{"role": "user", "content": "Continue."}]
