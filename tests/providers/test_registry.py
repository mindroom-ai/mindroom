"""The provider registry: the adapter, identity, and credentials each configured model gets, inside the pipeline.

Requests go through the provider ``build_model_provider`` returns, as production sends them: OpenAI-family and
Claude routes over the shared model HTTP client, which a mock transport answers here, and Gemini and Ollama over
the client their config injects.
"""
# ruff: noqa: D103

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from google.oauth2.credentials import Credentials
from structlog.testing import capture_logs

from mindroom.config.main import Config
from mindroom.config.models import DebugConfig, ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.error_handling import is_model_safeguard_refusal
from mindroom.llm.errors import MALFORMED_TOOL_CALL, ModelSafeguardRefusal, ProviderError
from mindroom.llm.media import Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ToolSpec, TurnCompleted, UsageReported, complete
from mindroom.providers.anthropic.messages import ClaudeProvider
from mindroom.providers.gemini.adapter import GeminiProvider
from mindroom.providers.gemini.wire import MALFORMED_FUNCTION_CALL_GUIDANCE
from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT
from mindroom.providers.ollama_adapter import OllamaProvider
from mindroom.providers.openai.chat import ChatAdapter
from mindroom.providers.openai.responses import ResponsesAdapter
from mindroom.providers.pipeline.wrapped import WrappedProvider
from mindroom.providers.registry import build_model_provider
from mindroom.providers.synthetic_adapter import SyntheticProvider
from tests.providers.golden_support import (
    FIXTURE_ROOT,
    expected_parsed,
    expected_stop_reason,
    golden_request,
    load_golden,
    normalized_parsed,
    normalized_request,
)
from tests.providers.openai_family_support import encoded_response, write_cli_logins

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelProvider, StreamEvent

_PROVIDER_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "CEREBRAS_",
    "CLOUD_ML_",
    "DEEPSEEK_",
    "GEMINI_",
    "GOOGLE_",
    "GROQ_",
    "OLLAMA_",
    "OPENAI_",
    "OPENROUTER_",
)
_GOOGLE_TOKEN = "parity-google-token"  # noqa: S105 - synthetic SDK credential
_BAD_REQUEST = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad", "code": 400}}
_SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}}


