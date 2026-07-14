"""Tests for the Codex-backed OpenAI Responses model provider."""

from __future__ import annotations

import base64
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from agno.metrics import MessageMetrics
from agno.models.anthropic import Claude
from agno.models.message import Message
from agno.models.openai import OpenAIChat
from agno.models.response import ModelResponse
from agno.utils.models.claude import format_messages as claude_format_messages
from openai.types.responses import Response, ResponseOutputItemDoneEvent, ResponseTextDeltaEvent

from mindroom import codex_model
from mindroom.codex_model import _CODEX_BASE_URL, CodexResponses, _borrow_codex_key, normalize_codex_model_id
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.model_loading import get_model_instance
from mindroom.openai_responses_model import MindRoomOpenAIResponses
from mindroom.openai_tool_search import (
    _DEFERRED_TOOL_NAMES_ATTR,
    install_openai_deferred_tool_search,
    openai_native_tool_search_supported,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity

if TYPE_CHECKING:
    from collections.abc import Iterator


def _jwt_with_exp(exp: int) -> str:
    payload = json.dumps({"exp": exp}).encode()
    encoded_payload = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"header.{encoded_payload}.signature"


def _write_codex_auth(codex_home: Path, access_token: str, refresh_value: str) -> None:
    codex_home.mkdir()
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_value,
            "account_id": "acct_123",
        },
    }
    (codex_home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")


@pytest.mark.parametrize(
    ("configured_id", "endpoint_id"),
    [
        ("gpt-5.6", "gpt-5.6-sol"),
        ("openai-codex/gpt-5.6", "gpt-5.6-sol"),
        ("gpt-5.6-sol", "gpt-5.6-sol"),
        ("openai-codex/gpt-5.6-terra", "gpt-5.6-terra"),
        ("gpt-5.4", "gpt-5.4"),
    ],
)
def test_normalize_codex_model_id_uses_endpoint_slug(configured_id: str, endpoint_id: str) -> None:
    """The public GPT-5.6 alias should resolve to the slug accepted by the Codex endpoint."""
    assert normalize_codex_model_id(configured_id) == endpoint_id


def test_borrow_codex_key_uses_unexpired_chatgpt_access_token(tmp_path: Path) -> None:
    """A valid Codex CLI ChatGPT token should be reused directly."""
    access_token = _jwt_with_exp(int(time.time()) + 3600)
    codex_home = tmp_path / ".codex"
    _write_codex_auth(codex_home, access_token, "refresh-value")

    token, account_id = _borrow_codex_key(codex_home=codex_home)

    assert token == access_token
    assert account_id == "acct_123"


def test_borrow_codex_key_refreshes_expired_access_token(tmp_path: Path) -> None:
    """Expired Codex CLI ChatGPT tokens should be refreshed and persisted."""
    codex_home = tmp_path / ".codex"
    _write_codex_auth(codex_home, _jwt_with_exp(int(time.time()) - 60), "refresh-value")
    refreshed_token = _jwt_with_exp(int(time.time()) + 7200)
    new_id_value = "new-id-value"
    new_refresh_value = "new-refresh-value"

    with patch(
        "mindroom.codex_model._refresh_codex_tokens",
        return_value={
            "access_token": refreshed_token,
            "id_token": new_id_value,
            "refresh_token": new_refresh_value,
        },
    ):
        token, account_id = _borrow_codex_key(codex_home=codex_home)

    auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert token == refreshed_token
    assert account_id == "acct_123"
    assert auth["tokens"]["access_token"] == refreshed_token
    assert auth["tokens"]["id_token"] == new_id_value
    assert auth["tokens"]["refresh_token"] == new_refresh_value
    assert "last_refresh" in auth


def test_write_codex_auth_creates_private_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refreshed Codex OAuth tokens should never be written through a world-readable temp file."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    auth_path = codex_home / "auth.json"
    observed_temp_modes: list[int] = []
    original_replace = Path.replace

    def spy_replace(self: Path, target: str | Path) -> Path:
        if self.name == "auth.json.tmp":
            observed_temp_modes.append(stat.S_IMODE(self.stat().st_mode))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)
    old_umask = os.umask(0o022)
    try:
        codex_model._write_codex_auth(auth_path, {"auth_mode": "chatgpt", "tokens": {"access_token": "token"}})
    finally:
        os.umask(old_umask)

    assert observed_temp_modes == [0o600]
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600


