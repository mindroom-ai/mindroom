"""A model's ``extra_kwargs`` and the runtime env shape its route's settings and the Claude prompt-cache ladder."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import yaml
from structlog.testing import capture_logs

from mindroom.config import legacy_fields
from mindroom.config.main import Config
from mindroom.config.models import DebugConfig, ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.hooks import render_transient_context
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, complete
from mindroom.providers.anthropic import messages as claude_messages
from mindroom.providers.anthropic.messages import build_provider as build_claude_provider
from mindroom.providers.anthropic.prompt_cache import native_tool_search_supported, prepare_request_body
from mindroom.providers.anthropic.settings import ACCEPTED_KEYS, ClaudeSettings
from mindroom.providers.cache_ladder import MAX_CACHE_MARKERS, has_cache_marker, prompt_cache_control
from mindroom.providers.extra_kwargs import settings_params
from mindroom.providers.gemini.adapter import GeminiSettings
from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT
from mindroom.providers.ollama_adapter import OllamaSettings
from mindroom.providers.openai.build import OPENAI_FAMILY_PROVIDERS, ProviderRuntime
from mindroom.providers.openai.build import build_provider as build_openai_provider
from mindroom.providers.openai.settings import CODEX_FIELDS, ChatSettings, ResponsesSettings
from mindroom.providers.registry import build_model_provider
from mindroom.providers.synthetic_adapter import SyntheticSettings
from mindroom.redaction import REDACTED
from mindroom.startup_errors import PermanentStartupError
from tests.providers.anthropic_family_support import claude_turn, json_reply, route_provider
from tests.providers.golden_support import Wire

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.providers.anthropic.settings import ClaudeRouteKind

_REQUEST_PARAMS = {"provider": {"order": ["Cerebras"], "allow_fallbacks": False}}


def _runtime_paths(root: Path, **process_env: str) -> RuntimePaths:
    root.mkdir(parents=True, exist_ok=True)
    return resolve_runtime_paths(
        config_path=root / "config.yaml",
        storage_path=root / "mindroom_data",
        process_env=process_env,
    )


# --- Config -------------------------------------------------------------------------------------------------


def test_model_config_keeps_extra_kwargs_as_authored() -> None:
    """``extra_kwargs`` reach the route's settings parser unchanged."""
    model_config = ModelConfig(
        provider="openrouter",
        id="openai/gpt-6-astra",
        extra_kwargs={"request_params": _REQUEST_PARAMS},
    )

    assert model_config.extra_kwargs == {"request_params": _REQUEST_PARAMS}


def test_extra_kwargs_load_from_yaml(tmp_path: Path) -> None:
    """Nested ``extra_kwargs`` in ``config.yaml`` load as the dicts and values authored."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "models": {
                    "test_model": {
                        "provider": "openrouter",
                        "id": "deepseek/deepseek-v4.1-flash",
                        "extra_kwargs": {"request_params": _REQUEST_PARAMS, "temperature": 0.7, "max_tokens": 4096},
                    },
                },
                "router": {"model": "test_model"},
                "agents": {},
            },
        ),
        encoding="utf-8",
    )

    config = Config(**yaml.safe_load(config_file.read_text(encoding="utf-8")))

    assert config.models["test_model"].extra_kwargs == {
        "request_params": _REQUEST_PARAMS,
        "temperature": 0.7,
        "max_tokens": 4096,
    }


# --- Model params ---------------------------------------------------------------------------------------------

# Each route family's settings type, one request setting it records, and every field holding a credential, a
# client, or headers or a query string that can carry one.
_ROUTE_SETTINGS: list[tuple[type[Any], dict[str, Any], frozenset[str]]] = [
    (
        ChatSettings,
        {"temperature": 0.3},
        frozenset(
            {
                "api_key",
                "azure_ad_token",
                "azure_ad_token_provider",
                "client_params",
                "default_headers",
                "default_query",
                "extra_headers",
                "extra_query",
            },
        ),
    ),
    (
        ResponsesSettings,
        {"temperature": 0.3},
        frozenset({"api_key", "client_params", "default_headers", "default_query", "extra_headers", "extra_query"}),
    ),
    (
        ClaudeSettings,
        {"temperature": 0.3},
        frozenset(
            {
                "api_key",
                "auth_token",
                "aws_access_key",
                "aws_secret_key",
                "aws_session_token",
                "client_params",
                "default_headers",
                "session",
            },
        ),
    ),
    (GeminiSettings, {"temperature": 0.3}, frozenset({"api_key", "client_params", "credentials"})),
    (OllamaSettings, {"options": {"num_ctx": 8192}}, frozenset({"api_key", "client_params"})),
    (SyntheticSettings, {"seed": 7}, frozenset()),
]


@pytest.mark.parametrize(
    ("settings_type", "request_settings", "credential_fields"),
    _ROUTE_SETTINGS,
    ids=[settings_type.__name__ for settings_type, _, _ in _ROUTE_SETTINGS],
)
def test_model_params_record_no_credential_of_any_route_family(
    settings_type: type[Any],
    request_settings: dict[str, Any],
    credential_fields: frozenset[str],
) -> None:
    """Run metadata and the request log record a route's request settings, never a credential, even a JSON-safe one."""
    credentials = {name: f"secret-{name}" for name in credential_fields}

    params = settings_params(settings_type(**request_settings, **credentials))

    assert params == settings_params(settings_type(**request_settings))
    assert params.items() >= request_settings.items()


