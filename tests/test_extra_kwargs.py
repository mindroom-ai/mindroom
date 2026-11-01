"""A model's ``extra_kwargs`` and the runtime env shape its route's settings and the Claude prompt-cache ladder."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from pydantic import ValidationError

from mindroom.config.main import Config, ConfigRuntimeValidationError, iter_config_validation_messages
from mindroom.config.models import DebugConfig, ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.hooks import render_transient_context
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, complete
from mindroom.providers.anthropic import messages as claude_messages
from mindroom.providers.anthropic.messages import build_provider as build_claude_provider
from mindroom.providers.anthropic.prompt_cache import native_tool_search_supported, prepare_request_body
from mindroom.providers.anthropic.repairs import replay_safe_blocks
from mindroom.providers.anthropic.settings import ClaudeSettings
from mindroom.providers.cache_ladder import MAX_CACHE_MARKERS, has_cache_marker, prompt_cache_control
from mindroom.providers.claude_wire_blocks import RebuiltBlock
from mindroom.providers.extra_kwargs import settings_params
from mindroom.providers.gemini.settings import GeminiSettings
from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT
from mindroom.providers.ollama.settings import OllamaSettings
from mindroom.providers.openai.build import accepted_extra_kwargs
from mindroom.providers.openai.build import build_provider as build_openai_provider
from mindroom.providers.openai.settings import ChatSettings, ResponsesSettings
from mindroom.providers.registry import build_model_provider
from mindroom.providers.synthetic_adapter import SyntheticSettings
from mindroom.redaction import REDACTED
from mindroom.startup_errors import PermanentStartupError
from tests.providers.anthropic_family_support import claude_turn, route_provider, sse_reply
from tests.providers.golden_support import Wire, streamed_reply
from tests.providers.openai_family_support import provider_runtime

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
        {"max_tokens": 8192, "temperature": 0.3},
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
    (GeminiSettings, {"temperature": 0.3}, frozenset({"api_key", "client_params"})),
    (OllamaSettings, {"options": {"num_ctx": 8192}}, frozenset({"api_key", "client_params"})),
    (SyntheticSettings, {"seed": 7}, frozenset({"api_key"})),
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
                api_key="openrouter-key",
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
    provider = build_model_provider(config, _runtime_paths(tmp_path))

    await complete(provider, ModelRequest(messages=[Message(role="user", content="Hi")]))

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
        "use_thinking": True,
    }
    (log_file,) = log_dir.glob("llm-requests-*.jsonl")
    [request_record] = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert request_record["model_params"] == provider.model_params
    # The body the record holds is the one sent, its secret field masked and the header left out as a request option.
    assert (request_record["body"]["api_key"], request_record["body"]["transforms"]) == (REDACTED, ["middle-out"])
    assert not [
        secret for secret in ("nested-header-secret", "nested-body-secret") if secret in json.dumps(request_record)
    ]


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
        system="",
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
    """``extended_cache_time`` selects the 1h TTL; without it the marker sends no ttl field."""
    assert prompt_cache_control(extended_cache_time=False) == _FIVE_MINUTES
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
async def test_turn_context_takes_no_marker_and_replays_unchanged_on_the_next_turn(tmp_path: Path) -> None:
    """Turn context after the prompt stays unmarked, and the next turn replays it unchanged after that prompt."""
    transient = render_transient_context(
        ("<mindroom_message_context>Current location: Amsterdam</mindroom_message_context>",),
    )
    wire = Wire(sse_reply([{"type": "text", "text": "A1"}]))
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs={"extended_cache_time": False})
    system = Message(role="system", content="Stable system")
    first = [Message(role="user", content="Q1"), Message(role="user", content=transient, add_to_agent_memory=False)]
    second_turn = [
        Message(role="user", content="Q2"),
        Message(role="user", content=transient, add_to_agent_memory=False),
    ]
    for messages in (first, [*first, Message(role="assistant", content="A1"), *second_turn]):
        await claude_turn(provider, ModelRequest(messages=[system, *messages]))

    current = wire.body(0)["messages"][0]["content"]
    assert current[0]["cache_control"] == _FIVE_MINUTES
    assert "cache_control" not in current[1]
    second = wire.body(1)["messages"]
    assert [block["text"] for block in second[0]["content"]] == [block["text"] for block in current]
    assert "cache_control" not in second[0]["content"][1]
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


def test_message_rungs_that_reach_the_limit_leave_the_last_tool_unmarked() -> None:
    """The tools marker comes last, so two authored system markers and two new rungs leave it no budget."""
    body = {
        "system": [
            {"type": "text", "text": "S1", "cache_control": _FIVE_MINUTES},
            {"type": "text", "text": "S2", "cache_control": _FIVE_MINUTES},
        ],
        "tools": [_tool("demo_tool")],
        "messages": [{"role": "user", "content": [_text("m1")]}, {"role": "user", "content": [_text("m2")]}],
    }

    prepared = _prepared(body)

    assert [message["content"][0]["cache_control"] for message in prepared["messages"]] == [_FIVE_MINUTES] * 2
    assert prepared["tools"] == body["tools"]
    assert _marker_count(prepared) == MAX_CACHE_MARKERS


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


def test_only_a_stored_assistant_block_carries_a_marker() -> None:
    """Text a stored Claude response holds can take a rung; text rebuilt from a message's fields cannot."""
    stored = _text("stored answer")
    rebuilt = RebuiltBlock(text="rebuilt answer", type="text")

    def marked(answer: dict[str, Any]) -> list[bool]:
        messages = [
            {"role": "user", "content": [_text("earlier turn")]},
            {"role": "assistant", "content": [answer]},
            {"role": "user", "content": [_text("this turn")]},
        ]
        return [has_cache_marker(message["content"][0]) for message in _prepared({"messages": messages})["messages"]]

    assert marked(stored) == [False, True, True]
    assert marked(rebuilt) == [True, False, True]


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
    ("route", "model_id", "expected"),
    [
        ("anthropic", "claude-opus-5", True),
        ("anthropic", "claude-sonnet-5", True),
        ("anthropic", "claude-sonnet-4-5-20250929", True),
        ("vertexai_claude", "claude-haiku-4-5@20251001", True),
        # Unreleased Claude models default to the native path (denylist gating).
        ("anthropic", "claude-opus-4-9", True),
        ("anthropic", "claude-fable-6", True),
        ("anthropic", "claude-opus-4-1", False),
        ("anthropic", "claude-opus-4-20250514", False),
        ("anthropic", "claude-3-5-sonnet-20241022", False),
        ("vertexai_claude", "claude-sonnet-4@20250514", False),
        ("bedrock_claude", "anthropic.claude-opus-5", False),
    ],
)
def test_native_tool_search_gating(route: ClaudeRouteKind, model_id: str, *, expected: bool) -> None:
    """Claude routes with server-side tool search qualify unless the model predates it."""
    assert native_tool_search_supported(route, model_id) is expected


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
    """A search result's response-only fields are left out of the replayed shape, without changing the input."""
    blocks = [dict(_SEARCH_USE), dict(_DIRTY_SEARCH_RESULT), _text("found it")]

    assert replay_safe_blocks(blocks) == [_SEARCH_USE, _SEARCH_RESULT, _text("found it")]
    assert "citations" in blocks[1]