def test_borrow_codex_key_serializes_concurrent_refreshes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent expired-token readers should share one refreshed token instead of racing refresh-token rotation."""
    codex_home = tmp_path / ".codex"
    _write_codex_auth(codex_home, _jwt_with_exp(int(time.time()) - 60), "refresh-value")
    refreshed_token = _jwt_with_exp(int(time.time()) + 7200)
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    refresh_call_count = 0
    refresh_call_count_lock = threading.Lock()
    results: list[tuple[str, str | None]] = []
    errors: list[BaseException] = []

    def fake_refresh(received_refresh: str) -> dict[str, str]:
        nonlocal refresh_call_count
        assert received_refresh == "refresh-value"
        with refresh_call_count_lock:
            refresh_call_count += 1
        refresh_started.set()
        assert release_refresh.wait(timeout=2)
        return {
            "access_token": refreshed_token,
            "refresh_token": "new-refresh-value",
        }

    def borrow_key() -> None:
        try:
            results.append(_borrow_codex_key(codex_home=codex_home))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(codex_model, "_refresh_codex_tokens", fake_refresh)

    first_thread = threading.Thread(target=borrow_key)
    first_thread.start()
    assert refresh_started.wait(timeout=2)

    second_thread = threading.Thread(target=borrow_key)
    second_thread.start()
    release_refresh.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert refresh_call_count == 1
    assert results == [(refreshed_token, "acct_123"), (refreshed_token, "acct_123")]


def test_codex_responses_client_params_use_codex_endpoint_and_account_header(tmp_path: Path) -> None:
    """CodexResponses should translate Codex CLI auth into OpenAI client params."""
    codex_home = tmp_path / ".codex"
    access_token = _jwt_with_exp(int(time.time()) + 3600)
    _write_codex_auth(codex_home, access_token, "refresh-value")

    model = CodexResponses(id="gpt-5.6", codex_home=str(codex_home), default_headers={"X-Test": "1"})

    params = model._get_client_params()

    assert params["api_key"] == access_token
    assert params["base_url"] == _CODEX_BASE_URL
    assert params["default_headers"] == {
        "X-Test": "1",
        "ChatGPT-Account-ID": "acct_123",
    }


def test_codex_responses_request_params_include_required_instructions() -> None:
    """Codex Responses requests should always include top-level instructions."""
    default_model = CodexResponses(id="gpt-5.6")
    configured_model = CodexResponses(id="gpt-5.6", instructions=["Be brief.", "Return plain text."])

    assert default_model.get_request_params()["instructions"] == "You are a helpful assistant."
    assert configured_model.get_request_params()["instructions"] == "Be brief.\n\nReturn plain text."


def test_codex_responses_request_params_drop_unsupported_params() -> None:
    """Unsupported OpenAI Responses parameters should not be sent to Codex."""
    model = CodexResponses(id="gpt-5.6", max_output_tokens=40, temperature=0)

    params = model.get_request_params()

    assert "max_output_tokens" not in params
    assert "temperature" not in params


def test_codex_responses_request_params_include_prompt_cache_key(tmp_path: Path) -> None:
    """Codex should expose OpenAI's cache-routing key when configured."""
    model = CodexResponses(id="gpt-5.6", prompt_cache_key="mindroom-code-agent", codex_home=str(tmp_path))

    params = model.get_request_params()

    assert params["prompt_cache_key"] == "mindroom-code-agent"
    assert params["extra_headers"] == {
        "session_id": "mindroom-code-agent",
        "x-client-request-id": "mindroom-code-agent",
        "x-codex-window-id": "mindroom-code-agent:0",
    }