@pytest.mark.asyncio
async def test_model_params_mask_credentials_nested_in_request_params_and_extra_body(
    tmp_path: Path,
    chat_wire: Wire,
) -> None:
    """A credential authored inside ``request_params`` or ``extra_body`` is masked in run metadata as in the request log."""
    log_dir = tmp_path / "request-logs"
    config = Config(
        models={
            "default": ModelConfig(
                provider="openrouter",
                id="deepseek/deepseek-v4.1-flash",
                extra_kwargs={
                    "temperature": 0.2,
                    "request_params": {
                        "extra_headers": {"Authorization": "Bearer nested-header-secret", "X-Title": "MindRoom"},
                        **_REQUEST_PARAMS,
                    },
                    "extra_body": {"api_key": "nested-body-secret", "transforms": ["middle-out"]},
                },
            ),
        },
        debug=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(log_dir)),
    )
    provider = build_model_provider(config, _runtime_paths(tmp_path, OPENROUTER_API_KEY="openrouter-key"))

    await complete(provider, ModelRequest(messages=[Message(role="user", content="Hi")], stream=False))

    [request] = chat_wire.requests
    assert request.headers["authorization"] == "Bearer nested-header-secret"
    assert json.loads(request.content)["api_key"] == "nested-body-secret"
    assert provider.model_params == {
        "temperature": 0.2,
        "request_params": {"extra_headers": {"Authorization": REDACTED, "X-Title": "MindRoom"}, **_REQUEST_PARAMS},
        "extra_body": {"api_key": REDACTED, "transforms": ["middle-out"]},
        "strict_output": True,
        "cache_system_prompt": True,
        "extended_cache_time": True,
    }
    (log_file,) = log_dir.glob("llm-requests-*.jsonl")
    [request_record] = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert request_record["model_params"] == provider.model_params


# --- The Claude prompt-cache ladder ---------------------------------------------------------------------------

_ONE_HOUR = {"type": "ephemeral", "ttl": "1h"}
_FIVE_MINUTES = {"type": "ephemeral"}


def _prepared(
    body: dict[str, Any],
    *,
    deferred: frozenset[str] = frozenset(),
    cache: bool = True,
    extended: bool = False,
    route: ClaudeRouteKind = "anthropic",
) -> dict[str, Any]:
    return prepare_request_body(
        body,
        route=route,
        deferred_tool_names=deferred,
        tool_choice=None,
        extended_cache_time=extended,
        cache_system_prompt=cache,
    )


def _marker_count(body: dict[str, Any]) -> int:
    blocks = [*(body.get("system") or []), *(body.get("tools") or [])]
    blocks += [block for message in body.get("messages") or [] for block in message["content"]]
    return sum(1 for block in blocks if has_cache_marker(block))


def _tool(name: str) -> dict[str, Any]:
    return {"name": name, "description": f"{name} description", "input_schema": {"type": "object"}}


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def test_prompt_cache_control_selects_the_one_hour_lifetime_only_when_extended() -> None:
    """``extended_cache_time`` selects the 1h TTL; the default sends no ttl field."""
    assert prompt_cache_control() == _FIVE_MINUTES
    assert prompt_cache_control(extended_cache_time=True) == _ONE_HOUR


def test_the_ladder_marks_the_newest_tool_result_a_prior_boundary_and_the_last_tool() -> None:
    """Every request of a tool loop extends the previous one's newest breakpoint, within the four-marker limit."""
    body = {
        "system": [{"type": "text", "text": "System prompt", "cache_control": _ONE_HOUR}],
        "tools": [_tool("demo_tool")],
        "messages": [
            {"role": "user", "content": [_text("Use the tool")]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "demo_tool", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
        ],
    }

    prepared = _prepared(body, extended=True)

    assert prepared["messages"][-1]["content"][0]["cache_control"] == _ONE_HOUR
    assert prepared["messages"][0]["content"][-1]["cache_control"] == _ONE_HOUR
    assert prepared["tools"][-1]["cache_control"] == _ONE_HOUR
    assert _marker_count(prepared) == MAX_CACHE_MARKERS


@pytest.mark.asyncio
async def test_transient_context_takes_no_marker_so_the_next_turn_reuses_the_prompt_breakpoint(tmp_path: Path) -> None:
    """Per-request context after the prompt stays unmarked, so the next turn replays the same cached prefix."""
    transient = render_transient_context(
        ("<mindroom_message_context>Current location: Amsterdam</mindroom_message_context>",),
    )
    wire = Wire(json_reply([{"type": "text", "text": "A1"}]))
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs={"extended_cache_time": False})
    system = Message(role="system", content="Stable system")
    for history in ([], [Message(role="user", content="Q1"), Message(role="assistant", content="A1")]):
        prompt = Message(role="user", content="Q2" if history else "Q1")
        context = Message(role="user", content=transient)
        await claude_turn(provider, ModelRequest(messages=[system, *history, prompt, context], stream=False))

    current = wire.body(0)["messages"][0]["content"]
    assert current[0]["cache_control"] == _FIVE_MINUTES
    assert "cache_control" not in current[1]
    second = wire.body(1)["messages"]
    assert second[0]["content"] == current[:1]
    assert second[-1]["content"][0]["cache_control"] == _FIVE_MINUTES
    assert "cache_control" not in second[-1]["content"][1]