class _Wire:
    """Records the requests providers send and answers each from a queue; the last answer repeats."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.replies: list[httpx.Response | Callable[[], httpx.Response]] = []
        self.transport = httpx.MockTransport(self._respond)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return reply if isinstance(reply, httpx.Response) else reply()

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(_PROVIDER_ENV_PREFIXES):
            monkeypatch.delenv(name)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> _Wire:
    """Answer requests sent through the shared model HTTP client, as the OpenAI-family and Claude routes send."""
    recorder = _Wire()
    monkeypatch.setattr(MODEL_HTTP_CLIENT, "send", httpx.AsyncClient(transport=recorder.transport).send)
    return recorder


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env={})


def _bare_adapter(provider: ModelProvider) -> ModelProvider:
    while isinstance(provider, WrappedProvider):
        provider = provider.inner
    return provider


def _sse(events: list[dict[str, Any]], *, named: bool = False) -> httpx.Response:
    lines = "".join(
        (f"event: {event['type']}\n" if named else "") + f"data: {json.dumps(event)}\n\n" for event in events
    )
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=lines.encode())


def _user(text: str = "Hi", **media: Any) -> Message:  # noqa: ANN401 - media fields
    return Message(role="user", content=text, **media)


async def _collect(provider: ModelProvider, request: ModelRequest, events: list[StreamEvent]) -> None:
    """Append each event of one request to ``events``, which keeps them when the request fails."""
    async for event in provider.stream(request):
        events.append(event)  # noqa: PERF401 - a comprehension would lose the events when the stream raises


def _texts(gemini_body: dict[str, Any]) -> list[str]:
    return [part["text"] for content in gemini_body["contents"] for part in content["parts"] if "text" in part]


# --- Dispatch and identity ----------------------------------------------------------------------------------


_BEDROCK_KEYS = {"aws_region": "us-east-1", "aws_access_key": "access", "aws_secret_key": "secret"}
# Every documented ``provider`` value, with its adapter and the persisted identity strings Agno's model had.
_ROUTES: dict[str, tuple[dict[str, Any], type, str, str]] = {
    "anthropic": ({"provider": "anthropic", "id": "claude-sonnet-5-5"}, ClaudeProvider, "Anthropic", "Anthropic"),
    "bedrock_claude": (
        {"provider": "bedrock_claude", "id": "anthropic.claude-sonnet-5-5", "extra_kwargs": _BEDROCK_KEYS},
        ClaudeProvider,
        "AwsBedrock",
        "AwsBedrock",
    ),
    "vertexai_claude": (
        {"provider": "vertexai_claude", "id": "claude-sonnet-5-5", "extra_kwargs": {"project_id": "p", "region": "r"}},
        ClaudeProvider,
        "VertexAI",
        "VertexAI",
    ),
    "azure": ({"provider": "azure", "id": "gpt-6-astra"}, ChatAdapter, "Azure", "Azure"),
    "openai-chat-completions": (
        {"provider": "openai", "id": "gpt-6-astra", "api": "chat_completions"},
        ChatAdapter,
        "OpenAI",
        "OpenAI Chat",
    ),
    "openai-responses": (
        {"provider": "openai", "id": "gpt-6-astra", "api": "responses"},
        ResponsesAdapter,
        "OpenAI",
        "OpenAI Responses",
    ),
    "openai-unset-searchable": (
        {"provider": "openai", "id": "gpt-6-astra"},
        ResponsesAdapter,
        "OpenAI",
        "OpenAI Responses",
    ),
    "openai-unset-custom-endpoint": (
        {"provider": "openai", "id": "qwen", "extra_kwargs": {"base_url": "http://localhost:4000/v1"}},
        ChatAdapter,
        "OpenAI",
        "OpenAI Chat",
    ),
    "codex": ({"provider": "codex", "id": "gpt-6.1-sol"}, ResponsesAdapter, "OpenAI Codex", "OpenAI Codex Responses"),
    "openai_codex": (
        {"provider": "openai_codex", "id": "gpt-6.1-sol"},
        ResponsesAdapter,
        "OpenAI Codex",
        "OpenAI Codex Responses",
    ),
    "kimi": ({"provider": "kimi", "id": "k3"}, ChatAdapter, "Kimi", "Kimi Chat"),
    "kimi_code": ({"provider": "kimi_code", "id": "k3"}, ChatAdapter, "Kimi", "Kimi Chat"),
    "google": ({"provider": "google", "id": "gemini-3.8-flash"}, GeminiProvider, "Google", "Google"),
    "gemini": ({"provider": "gemini", "id": "gemini-3.8-flash"}, GeminiProvider, "Google", "Google"),
    "google-vertexai": (
        {"provider": "google", "id": "gemini-3.8-flash", "extra_kwargs": {"vertexai": True, "project_id": "p"}},
        GeminiProvider,
        "Google",
        "Google",
    ),
    "ollama": ({"provider": "ollama", "id": "qwen3.8:27b"}, OllamaProvider, "Ollama", "Ollama"),
    "llama_cpp": ({"provider": "llama_cpp", "id": "qwen"}, ChatAdapter, "LlamaCpp", "LlamaCpp"),
    "llama_cpp-spelled-loosely": ({"provider": " Llama-CPP ", "id": "qwen"}, ChatAdapter, "LlamaCpp", "LlamaCpp"),
    "groq": ({"provider": "groq", "id": "openai/gpt-oss-120b"}, ChatAdapter, "Groq", "Groq"),
    "openrouter": ({"provider": "openrouter", "id": "z-ai/glm-5.3"}, ChatAdapter, "OpenRouter", "OpenRouter"),
    "cerebras": ({"provider": "cerebras", "id": "gpt-oss-120b"}, ChatAdapter, "Cerebras", "Cerebras"),
    "deepseek": ({"provider": "deepseek", "id": "deepseek-flash"}, ChatAdapter, "DeepSeek", "DeepSeek"),
    "zai": ({"provider": "zai", "id": "glm-5.3"}, ChatAdapter, "ZAI", "ZAI"),
    "synthetic": ({"provider": "synthetic", "id": "lorem-ipsum"}, SyntheticProvider, "Synthetic", "Synthetic"),
}


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_every_documented_provider_value_gets_its_adapter_and_identity(route: str, runtime_paths: RuntimePaths) -> None:
    """Dispatch picks the family adapter, and the identity keeps the provider strings stored runs and usage carry."""
    fields, adapter_type, provider_label, provider_name = _ROUTES[route]
    config = Config(models={"target": ModelConfig(**fields)})

    provider = build_model_provider(config, runtime_paths, "target")

    adapter = _bare_adapter(provider)
    assert type(adapter) is adapter_type
    assert (provider.identity.provider, provider.identity.provider_name) == (provider_label, provider_name)
    assert (provider.identity, provider.capabilities) == (adapter.identity, adapter.capabilities)


def test_unknown_models_and_providers_are_rejected(runtime_paths: RuntimePaths) -> None:
    config = Config(
        models={"b": ModelConfig(provider="openai", id="m"), "a": ModelConfig(provider="telepathy", id="m")},
    )

    with pytest.raises(ValueError, match=r"^Unknown model: missing\. Available models: a, b$"):
        build_model_provider(config, runtime_paths, "missing")
    with pytest.raises(ValueError, match=r"^Unsupported AI provider: telepathy$"):
        build_model_provider(config, runtime_paths, "a")


def test_the_model_in_use_is_logged_with_its_effective_id(runtime_paths: RuntimePaths) -> None:
    config = Config(models={"default": ModelConfig(provider="codex", id="openai-codex/gpt-6.1-sol")})

    with capture_logs() as logs:
        provider = build_model_provider(config, runtime_paths)

    assert [entry for entry in logs if entry["event"] == "Using AI model"] == [
        {
            "event": "Using AI model",
            "log_level": "info",
            "model": "default",
            "provider": "codex",
            "configured_id": "openai-codex/gpt-6.1-sol",
            "effective_id": "gpt-6.1-sol",
        },
    ]
    assert provider.identity.id == "gpt-6.1-sol"


# --- Credentials --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_fields", "dashboard_key", "expected_key"),
    [
        ({}, None, "sk-shared"),
        ({"api_key": " sk-config "}, None, "sk-config"),
        ({"extra_kwargs": {"api_key": " sk-config "}}, None, "sk-config"),
        ({"api_key": "  ", "extra_kwargs": {"api_key": ""}}, None, "sk-shared"),
        ({"api_key": "sk-config"}, "sk-dashboard", "sk-dashboard"),
        ({"extra_kwargs": {"api_key": "sk-config"}}, "sk-dashboard", "sk-dashboard"),
    ],
    ids=[
        "shared",
        "api-key",
        "extra-kwargs-api-key",
        "blank-is-unset",
        "dashboard-beats-api-key",
        "dashboard-beats-extra",
    ],
)
async def test_api_key_precedence(
    model_fields: dict[str, Any],
    dashboard_key: str | None,
    expected_key: str,
    runtime_paths: RuntimePaths,
    wire: _Wire,
) -> None:
    """The key the dashboard saved for the model beats a configured key, which beats the provider's shared key."""
    credentials = get_runtime_shared_credentials_manager(runtime_paths)
    credentials.save_credentials("openai", {"api_key": "sk-shared"})
    if dashboard_key is not None:
        credentials.save_credentials("model:target", {"api_key": dashboard_key})
    config = Config(models={"target": ModelConfig(provider="openai", api="chat_completions", id="m", **model_fields)})
    wire.replies.append(httpx.Response(400, json=_BAD_REQUEST))

    with pytest.raises(ProviderError):
        await complete(build_model_provider(config, runtime_paths, "target"), ModelRequest(messages=[_user()]))

    assert wire.requests[0].headers["authorization"] == f"Bearer {expected_key}"