def test_only_orphaned_search_uses_are_removed() -> None:
    """A regex search without its result in the same turn goes; other server tools and paired searches stay."""
    blocks = [dict(block) for block in (_ORPHAN_SEARCH_USE, _OTHER_SERVER_TOOL_USE, _SEARCH_USE, _DIRTY_SEARCH_RESULT)]

    assert replay_safe_blocks([*blocks, _text("found it")]) == [
        _OTHER_SERVER_TOOL_USE,
        _SEARCH_USE,
        _SEARCH_RESULT,
        _text("found it"),
    ]
    assert blocks[0] == _ORPHAN_SEARCH_USE


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


def test_vertex_claude_keeps_its_configured_credentials_over_the_runtime_file(tmp_path: Path) -> None:
    """Credentials a model passes in ``client_params`` win, so the runtime ``GOOGLE_APPLICATION_CREDENTIALS`` is unread."""
    runtime_paths = _runtime_paths(tmp_path, GOOGLE_APPLICATION_CREDENTIALS=str(tmp_path / "missing.json"))
    configured = object()
    model_config = _vertex_config()
    model_config = model_config.model_copy(
        update={"extra_kwargs": {**(model_config.extra_kwargs or {}), "client_params": {"credentials": configured}}},
    )

    provider = build_claude_provider(model_config, runtime_paths)

    assert provider.settings.client_params == {"credentials": configured}


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
    """Answer every request sent through the shared model HTTP client with a finished chat completion stream."""
    wire = Wire(lambda _request: streamed_reply(_COMPLETION))
    monkeypatch.setattr(MODEL_HTTP_CLIENT, "send", wire.client.send)
    return wire