def test_an_existing_message_marker_uses_the_budget_once() -> None:
    """A marker already on a message occupies a rung, so one new rung and the tools marker fill the limit."""
    body = {
        "system": [{"type": "text", "text": "S", "cache_control": _FIVE_MINUTES}],
        "tools": [_tool("demo_tool")],
        "messages": [
            {"role": "user", "content": [_text("m1")]},
            {"role": "user", "content": [{"type": "text", "text": "m2", "cache_control": _FIVE_MINUTES}]},
            {"role": "user", "content": [_text("m3")]},
        ],
    }

    prepared = _prepared(body)

    assert prepared["messages"][-1]["content"][0]["cache_control"] == _FIVE_MINUTES
    assert "cache_control" not in prepared["messages"][0]["content"][0]
    assert prepared["tools"][-1]["cache_control"] == _FIVE_MINUTES
    assert _marker_count(prepared) == MAX_CACHE_MARKERS


def test_a_request_already_at_the_marker_limit_passes_through() -> None:
    """Four authored markers leave no budget, so the ladder adds none."""
    marked = {"type": "text", "text": "x", "cache_control": _FIVE_MINUTES}
    body = {
        "system": [dict(marked)],
        "tools": [dict(marked)],
        "messages": [
            {"role": "user", "content": [dict(marked)]},
            {"role": "user", "content": [dict(marked), _text("tail")]},
        ],
    }

    assert _prepared(body) == body


def test_thinking_and_empty_text_never_carry_a_marker() -> None:
    """The ladder skips blocks the API cannot mark and marks the nearest earlier text instead."""
    body = {
        "messages": [
            {"role": "user", "content": [_text("earlier turn")]},
            {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "reasoning", "signature": "sig"}, _text("")],
            },
        ],
    }

    prepared = _prepared(body)

    assert "cache_control" not in json.dumps(prepared["messages"][1])
    assert prepared["messages"][0]["content"][0]["cache_control"] == _FIVE_MINUTES


def test_the_ladder_never_mutates_the_request_it_marks() -> None:
    """Markers are added to copies, never to stored messages or reusable tool definitions."""
    body = {"messages": [{"role": "user", "content": [_text("hello")]}], "tools": [_tool("demo_tool")]}
    original = deepcopy(body)

    _prepared(body)

    assert body == original


def test_a_disabled_prompt_cache_sends_no_markers() -> None:
    """``cache_system_prompt: false`` leaves the request unmarked."""
    body = {"system": [_text("System prompt")], "messages": [{"role": "user", "content": [_text("hi")]}]}

    assert _marker_count(_prepared(body, cache=False)) == 0


# --- Deferred tool search -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "model_id", "expected"),
    [
        ("anthropic", "claude-opus-5", True),
        ("anthropic", "claude-sonnet-5", True),
        ("Anthropic", "claude-sonnet-4-5-20250929", True),
        ("vertexai_claude", "claude-haiku-4-5@20251001", True),
        # Unreleased Claude models default to the native path (denylist gating).
        ("anthropic", "claude-opus-4-9", True),
        ("anthropic", "claude-fable-6", True),
        ("anthropic", "claude-opus-4-1", False),
        ("anthropic", "claude-opus-4-20250514", False),
        ("anthropic", "claude-3-5-sonnet-20241022", False),
        ("vertexai_claude", "claude-sonnet-4@20250514", False),
        ("openai", "gpt-6-astra", False),
        ("bedrock_claude", "anthropic.claude-opus-5", False),
    ],
)
def test_native_tool_search_gating(provider: str, model_id: str, *, expected: bool) -> None:
    """Claude routes with server-side tool search qualify unless the model predates it."""
    assert native_tool_search_supported(provider, model_id) is expected