# The header each keyed family sends its key in, and how it formats it.
_KEY_HEADERS: dict[str, tuple[dict[str, Any], str, str]] = {
    "anthropic": ({"provider": "anthropic", "id": "claude-sonnet-5-5"}, "x-api-key", "{key}"),
    "azure": (
        {"provider": "azure", "id": "gpt-6-astra", "extra_kwargs": {"azure_endpoint": "https://r.openai.azure.com"}},
        "api-key",
        "{key}",
    ),
    "google": ({"provider": "google", "id": "gemini-3.8-flash"}, "x-goog-api-key", "{key}"),
    "ollama": (
        {"provider": "ollama", "id": "qwen3.8:27b", "host": "http://ollama.test:11434"},
        "authorization",
        "Bearer {key}",
    ),
    "openai-responses": (
        {"provider": "openai", "api": "responses", "id": "gpt-6-astra"},
        "authorization",
        "Bearer {key}",
    ),
    "openrouter": ({"provider": "openrouter", "id": "z-ai/glm-5.3"}, "authorization", "Bearer {key}"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(_KEY_HEADERS))
async def test_the_dashboard_key_reaches_every_keyed_family(
    family: str,
    runtime_paths: RuntimePaths,
    wire: _Wire,
) -> None:
    """A key the dashboard saved for the model reaches the wire on every family that sends one."""
    fields, header, template = _KEY_HEADERS[family]
    fields = deepcopy(fields)
    extra_kwargs = fields.setdefault("extra_kwargs", {})
    if family == "google":
        extra_kwargs["client_params"] = {
            "http_options": {"httpx_async_client": httpx.AsyncClient(transport=wire.transport)},
        }
    if family == "ollama":
        extra_kwargs["client_params"] = {"transport": wire.transport}
    credentials = get_runtime_shared_credentials_manager(runtime_paths)
    credentials.save_credentials(family.split("-", maxsplit=1)[0], {"api_key": "sk-shared"})
    credentials.save_credentials("model:target", {"api_key": "sk-dashboard"})
    config = Config(models={"target": ModelConfig(api_key="sk-config", **fields)})
    wire.replies.append(httpx.Response(400, json=_BAD_REQUEST))

    with pytest.raises(ProviderError):
        await complete(build_model_provider(config, runtime_paths, "target"), ModelRequest(messages=[_user()]))

    assert wire.requests[0].headers[header] == template.format(key="sk-dashboard")


# --- One golden per family, through the registry ------------------------------------------------------------


_GOLDEN_FAMILIES = sorted(path.name for path in FIXTURE_ROOT.iterdir() if path.is_dir())


def _golden_model_config(golden: dict[str, Any], root: Path, wire: _Wire) -> ModelConfig:
    """Return the golden's model config with what JSON cannot carry: CLI logins, Google credentials, transports."""
    fields = deepcopy(golden["input"]["model_config"])
    extra_kwargs = fields.setdefault("extra_kwargs", {})
    placeholders = write_cli_logins(root)
    for key, value in list(extra_kwargs.items()):
        if isinstance(value, str) and value in placeholders:
            extra_kwargs[key] = placeholders[value]
    if fields["provider"] == "vertexai_claude":
        extra_kwargs["client_params"] = {"credentials": Credentials(token=_GOOGLE_TOKEN)}
    if fields["provider"] == "google":
        extra_kwargs["client_params"] = {
            "http_options": {"httpx_async_client": httpx.AsyncClient(transport=wire.transport)},
        }
        if extra_kwargs.get("vertexai"):
            extra_kwargs["credentials"] = Credentials(token=_GOOGLE_TOKEN)
    if fields["provider"] == "ollama":
        extra_kwargs["client_params"] = {"transport": wire.transport}
    return ModelConfig(**fields)


def _golden_reply(response_raw: dict[str, Any]) -> httpx.Response:
    if "ndjson" in response_raw:
        content = b"".join(json.dumps(event).encode() + b"\n" for event in response_raw["ndjson"])
        return httpx.Response(response_raw["status"], headers=response_raw["headers"], content=content)
    return encoded_response(response_raw)


async def _replay(
    golden: dict[str, Any],
    config: Config,
    runtime_paths: RuntimePaths,
    wire: _Wire,
) -> list[StreamEvent]:
    wire.replies.append(lambda: _golden_reply(golden["response_raw"]))
    provider = build_model_provider(config, runtime_paths)
    return [event async for event in provider.stream(golden_request(golden["input"]))]


@pytest.mark.asyncio
@pytest.mark.parametrize("family", _GOLDEN_FAMILIES)
async def test_a_golden_of_every_family_replays_through_the_registry(
    family: str,
    tmp_path: Path,
    runtime_paths: RuntimePaths,
    wire: _Wire,
) -> None:
    """The registry-built provider sends each family's plain turn byte for byte and parses its reply as recorded."""
    golden = load_golden(FIXTURE_ROOT / family / "plain_turn.json")
    config = Config(models={"default": _golden_model_config(golden, tmp_path, wire)})

    events = await _replay(golden, config, runtime_paths, wire)

    assert len(wire.requests) == 1
    await wire.requests[0].aread()
    assert normalized_request(wire.requests[0]) == golden["request"]
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    assert normalized_parsed(completed.message) == expected_parsed(golden)
    assert completed.stop_reason == expected_stop_reason(golden)


@pytest.mark.asyncio
async def test_codex_requests_carry_the_configured_default_instructions(
    tmp_path: Path,
    runtime_paths: RuntimePaths,
    wire: _Wire,
) -> None:
    golden = load_golden(FIXTURE_ROOT / "codex" / "plain_turn.json")
    model = _golden_model_config(golden, tmp_path, wire)
    config = Config(models={"default": model}, prompts={"CODEX_DEFAULT_INSTRUCTIONS": "Answer like a travel agent."})

    await _replay(golden, config, runtime_paths, wire)

    expected = deepcopy(golden["request"]["body"])
    expected["instructions"] = "Answer like a travel agent."
    assert wire.body() == expected


@pytest.mark.asyncio
async def test_requests_are_logged_under_the_storage_root_with_the_model_settings(
    runtime_paths: RuntimePaths,
    wire: _Wire,
) -> None:
    """With ``debug.log_llm_requests``, each request and its usage land in the day's log, keyed by the model name."""
    golden = load_golden(FIXTURE_ROOT / "openai_chat" / "plain_turn.json")
    model = ModelConfig(
        provider="openai",
        api="chat_completions",
        id="gpt-6-astra",
        api_key="sk-test",
        extra_kwargs={"temperature": 0.3},
    )
    config = Config(models={"default": model}, debug=DebugConfig(log_llm_requests=True))

    await _replay(golden, config, runtime_paths, wire)

    (log_file,) = (runtime_paths.storage_root / "logs" / "llm_requests").glob("llm-requests-*.jsonl")
    request_record, response_record = (json.loads(line) for line in log_file.read_text().splitlines())
    assert set(request_record) == {
        "timestamp",
        "request_log_id",
        "agent_id",
        "model_id",
        "system_prompt",
        "messages",
        "message_count",
        "tools",
        "tool_count",
        "model_params",
    }
    assert (request_record["agent_id"], request_record["model_id"]) == ("default", "gpt-6-astra")
    assert request_record["model_params"]["temperature"] == 0.3
    assert "api_key" not in request_record["model_params"]
    assert (response_record["record"], response_record["agent_id"]) == ("response", "default")


# --- The refusal and malformed tool-call policies, on real routes -------------------------------------------


def _claude_refusal() -> httpx.Response:
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 40, "output_tokens": 1},
    }
    events = [
        {"type": "message_start", "message": message},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "refusal", "stop_sequence": None},
            "usage": {"output_tokens": 9},
        },
        {"type": "message_stop"},
    ]
    return _sse(events, named=True)