def test_codex_responses_request_params_include_installation_metadata(tmp_path: Path) -> None:
    """Codex should forward the local CLI installation id in Responses client_metadata."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "installation_id").write_text("install-123\n", encoding="utf-8")
    model = CodexResponses(id="gpt-5.6", prompt_cache_key="mindroom-code-agent", codex_home=str(codex_home))

    params = model.get_request_params()

    assert params["extra_body"] == {
        "client_metadata": {
            "x-codex-installation-id": "install-123",
        },
    }


def test_codex_responses_request_params_preserve_existing_extra_body(tmp_path: Path) -> None:
    """Codex client metadata should merge into caller-supplied extra_body."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "installation_id").write_text("install-123\n", encoding="utf-8")
    model = CodexResponses(
        id="gpt-5.6",
        prompt_cache_key="mindroom-code-agent",
        codex_home=str(codex_home),
        extra_body={
            "debug": True,
            "client_metadata": {
                "x-codex-installation-id": "custom-install",
                "x-test": "1",
            },
        },
    )

    assert model.get_request_params()["extra_body"] == {
        "debug": True,
        "client_metadata": {
            "x-codex-installation-id": "custom-install",
            "x-test": "1",
        },
    }


def test_codex_responses_request_params_preserve_existing_extra_headers(tmp_path: Path) -> None:
    """Codex prompt-cache headers should not clobber caller-supplied headers."""
    model = CodexResponses(
        id="gpt-5.6",
        prompt_cache_key="mindroom-code-agent",
        codex_home=str(tmp_path),
        extra_headers={"X-Test": "1", "x-codex-window-id": "custom-window"},
    )

    assert model.get_request_params()["extra_headers"] == {
        "X-Test": "1",
        "session_id": "mindroom-code-agent",
        "x-client-request-id": "mindroom-code-agent",
        "x-codex-window-id": "custom-window",
    }


def test_codex_model_loader_derives_prompt_cache_key_from_execution_identity(tmp_path: Path) -> None:
    """MindRoom should use a stable per-agent/session Codex cache key by default."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    config = Config(
        models={
            "default": ModelConfig(
                provider="codex",
                id="gpt-5.6",
            ),
        },
        agents={},
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread:example.org",
        resolved_thread_id="$thread:example.org",
        session_id="!room:example.org:$thread:example.org",
    )

    model = get_model_instance(config, runtime_paths, execution_identity=identity)
    params = model.get_request_params()

    assert isinstance(model, CodexResponses)
    assert params["prompt_cache_key"] == "mindroom-7ac97f304c4001bd9939c88ddba8b0e2"
    assert params["extra_headers"] == {
        "session_id": "mindroom-7ac97f304c4001bd9939c88ddba8b0e2",
        "x-client-request-id": "mindroom-7ac97f304c4001bd9939c88ddba8b0e2",
        "x-codex-window-id": "mindroom-7ac97f304c4001bd9939c88ddba8b0e2:0",
    }


def test_codex_responses_invoke_aggregates_streaming_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-streaming callers should still work with Codex's stream-only endpoint."""
    model = CodexResponses(id="gpt-5.6")
    usage = MessageMetrics(
        input_tokens=7,
        output_tokens=3,
        total_tokens=10,
        cache_read_tokens=5,
        reasoning_tokens=2,
    )

    def fake_invoke_stream(
        *,
        messages: list[Message],
        assistant_message: Message,
        response_format: object | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | dict[str, object] | None = None,
        run_response: object | None = None,
        compress_tool_results: bool = False,
    ) -> Iterator[ModelResponse]:
        del messages, response_format, tools, tool_choice, run_response, compress_tool_results
        model._ensure_message_metrics_initialized(assistant_message)
        yield ModelResponse(provider_data={"response_id": "resp_123"})
        yield ModelResponse(content="mindroom")
        yield ModelResponse(content="-codex-live-ok")
        yield ModelResponse(response_usage=usage)

    monkeypatch.setattr(model, "invoke_stream", fake_invoke_stream)
    assistant_message = Message(role="assistant")

    response = model.invoke([Message(role="user", content="hello")], assistant_message)

    assert response.content == "mindroom-codex-live-ok"
    assert response.provider_data == {"response_id": "resp_123"}
    assert response.response_usage == usage
    assert assistant_message.content == "mindroom-codex-live-ok"
    assert assistant_message.provider_data == {"response_id": "resp_123"}
    assert assistant_message.metrics.input_tokens == 7
    assert assistant_message.metrics.cache_read_tokens == 5
    assert assistant_message.metrics.reasoning_tokens == 2