def test_deferred_tools_ship_tagged_after_the_search_tool_and_the_other_tools() -> None:
    """The search tool comes first, then the other tools, then the deferred ones by name, none of them marked."""
    tools = [_tool("always_tool"), {**_tool("zeta_tool"), "cache_control": _FIVE_MINUTES}, _tool("alpha_tool")]

    prepared = _prepared({"tools": tools, "messages": []}, deferred=frozenset({"zeta_tool", "alpha_tool"}))

    sent = prepared["tools"]
    assert [tool.get("name") for tool in sent] == ["tool_search_tool_regex", "always_tool", "alpha_tool", "zeta_tool"]
    assert sent[0] == {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}
    # Deferred tools may not carry cache_control, so the ladder's marker lands on the last ordinary tool.
    assert sent[1]["cache_control"] == _FIVE_MINUTES
    for deferred in sent[2:]:
        assert deferred["defer_loading"] is True
        assert "cache_control" not in deferred


def test_an_all_deferred_tool_list_gets_no_tools_marker() -> None:
    """Neither the search tool nor a deferred tool may take the marker, so the system breakpoint covers the tools."""
    prepared = _prepared({"tools": [_tool("alpha_tool")], "messages": []}, deferred=frozenset({"alpha_tool"}))

    assert [tool["name"] for tool in prepared["tools"]] == ["tool_search_tool_regex", "alpha_tool"]
    assert prepared["tools"][1]["defer_loading"] is True
    assert _marker_count({"tools": prepared["tools"]}) == 0


def test_deferred_tools_are_tagged_with_the_prompt_cache_disabled() -> None:
    """Deferring tools does not depend on the cache ladder."""
    prepared = _prepared(
        {"tools": [_tool("alpha_tool")], "messages": []},
        deferred=frozenset({"alpha_tool"}),
        cache=False,
    )

    assert prepared["tools"][1]["defer_loading"] is True
    assert _marker_count(prepared) == 0


def test_the_search_tool_is_added_only_when_a_deferred_tool_is_offered() -> None:
    """A deferred name the request does not offer leaves the tools as they are."""
    prepared = _prepared({"tools": [_tool("always_tool")], "messages": []}, deferred=frozenset({"other_tool"}))

    assert [tool["name"] for tool in prepared["tools"]] == ["always_tool"]


# --- Replayed tool search -------------------------------------------------------------------------------------

_SEARCH_USE = {
    "type": "server_tool_use",
    "id": "srvtoolu_01ABC",
    "name": "tool_search_tool_regex",
    "input": {"pattern": "weather"},
}
_ORPHAN_SEARCH_USE = {**_SEARCH_USE, "id": "srvtoolu_01ORPHAN"}
_OTHER_SERVER_TOOL_USE = {
    "type": "server_tool_use",
    "id": "srvtoolu_01OTHER",
    "name": "web_search",
    "input": {"query": "weather"},
}
_SEARCH_RESULT = {
    "type": "tool_search_tool_result",
    "tool_use_id": "srvtoolu_01ABC",
    "content": {
        "type": "tool_search_tool_search_result",
        "tool_references": [{"type": "tool_reference", "tool_name": "get_weather"}],
    },
}
_EMPTIED_SEARCH_RESULT = {
    **_SEARCH_RESULT,
    "content": {"type": "tool_search_tool_search_result", "tool_references": []},
}
# The shape the response capture persists: response-only fields the request schema rejects ride along.
_DIRTY_SEARCH_RESULT = {**_SEARCH_RESULT, "citations": None, "parsed_output": None, "text": "Found 1 tool"}


def _replayed(*blocks: dict[str, Any], tools: list[str] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"messages": [{"role": "assistant", "content": [dict(block) for block in blocks]}]}
    if tools is not None:
        body["tools"] = [_tool(name) for name in tools]
    return body


def _replayed_blocks(body: dict[str, Any]) -> list[dict[str, Any]]:
    return _prepared(body, cache=False)["messages"][0]["content"]


def test_replayed_search_results_carry_only_request_fields() -> None:
    """Response-only fields stored with a search result are dropped, and the stored history keeps them."""
    body = _replayed(_SEARCH_USE, _DIRTY_SEARCH_RESULT, _text("found it"), tools=["get_weather"])

    assert _replayed_blocks(body) == [_SEARCH_USE, _SEARCH_RESULT, _text("found it")]
    assert "citations" in body["messages"][0]["content"][1]


def test_only_orphaned_search_uses_are_removed() -> None:
    """A regex search without its result in the same turn goes; other server tools and paired searches stay."""
    body = _replayed(
        _ORPHAN_SEARCH_USE,
        _OTHER_SERVER_TOOL_USE,
        _SEARCH_USE,
        _DIRTY_SEARCH_RESULT,
        _text("found it"),
        tools=["get_weather"],
    )

    assert _replayed_blocks(body) == [_OTHER_SERVER_TOOL_USE, _SEARCH_USE, _SEARCH_RESULT, _text("found it")]
    assert body["messages"][0]["content"][0] == _ORPHAN_SEARCH_USE