@pytest.mark.asyncio
async def test_a_refusal_fails_the_request_once_and_its_usage_is_counted(
    runtime_paths: RuntimePaths,
    wire: _Wire,
) -> None:
    """A refused Claude turn raises the typed refusal after its usage; neither a retry nor a media-free resend follows."""
    config = Config(models={"claude": ModelConfig(provider="anthropic", id="claude-sonnet-5-5", api_key="sk-test")})
    wire.replies.append(_claude_refusal)
    image = Image(content=b"\x89PNG\r\n\x1a\n0000", mime_type="image/png")
    provider = build_model_provider(config, runtime_paths, "claude")
    request = ModelRequest(messages=[_user("Describe this", images=[image])])
    events: list[StreamEvent] = []

    with capture_logs() as logs, pytest.raises(ModelSafeguardRefusal) as raised:
        await _collect(provider, request, events)

    assert len(wire.requests) == 1
    assert is_model_safeguard_refusal(raised.value)
    assert (raised.value.provider, raised.value.retryable) == ("Anthropic", False)
    assert not any(isinstance(event, TurnCompleted) for event in events)
    final_usage = [event.usage for event in events if isinstance(event, UsageReported) and event.final]
    assert [(usage.input_tokens, usage.output_tokens) for usage in final_usage] == [(40, 9)]
    (usage_line,) = [entry for entry in logs if entry["event"] == "LLM usage"]
    assert (usage_line["input_tokens"], usage_line["output_tokens"]) == (40, 9)