def test_get_model_instance_supports_codex_provider(tmp_path: Path) -> None:
    """The model loader should expose Codex as a first-class model provider."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    config = Config(
        models={
            "default": ModelConfig(
                provider="codex",
                id="openai-codex/gpt-5.6",
            ),
        },
        agents={},
        prompts={"CODEX_DEFAULT_INSTRUCTIONS": "Custom Codex default instructions."},
    )

    with patch("mindroom.model_loading.logger.info") as log_info:
        model = get_model_instance(config, runtime_paths)

    assert isinstance(model, CodexResponses)
    assert model.id == "gpt-5.6-sol"
    log_info.assert_called_once_with(
        "Using AI model",
        model="default",
        provider="codex",
        configured_id="openai-codex/gpt-5.6",
        effective_id="gpt-5.6-sol",
    )
    assert model.store is False
    assert model.get_request_params()["instructions"] == "Custom Codex default instructions."
    assert str(model.base_url) == _CODEX_BASE_URL


_TOOL_SEARCH_CALL_ITEM = {
    "id": "ts_1",
    "type": "tool_search_call",
    "call_id": "tsc_1",
    "arguments": {"queries": ["weather"]},
    "execution": "server",
    "status": "completed",
}
_TOOL_SEARCH_OUTPUT_ITEM = {
    "id": "tso_1",
    "type": "tool_search_output",
    "call_id": "tsc_1",
    "execution": "server",
    "status": "completed",
    "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
}


def _agno_tool(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {"name": name, "description": f"{name} description", "parameters": {"type": "object"}},
    }


def _output_item_done_event(item: dict[str, object], index: int) -> ResponseOutputItemDoneEvent:
    return ResponseOutputItemDoneEvent.model_validate(
        {"type": "response.output_item.done", "item": item, "output_index": index, "sequence_number": index},
    )


class _FakeResponsesAPI:
    def __init__(self, event_batches: list[list[object]]) -> None:
        self._event_batches = iter(event_batches)
        self.captured_kwargs: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> Iterator[object]:
        self.captured_kwargs.append(kwargs)
        return iter(next(self._event_batches))


class _FakeCodexClient:
    def __init__(self, event_batches: list[list[object]]) -> None:
        self.responses = _FakeResponsesAPI(event_batches)


@pytest.mark.parametrize(
    ("provider", "model_id", "expected"),
    [
        ("codex", "gpt-5.6", True),
        ("openai_codex", "openai-codex/gpt-5.6", True),
        ("codex", "gpt-5.6-codex", True),
        ("codex", "gpt-5.4-mini", True),
        # Unreleased GPT versions default to the native path (version gating),
        # including major-only spellings, which parse as .0.
        ("codex", "gpt-6.0", True),
        ("codex", "gpt-6", True),
        ("codex", "gpt-6-codex", True),
        ("codex", "gpt-5-codex", False),
        ("codex", "gpt-4.1", False),
        ("codex", "codex-mini-latest", False),
        ("openai", "gpt-5.6", True),
        ("anthropic", "claude-opus-4-8", False),
    ],
)
def test_openai_native_tool_search_supported_gating(provider: str, model_id: str, *, expected: bool) -> None:
    """OpenAI and Codex providers qualify when the model id parses to gpt-5.4 or newer."""
    assert openai_native_tool_search_supported(provider, model_id) is expected


def test_openai_native_tool_search_rejects_custom_compatible_base_url() -> None:
    """Chat-Completions-compatible endpoints do not implicitly opt into the Responses API."""
    assert not openai_native_tool_search_supported(
        "openai",
        "gpt-5.6",
        base_url="http://localhost:9292/v1",
    )
    assert openai_native_tool_search_supported(
        "openai",
        "gpt-5.6",
        base_url="https://api.openai.com/v1/",
    )


def test_install_openai_deferred_tool_search_ignores_non_responses_models_and_empty_sets() -> None:
    """The installer is a no-op for non-Responses models and empty name sets."""
    claude = Claude(id="claude-opus-4-8", api_key="test-key")
    install_openai_deferred_tool_search(claude, deferred_tool_names=frozenset({"alpha_tool"}))
    assert _DEFERRED_TOOL_NAMES_ATTR not in vars(claude)

    codex = CodexResponses(id="gpt-5.6")
    install_openai_deferred_tool_search(codex, deferred_tool_names=frozenset())
    assert _DEFERRED_TOOL_NAMES_ATTR not in vars(codex)


def test_codex_deferred_tool_search_tags_tools_and_injects_search_tool() -> None:
    """Deferred tools ship tagged and name-sorted after the search tool and non-deferred tools."""
    model = CodexResponses(id="gpt-5.6")
    install_openai_deferred_tool_search(model, deferred_tool_names=frozenset({"zeta_tool", "alpha_tool"}))

    request_params = model.get_request_params(
        tools=[_agno_tool("always_tool"), _agno_tool("zeta_tool"), _agno_tool("alpha_tool")],
    )

    wire_tools = request_params["tools"]
    assert wire_tools[0] == {"type": "tool_search"}
    assert [tool.get("name") for tool in wire_tools] == [None, "always_tool", "alpha_tool", "zeta_tool"]
    assert "defer_loading" not in wire_tools[1]
    for deferred_tool in wire_tools[2:]:
        assert deferred_tool["defer_loading"] is True


def test_openai_responses_deferred_tool_search_tags_tools_and_injects_search_tool() -> None:
    """The regular OpenAI Responses model uses the same native deferred-tool wire format."""
    model = MindRoomOpenAIResponses(id="gpt-5.6", api_key="test-key")
    install_openai_deferred_tool_search(model, deferred_tool_names=frozenset({"sleep"}))

    request_params = model.get_request_params(tools=[_agno_tool("always_tool"), _agno_tool("sleep")])

    wire_tools = request_params["tools"]
    assert wire_tools[0] == {"type": "tool_search"}
    assert [tool.get("name") for tool in wire_tools] == [None, "always_tool", "sleep"]
    assert "defer_loading" not in wire_tools[1]
    assert wire_tools[2]["defer_loading"] is True


def test_codex_deferred_tool_search_leaves_requests_without_matching_tools_unchanged() -> None:
    """The search tool is injected only when a deferred tool is present in the request."""
    model = CodexResponses(id="gpt-5.6")
    install_openai_deferred_tool_search(model, deferred_tool_names=frozenset({"other_tool"}))

    request_params = model.get_request_params(tools=[_agno_tool("always_tool")])

    assert [tool["name"] for tool in request_params["tools"]] == ["always_tool"]


def test_codex_tool_search_items_round_trip_through_streaming_history() -> None:
    """tool_search_call and tool_search_output replay verbatim, in order, exactly once."""
    text_event = ResponseTextDeltaEvent.model_validate(
        {
            "type": "response.output_text.delta",
            "delta": "I found a weather tool.",
            "content_index": 0,
            "item_id": "msg_1",
            "output_index": 2,
            "sequence_number": 2,
            "logprobs": [],
        },
    )
    first_batch = [
        _output_item_done_event(_TOOL_SEARCH_CALL_ITEM, 0),
        _output_item_done_event(_TOOL_SEARCH_OUTPUT_ITEM, 1),
        text_event,
    ]
    client = _FakeCodexClient([first_batch, []])
    model = CodexResponses(id="gpt-5.6")
    vars(model)["get_client"] = lambda: client

    messages = [Message(role="user", content="What is the weather?")]
    model.response(messages=messages, compression_manager=None)
    model.response(messages=messages, compression_manager=None)

    expected_items = [
        _output_item_done_event(_TOOL_SEARCH_CALL_ITEM, 0).item.model_dump(exclude_none=True),
        _output_item_done_event(_TOOL_SEARCH_OUTPUT_ITEM, 1).item.model_dump(exclude_none=True),
    ]
    assert messages[1].provider_data == {"tool_search_items": expected_items}
    assert client.responses.captured_kwargs[1]["input"] == [
        {"role": "user", "content": "What is the weather?"},
        *expected_items,
        {"role": "assistant", "content": "I found a weather tool."},
    ]


def test_codex_tool_search_items_replay_ahead_of_the_discovered_function_call() -> None:
    """Captured items are reinserted immediately ahead of the message's replayed function calls."""
    model = CodexResponses(id="gpt-5.6")
    assistant = Message(
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "fc_1",
                "call_id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": "{}"},
            },
        ],
        provider_data={"tool_search_items": [dict(_TOOL_SEARCH_CALL_ITEM), dict(_TOOL_SEARCH_OUTPUT_ITEM)]},
    )
    tool_result = Message(role="tool", content="sunny", tool_call_id="fc_1")

    formatted_input = model._format_messages([Message(role="user", content="weather?"), assistant, tool_result])

    assert formatted_input == [
        {"role": "user", "content": "weather?"},
        dict(_TOOL_SEARCH_CALL_ITEM),
        dict(_TOOL_SEARCH_OUTPUT_ITEM),
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": "{}",
            "status": "completed",
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
    ]