def test_references_to_tools_the_request_no_longer_offers_are_dropped() -> None:
    """A replayed search result keeps only references to tools this request still offers."""
    result = {
        **_SEARCH_RESULT,
        "content": {
            "tool_references": [
                {"type": "tool_reference", "tool_name": "get_weather"},
                {"type": "tool_reference", "tool_name": "retired_weather"},
                {"type": "tool_reference"},
                "malformed-reference",
            ],
        },
    }
    body = _replayed(_SEARCH_USE, result, tools=["get_weather"])

    assert _replayed_blocks(body) == [
        _SEARCH_USE,
        {
            "type": "tool_search_tool_result",
            "tool_use_id": "srvtoolu_01ABC",
            "content": {"tool_references": [{"type": "tool_reference", "tool_name": "get_weather"}]},
        },
    ]
    assert len(body["messages"][0]["content"][1]["content"]["tool_references"]) == 4


@pytest.mark.parametrize("tools", [["other_tool"], None], ids=["unavailable", "no-tools"])
def test_a_search_whose_tools_are_all_gone_keeps_its_pair_with_an_empty_result(tools: list[str] | None) -> None:
    """Without any referenced tool on the request, the search result is emptied rather than removed."""
    body = _replayed(_OTHER_SERVER_TOOL_USE, _SEARCH_USE, _SEARCH_RESULT, tools=tools)

    assert _replayed_blocks(body) == [_OTHER_SERVER_TOOL_USE, _SEARCH_USE, _EMPTIED_SEARCH_RESULT]
    assert body["messages"][0]["content"][2] == _SEARCH_RESULT


def test_a_replay_whose_references_are_available_is_unchanged() -> None:
    """A clean replay keeps the stored blocks exactly."""
    body = _replayed(_SEARCH_USE, _SEARCH_RESULT, _OTHER_SERVER_TOOL_USE, tools=["get_weather"])

    assert _replayed_blocks(body) == [_SEARCH_USE, _SEARCH_RESULT, _OTHER_SERVER_TOOL_USE]


@pytest.mark.parametrize("available", ["get_weather", "bridge_call_tool"], ids=["matched-nothing", "all-gone"])
def test_a_search_between_signed_thinking_blocks_always_stays(available: str) -> None:
    """Removing the search would make the tool loop's two thinking blocks adjacent, which the API rejects."""
    before = {"type": "thinking", "thinking": "", "signature": "sig-before-search"}
    after = {"type": "thinking", "thinking": "", "signature": "sig-after-search"}
    result = _EMPTIED_SEARCH_RESULT if available == "get_weather" else _SEARCH_RESULT
    body = {
        "tools": [_tool(available)],
        "messages": [
            {"role": "user", "content": [_text("What is the weather?")]},
            {
                "role": "assistant",
                "content": [
                    dict(before),
                    dict(_SEARCH_USE),
                    deepcopy(result),
                    dict(after),
                    {"type": "tool_use", "id": "toolu_01", "name": "get_weather", "input": {}},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "sunny"}]},
        ],
    }

    content = _prepared(body, cache=False)["messages"][1]["content"]

    assert [block["type"] for block in content] == [
        "thinking",
        "server_tool_use",
        "tool_search_tool_result",
        "thinking",
        "tool_use",
    ]
    assert (content[0], content[3]) == (before, after)
    assert content[2]["content"]["tool_references"] == []


# --- Route settings from the runtime env ----------------------------------------------------------------------


def _vertex_config() -> ModelConfig:
    return ModelConfig(
        provider="vertexai_claude",
        id="claude-sonnet-5",
        extra_kwargs={"project_id": "demo-project", "region": "us-central1"},
    )