async def _send(model_config: ModelConfig, runtime_paths: RuntimePaths) -> None:
    config = Config(models={"default": model_config})
    await complete(
        build_model_provider(config, runtime_paths),
        ModelRequest(messages=[Message(role="user", content="Hi")]),
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
async def test_azure_blank_exports_do_not_hide_the_env_file_settings(tmp_path: Path, chat_wire: Wire) -> None:
    """A blank exported ``AZURE_OPENAI_*`` variable is unset, so the ``.env`` value under it applies."""
    (tmp_path / ".env").write_text(
        "AZURE_OPENAI_API_KEY=sk-azure\n"
        "AZURE_OPENAI_ENDPOINT=https://example-resource.openai.azure.com\n"
        "AZURE_OPENAI_API_VERSION=2025-04-01-preview\n"
        "AZURE_OPENAI_DEPLOYMENT=env-chat-deployment\n",
        encoding="utf-8",
    )
    blank = dict.fromkeys(("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_DEPLOYMENT"), "")

    await _send(ModelConfig(provider="azure", id="config-chat-deployment"), _runtime_paths(tmp_path, **blank))

    [request] = chat_wire.requests
    assert request.url.host == "example-resource.openai.azure.com"
    assert request.url.params["api-version"] == "2025-04-01-preview"
    assert "/deployments/env-chat-deployment/" in request.url.path


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

    with pytest.raises(
        ValueError,
        match=r"Unsupported extra_kwargs for provider 'zai': temprature; remove or correct them\. Accepted keys: ",
    ):
        build_openai_provider(
            model_config,
            provider_runtime(_runtime_paths(tmp_path)),
        )


# --- Unsupported keys at config load ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "field"),
    [
        ("openai", "retries"),
        ("synthetic", "retry_with_guidance"),
        ("groq", "cache_response"),
        ("kimi", "modalities"),
        ("openrouter", "audio"),
        ("openai", "background"),
        ("codex", "temperature"),
        ("openai_codex", "max_output_tokens"),
        ("anthropic", "skills"),
        ("vertexai_claude", "auth_token"),
        ("google", "url_context"),
        ("gemini", "speech_config"),
        ("ollama", "retries"),
    ],
)
def test_a_retired_agno_model_field_fails_config_load_and_names_the_model(
    tmp_path: Path,
    provider: str,
    field: str,
) -> None:
    """A field only the provider's Agno model class took fails config load like any unknown key, before any build."""
    data = {"models": {"default": {"provider": provider, "id": "m", "extra_kwargs": {field: True}}}}

    with pytest.raises(ConfigRuntimeValidationError) as exc_info:
        Config.validate_with_runtime(data, _runtime_paths(tmp_path))

    assert str(exc_info.value).startswith(
        f"models.default: Unsupported extra_kwargs for provider '{provider}': {field}; remove or correct them. "
        "Accepted keys: ",
    )


@pytest.mark.parametrize("provider", ["openai", "azure", "openrouter", "codex", "anthropic"])
def test_a_retired_max_retries_fails_config_load_with_its_reason(tmp_path: Path, provider: str) -> None:
    """The SDK's own retries are gone, so a model that sets them is told why and what to do."""
    data = {"models": {"default": {"provider": provider, "id": "m", "extra_kwargs": {"max_retries": 3}}}}

    with pytest.raises(ConfigRuntimeValidationError) as exc_info:
        Config.validate_with_runtime(data, _runtime_paths(tmp_path))

    assert str(exc_info.value) == (
        f"models.default: Unsupported extra_kwargs for provider '{provider}': max_retries was removed; "
        "MindRoom retries a failed model request itself, so remove the setting."
    )


@pytest.mark.parametrize("provider", ["openai", "openrouter", "llama_cpp", "groq"])
def test_a_retired_role_map_fails_config_load_with_its_reason(tmp_path: Path, provider: str) -> None:
    """The role map is gone, so a model that sets it is told how requests name the system prompt and what to do."""
    role_map = {"system": "system", "user": "user", "assistant": "assistant", "tool": "tool"}
    data = {"models": {"default": {"provider": provider, "id": "m", "extra_kwargs": {"role_map": role_map}}}}

    with pytest.raises(ConfigRuntimeValidationError) as exc_info:
        Config.validate_with_runtime(data, _runtime_paths(tmp_path))

    assert str(exc_info.value) == (
        f"models.default: Unsupported extra_kwargs for provider '{provider}': role_map was removed; "
        "Chat Completions requests send the system prompt as developer to OpenAI's own API and as system to every "
        "other endpoint, an OpenAI-compatible server set as provider openai with its base_url included, so remove the "
        "setting."
    )


_MODELS_DOC = Path(__file__).resolve().parents[1] / "docs" / "configuration" / "models.md"