def test_codex_parse_provider_response_captures_tool_search_items() -> None:
    """The non-streaming Response parse stores the search items Agno drops."""
    model = CodexResponses(id="gpt-5.6")
    response = Response.model_validate(
        {
            "id": "resp_1",
            "created_at": 1,
            "model": "gpt-5.6",
            "object": "response",
            "output": [dict(_TOOL_SEARCH_CALL_ITEM), dict(_TOOL_SEARCH_OUTPUT_ITEM)],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "metadata": None,
            "temperature": None,
            "top_p": None,
        },
    )

    model_response = model._parse_provider_response(response)

    assert model_response.provider_data["tool_search_items"] == [
        response.output[0].model_dump(exclude_none=True),
        response.output[1].model_dump(exclude_none=True),
    ]


def test_tool_search_items_replay_to_non_openai_provider_without_crashing() -> None:
    """History stored on the native path must stay replayable after a `!model` provider switch."""
    assistant = Message(
        role="assistant",
        content="I found a weather tool.",
        provider_data={"tool_search_items": [dict(_TOOL_SEARCH_CALL_ITEM), dict(_TOOL_SEARCH_OUTPUT_ITEM)]},
    )

    chat_wire = OpenAIChat(id="gpt-5.6", api_key="test-key")._format_message(assistant)
    assert chat_wire["role"] == "assistant"
    assert chat_wire["content"] == "I found a weather tool."

    claude_wire, _system = claude_format_messages([assistant])
    assert claude_wire[0]["role"] == "assistant"