@pytest.mark.parametrize(
    ("adc_type", "loader"),
    [
        ("authorized_user", "google.oauth2.credentials.Credentials.from_authorized_user_file"),
        ("service_account", "google.oauth2.service_account.Credentials.from_service_account_file"),
    ],
)
def test_vertex_claude_loads_the_runtime_application_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adc_type: str,
    loader: str,
) -> None:
    """A runtime ``GOOGLE_APPLICATION_CREDENTIALS`` file becomes the client's credentials without google.auth."""
    credentials_path = tmp_path / "google-credentials.json"
    credentials_path.write_text(json.dumps({"type": adc_type}), encoding="utf-8")
    fake_credentials = object()

    def load(path: str, *, scopes: list[str]) -> object:
        assert Path(path).resolve() == credentials_path.resolve()
        assert scopes == ["https://www.googleapis.com/auth/cloud-platform"]
        return fake_credentials

    def load_through_google_auth(*_args: object, **_kwargs: object) -> object:
        msg = "google.auth should not be used for this credential type"
        raise AssertionError(msg)

    monkeypatch.setattr(loader, load)
    monkeypatch.setattr("google.auth.load_credentials_from_file", load_through_google_auth)
    runtime_paths = _runtime_paths(tmp_path, GOOGLE_APPLICATION_CREDENTIALS=str(credentials_path))

    provider = build_claude_provider(_vertex_config(), runtime_paths)

    assert provider.settings.client_params == {"credentials": fake_credentials}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "GOOGLE_APPLICATION_CREDENTIALS points to a file that does not exist"),
        ('{"type":"authorized_user"}\n', "Failed to load GOOGLE_APPLICATION_CREDENTIALS"),
    ],
    ids=["missing", "invalid"],
)
def test_vertex_claude_rejects_unusable_runtime_application_credentials(
    tmp_path: Path,
    content: str | None,
    message: str,
) -> None:
    """A missing or unreadable credential file fails at startup with an actionable error."""
    credentials_path = tmp_path / "google-credentials.json"
    if content is not None:
        credentials_path.write_text(content, encoding="utf-8")
    runtime_paths = _runtime_paths(tmp_path, GOOGLE_APPLICATION_CREDENTIALS=str(credentials_path))

    with pytest.raises(PermanentStartupError, match=message):
        build_claude_provider(_vertex_config(), runtime_paths)


def test_bedrock_claude_prefers_an_explicit_profile_over_static_env_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured ``aws_profile`` resolves credentials through a boto3 session in the env's region."""
    (tmp_path / ".env").write_text(
        "AWS_ACCESS_KEY_ID=env-access\nAWS_SECRET_ACCESS_KEY=env-secret\nAWS_REGION=us-east-1\n",
        encoding="utf-8",
    )

    class _Session:
        def __init__(self, **kwargs: object) -> None:
            self.profile_name = kwargs.get("profile_name")
            self.region_name = kwargs.get("region_name")

    monkeypatch.setattr("boto3.session.Session", _Session)
    model_config = ModelConfig(
        provider="bedrock_claude",
        id="anthropic.claude-opus-5",
        extra_kwargs={"aws_profile": "my-explicit-profile"},
    )

    settings = build_claude_provider(model_config, _runtime_paths(tmp_path)).settings

    assert isinstance(settings.session, _Session)
    assert (settings.session.profile_name, settings.session.region_name) == ("my-explicit-profile", "us-east-1")
    assert (settings.aws_access_key, settings.aws_secret_key) == (None, None)


def test_bedrock_claude_installs_boto3_on_demand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """boto3 is an optional dependency that the Bedrock route installs when it is first built."""
    calls: list[tuple[list[str], str]] = []

    def ensure(dependencies: list[str], extra_name: str, *_args: object, **_kwargs: object) -> bool:
        calls.append((dependencies, extra_name))
        return False

    monkeypatch.setattr(claude_messages, "ensure_optional_deps", ensure)
    model_config = ModelConfig(
        provider="bedrock_claude",
        id="anthropic.claude-opus-5",
        extra_kwargs={"aws_access_key": "aws-access", "aws_secret_key": "aws-secret", "aws_region": "us-east-1"},
    )

    build_claude_provider(model_config, _runtime_paths(tmp_path))

    assert calls == [(["boto3"], "aws_bedrock")]


def test_bedrock_claude_records_none_of_the_aws_keys_it_reads_from_the_runtime_env(tmp_path: Path) -> None:
    """Static AWS keys from the runtime ``.env`` authenticate requests but stay out of the route's model params."""
    (tmp_path / ".env").write_text(
        "AWS_ACCESS_KEY_ID=env-access\nAWS_SECRET_ACCESS_KEY=env-secret\nAWS_SESSION_TOKEN=env-session\n",
        encoding="utf-8",
    )
    model_config = ModelConfig(
        provider="bedrock_claude",
        id="anthropic.claude-opus-5",
        extra_kwargs={"aws_region": "us-east-1"},
    )

    provider = build_claude_provider(model_config, _runtime_paths(tmp_path))

    settings = provider.settings
    assert (settings.aws_access_key, settings.aws_secret_key, settings.aws_session_token) == (
        "env-access",
        "env-secret",
        "env-session",
    )
    assert provider.model_params == {
        "max_tokens": 8192,
        "cache_system_prompt": True,
        "extended_cache_time": True,
        "timeout": 3600.0,
        "aws_region": "us-east-1",
    }


_COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
}


@pytest.fixture
def chat_wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    """Answer every request sent through the shared model HTTP client with a finished chat completion."""
    wire = Wire(httpx.Response(200, json=_COMPLETION))
    monkeypatch.setattr(MODEL_HTTP_CLIENT, "send", wire.client.send)
    return wire


async def _send(model_config: ModelConfig, runtime_paths: RuntimePaths) -> None:
    config = Config(models={"default": model_config})
    await complete(
        build_model_provider(config, runtime_paths),
        ModelRequest(messages=[Message(role="user", content="Hi")], stream=False),
    )