def _gemini_malformed() -> httpx.Response:
    candidate = {"content": {"role": "model", "parts": []}, "finishReason": "MALFORMED_FUNCTION_CALL"}
    return _sse([{"candidates": [candidate]}])


def _gemini_text() -> httpx.Response:
    candidate = {"content": {"role": "model", "parts": [{"text": "Sunny."}]}, "finishReason": "STOP", "index": 0}
    return _sse([{"candidates": [candidate], "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 9}}])


def _gemini_config(wire: _Wire) -> Config:
    client = httpx.AsyncClient(transport=wire.transport)
    extra_kwargs = {"client_params": {"http_options": {"httpx_async_client": client}}}
    model = ModelConfig(provider="google", id="gemini-3.8-flash", api_key="sk-test", extra_kwargs=extra_kwargs)
    return Config(models={"gemini": model})


@pytest.mark.asyncio
async def test_a_malformed_tool_call_is_answered_once_with_guidance_that_is_not_stored(
    runtime_paths: RuntimePaths,
) -> None:
    wire = _Wire()
    wire.replies.extend([_gemini_malformed, _gemini_text])
    provider = build_model_provider(_gemini_config(wire), runtime_paths, "gemini")
    messages = [_user("Weather in Paris?")]
    tools = [ToolSpec(name="get_weather", description="Weather.", parameters=_SCHEMA)]

    turn = await complete(provider, ModelRequest(messages=messages, tools=tools))

    assert turn.content == "Sunny."
    assert len(wire.requests) == 2
    assert MALFORMED_FUNCTION_CALL_GUIDANCE not in _texts(wire.body(0))
    assert _texts(wire.body(1)) == [*_texts(wire.body(0)), MALFORMED_FUNCTION_CALL_GUIDANCE]
    assert [message.content for message in messages] == ["Weather in Paris?"]


@pytest.mark.asyncio
async def test_a_second_malformed_tool_call_fails_the_request(runtime_paths: RuntimePaths) -> None:
    wire = _Wire()
    wire.replies.append(_gemini_malformed)
    provider = build_model_provider(_gemini_config(wire), runtime_paths, "gemini")

    with pytest.raises(ProviderError) as raised:
        await complete(provider, ModelRequest(messages=[_user("Weather in Paris?")]))

    assert raised.value.code == MALFORMED_TOOL_CALL
    assert [_texts(wire.body(index)).count(MALFORMED_FUNCTION_CALL_GUIDANCE) for index in range(2)] == [0, 1]
    assert len(wire.requests) == 2