def test_claude_server_tool_history_replays_to_codex_without_injection() -> None:
    """Anthropic-native server tool blocks are ignored when a thread switches to Codex."""
    assistant = Message(
        role="assistant",
        content="Claude searched here.",
        provider_data={"server_tool_blocks": [{"type": "server_tool_use", "id": "srv_1"}]},
    )

    formatted_input = CodexResponses(id="gpt-5.6")._format_messages([assistant])

    assert formatted_input == [{"role": "assistant", "content": "Claude searched here."}]


def test_codex_tool_search_replay_skips_identical_earlier_assistant_content() -> None:
    """An earlier identical-content assistant turn cannot claim a later message's anchor."""
    model = CodexResponses(id="gpt-5.6")
    later = Message(
        role="assistant",
        content="same text",
        provider_data={"tool_search_items": [dict(_TOOL_SEARCH_CALL_ITEM)]},
    )

    formatted_input = model._format_messages(
        [
            Message(role="user", content="q1"),
            Message(role="assistant", content="same text"),
            Message(role="user", content="q2"),
            later,
        ],
    )

    assert formatted_input == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "same text"},
        {"role": "user", "content": "q2"},
        dict(_TOOL_SEARCH_CALL_ITEM),
        {"role": "assistant", "content": "same text"},
    ]