@pytest.mark.asyncio
async def test_azure_takes_its_key_endpoint_and_version_from_the_runtime_env(tmp_path: Path, chat_wire: Wire) -> None:
    """The Azure route reads ``AZURE_OPENAI_*`` from the runtime ``.env``."""
    (tmp_path / ".env").write_text(
        "AZURE_OPENAI_API_KEY=sk-azure\n"
        "AZURE_OPENAI_ENDPOINT=https://example-resource.openai.azure.com\n"
        "AZURE_OPENAI_API_VERSION=2024-10-21\n",
        encoding="utf-8",
    )

    await _send(ModelConfig(provider="azure", id="team-chat-deployment"), _runtime_paths(tmp_path))

    [request] = chat_wire.requests
    assert request.url.host == "example-resource.openai.azure.com"
    assert request.url.params["api-version"] == "2024-10-21"
    assert "/deployments/team-chat-deployment/" in request.url.path
    assert request.headers["api-key"] == "sk-azure"


@pytest.mark.asyncio
async def test_azure_reads_an_endpoint_file_and_only_azure_specific_variables(tmp_path: Path, chat_wire: Wire) -> None:
    """``AZURE_OPENAI_ENDPOINT_FILE`` and ``AZURE_OPENAI_DEPLOYMENT`` apply; the generic ``OPENAI_API_VERSION`` does not."""
    endpoint_file = tmp_path / "azure-endpoint.txt"
    endpoint_file.write_text("https://file-resource.openai.azure.com\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "AZURE_OPENAI_API_KEY=sk-azure\n"
        f"AZURE_OPENAI_ENDPOINT_FILE={endpoint_file}\n"
        "OPENAI_API_VERSION=2023-01-01\n"
        "AZURE_OPENAI_DEPLOYMENT=env-chat-deployment\n",
        encoding="utf-8",
    )

    await _send(ModelConfig(provider="azure", id="config-chat-deployment"), _runtime_paths(tmp_path))

    [request] = chat_wire.requests
    assert request.url.host == "file-resource.openai.azure.com"
    assert "/deployments/env-chat-deployment/" in request.url.path
    assert request.url.params["api-version"] != "2023-01-01"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_kwargs", "host"),
    [({}, "api.z.ai"), ({"base_url": "https://open.bigmodel.cn/api/paas/v4"}, "open.bigmodel.cn")],
    ids=["default", "custom"],
)
async def test_zai_sends_to_its_endpoint_with_the_runtime_env_key(
    tmp_path: Path,
    chat_wire: Wire,
    extra_kwargs: dict[str, str],
    host: str,
) -> None:
    """Z.ai defaults to its own API and takes ``ZAI_API_KEY`` from the runtime env when the config sets none."""
    runtime_paths = _runtime_paths(tmp_path, ZAI_API_KEY="env-zai-key")

    await _send(ModelConfig(provider="zai", id="glm-5.3", extra_kwargs=extra_kwargs), runtime_paths)

    [request] = chat_wire.requests
    assert request.url.host == host
    assert request.headers["authorization"] == "Bearer env-zai-key"


@pytest.mark.asyncio
async def test_zai_without_a_key_never_sends_the_openai_key(tmp_path: Path, chat_wire: Wire) -> None:
    """A Z.ai model without a key sends the keyless placeholder, never ``OPENAI_API_KEY`` from the environment."""
    runtime_paths = _runtime_paths(tmp_path, OPENAI_API_KEY="sk-openai")

    await _send(ModelConfig(provider="zai", id="glm-5.3", extra_kwargs={"api_key": None}), runtime_paths)

    [request] = chat_wire.requests
    assert request.headers["authorization"] == "Bearer not-provided"


def test_the_openai_family_builder_rejects_keys_its_route_does_not_take(tmp_path: Path) -> None:
    """A misspelled setting fails at construction and names the keys the route accepts."""
    model_config = ModelConfig(provider="zai", id="glm-5.3", extra_kwargs={"temprature": 0.2})

    with pytest.raises(ValueError, match=r"Unsupported extra_kwargs for provider 'zai': temprature\. Accepted keys: "):
        build_openai_provider(model_config, ProviderRuntime(runtime_paths=_runtime_paths(tmp_path)))


# --- Retired Agno fields --------------------------------------------------------------------------------------

_RETIRED_FIELD_WARNING = "Ignoring a retired config field; remove it from the config"


def _load_with_retired_field_warnings(models: dict[str, dict[str, Any]]) -> tuple[Config, list[str]]:
    """Load ``models`` in a fresh process state and return the config with the fields it warned about."""
    legacy_fields._warn_retired_field.cache_clear()
    with capture_logs() as logs:
        config = Config.model_validate({"models": models})
    return config, [log["field"] for log in logs if log["event"] == _RETIRED_FIELD_WARNING]