@pytest.mark.parametrize(
    ("row", "provider"),
    [
        ("`openai` with Chat Completions, `zai`, `llama_cpp`", "llama_cpp"),
        ("`groq`", "groq"),
        ("`cerebras`", "cerebras"),
    ],
    ids=["chat", "groq", "cerebras"],
)
def test_the_models_page_lists_the_keys_each_chat_route_accepts(tmp_path: Path, row: str, provider: str) -> None:
    """The accepted-keys table of the models page names exactly the ``extra_kwargs`` keys the route accepts."""
    lines = _MODELS_DOC.read_text(encoding="utf-8").splitlines()
    [cell] = [line.split("|")[2] for line in lines if line.startswith(f"| {row} |")]

    documented = {key.strip().strip("`") for key in cell.split(",")}

    assert documented == accepted_extra_kwargs(ModelConfig(provider=provider, id="m"), _runtime_paths(tmp_path))


_NOT_A_MAPPING = "extra_kwargs.client_params must be a mapping, not str"
_NESTED_NOT_A_STRING = "extra_kwargs.client_params.base_url must be a string, not int"


@pytest.mark.parametrize(
    ("provider", "extra_kwargs", "error"),
    [
        ("openai", {"client_params": "http://127.0.0.1:9/v1"}, _NOT_A_MAPPING),
        ("openai", {"base_url": 9}, "extra_kwargs.base_url must be a string, not int"),
        ("openai", {"client_params": {"base_url": 9}}, _NESTED_NOT_A_STRING),
        ("openai", {"client_params": {"base_url": 0}, "base_url": "http://127.0.0.1:9/v1"}, _NESTED_NOT_A_STRING),
        ("groq", {"client_params": "http://127.0.0.1:9"}, _NOT_A_MAPPING),
        ("codex", {"client_params": {"base_url": 9}}, _NESTED_NOT_A_STRING),
        ("anthropic", {"client_params": "http://127.0.0.1:9"}, _NOT_A_MAPPING),
        ("google", {"client_params": "http://127.0.0.1:9"}, _NOT_A_MAPPING),
        ("ollama", {"client_params": "http://127.0.0.1:9"}, _NOT_A_MAPPING),
    ],
)
def test_a_wrongly_typed_client_setting_fails_config_validation_at_the_model(
    provider: str,
    extra_kwargs: dict[str, Any],
    error: str,
) -> None:
    """The type of ``client_params`` and the endpoints is checked with the model, whatever its provider."""
    data = {"models": {"default": {"provider": provider, "id": "gpt-6-astra", "extra_kwargs": extra_kwargs}}}

    with pytest.raises(ValidationError) as exc_info:
        Config.model_validate(data)

    assert iter_config_validation_messages(exc_info.value) == [
        ("models → default → extra_kwargs", error),
    ]


_OPENAI_ENDPOINT_ENV = {"OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}


@pytest.mark.parametrize(
    ("model", "process_env"),
    [
        pytest.param({"provider": "openai", "id": "local-model"}, {}, id="openai-chat"),
        pytest.param({"provider": "openai", "id": "gpt-6-astra"}, {}, id="openai-native-tool-search-responses"),
        pytest.param({"provider": "openai", "id": "gpt-6-astra"}, _OPENAI_ENDPOINT_ENV, id="openai-env-endpoint-chat"),
        pytest.param({"provider": "openai", "id": "local-model", "api": "responses"}, {}, id="openai-api-responses"),
        pytest.param({"provider": "openai", "id": "gpt-6-astra", "api": "chat_completions"}, {}, id="openai-api-chat"),
        *(
            pytest.param({"provider": provider, "id": "m"}, {}, id=provider)
            for provider in (
                "azure",
                "openrouter",
                "deepseek",
                "zai",
                "llama_cpp",
                "kimi",
                "groq",
                "cerebras",
                "codex",
                "anthropic",
                "vertexai_claude",
                "google",
                "ollama",
                "synthetic",
            )
        ),
        pytest.param(
            {"provider": "bedrock_claude", "id": "m", "extra_kwargs": {"aws_access_key": "a", "aws_secret_key": "s"}},
            {},
            id="bedrock_claude",
        ),
    ],
)
def test_config_load_rejects_the_keys_the_model_build_rejects(
    tmp_path: Path,
    model: dict[str, Any],
    process_env: dict[str, str],
) -> None:
    """Config load picks the route the build picks, so both name the same unsupported and accepted keys."""
    runtime_paths = _runtime_paths(tmp_path, **process_env)
    data = {"models": {"default": {**model, "extra_kwargs": {**model.get("extra_kwargs", {}), "zzz": True}}}}

    with pytest.raises(ConfigRuntimeValidationError) as load_error:
        Config.validate_with_runtime(data, runtime_paths)
    with pytest.raises(ValueError, match="Unsupported extra_kwargs") as build_error:
        build_model_provider(Config.model_validate(data), runtime_paths)

    assert str(load_error.value) == f"models.default: {build_error.value}"