@pytest.mark.asyncio
async def test_a_retired_agno_field_loads_with_one_warning_and_the_model_answers(
    tmp_path: Path,
    chat_wire: Wire,
) -> None:
    """``retries`` on two OpenAI models warns once, leaves their settings, and the model still answers."""
    retrying = {"provider": "openai", "id": "gpt-6-luna", "api": "chat_completions"}
    config, warned = _load_with_retired_field_warnings(
        {
            "default": {**retrying, "extra_kwargs": {"retries": 3, "seed": 7}},
            "helper": {**retrying, "extra_kwargs": {"retries": 1}},
        },
    )

    assert warned == ["extra_kwargs.retries"]
    assert config.models["default"].extra_kwargs == {"seed": 7}
    assert config.models["helper"].extra_kwargs == {}
    turn = await complete(
        build_model_provider(config, _runtime_paths(tmp_path, OPENAI_API_KEY="sk-test")),
        ModelRequest(messages=[Message(role="user", content="Hi")], stream=False),
    )
    assert turn.message.content == "ok"
    [request] = chat_wire.requests
    body = json.loads(request.content)
    assert body["seed"] == 7
    assert "retries" not in body


@pytest.mark.parametrize(
    ("provider", "field"),
    [
        ("synthetic", "retry_with_guidance"),
        ("groq", "cache_response"),
        ("kimi", "modalities"),
        ("openai", "background"),
        ("codex", "temperature"),
        ("openai_codex", "max_output_tokens"),
        ("anthropic", "skills"),
        ("vertexai_claude", "auth_token"),
        ("google", "url_context"),
        ("gemini", "speech_config"),
    ],
)
def test_retired_agno_fields_load_with_one_warning_on_the_providers_that_took_them(provider: str, field: str) -> None:
    """A field the provider's Agno model class took loads with one warning and leaves the model's other keys."""
    config, warned = _load_with_retired_field_warnings(
        {"default": {"provider": provider, "id": "m", "extra_kwargs": {field: True, "other": 1}}},
    )

    assert warned == [f"extra_kwargs.{field}"]
    assert config.models["default"].extra_kwargs == {"other": 1}


def test_a_retired_field_that_two_providers_took_warns_once() -> None:
    """Agno's OpenAI Chat and Gemini models both took the metrics switch, so setting it on each still warns once."""
    switch = {"collect_metrics_on_completion": True}
    config, warned = _load_with_retired_field_warnings(
        {
            "default": {"provider": "openai", "id": "gpt-6-luna", "extra_kwargs": switch},
            "gemini": {"provider": "google", "id": "gemini-3.8-flash", "extra_kwargs": switch},
        },
    )

    assert warned == ["extra_kwargs.collect_metrics_on_completion"]
    assert (config.models["default"].extra_kwargs, config.models["gemini"].extra_kwargs) == ({}, {})


@pytest.mark.parametrize(
    ("provider", "field"),
    [("openai", "retriez"), ("openai", "skills"), ("groq", "audio"), ("anthropic", "url_context")],
    ids=["typo", "claude-only", "chat-only", "gemini-only"],
)
def test_keys_no_agno_class_of_the_provider_took_still_fail_at_build(
    tmp_path: Path,
    provider: str,
    field: str,
) -> None:
    """A typo or another provider's Agno field loads without a warning and fails at build with the accepted keys."""
    config, warned = _load_with_retired_field_warnings(
        {"default": {"provider": provider, "id": "m", "extra_kwargs": {field: True}}},
    )

    assert warned == []
    with pytest.raises(
        ValueError,
        match=rf"Unsupported extra_kwargs for provider '{provider}': {field}\. Accepted keys: ",
    ):
        build_model_provider(config, _runtime_paths(tmp_path))


def _accepted_keys(route: str) -> frozenset[str]:
    """Return every ``extra_kwargs`` key ``route`` accepts, from the settings its adapter parses."""
    if route in ACCEPTED_KEYS:
        return ACCEPTED_KEYS[route]
    if route == "codex":
        return CODEX_FIELDS
    settings_types = {
        "google": [GeminiSettings],
        "ollama": [OllamaSettings],
        "synthetic": [SyntheticSettings],
    }.get(route, [ChatSettings, ResponsesSettings])
    return frozenset(field.name for settings_type in settings_types for field in fields(settings_type))


def test_no_route_accepts_a_retired_agno_field_of_its_provider() -> None:
    """Config load drops a retired field only where no route would take it, so no accepted setting is lost."""
    routes = frozenset(ACCEPTED_KEYS) | OPENAI_FAMILY_PROVIDERS | {"google", "ollama", "synthetic"}

    for retired in legacy_fields._RETIRED_MODEL_SETTINGS:
        scope = routes if retired.providers is None else retired.providers
        assert scope <= routes
        for route in scope:
            assert not retired.fields & _accepted_keys(route), route
