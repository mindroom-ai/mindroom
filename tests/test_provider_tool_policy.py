"""Provider request boundaries must keep participation decisions free of native tools."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

import pytest
from agno.media import File
from agno.models.message import Message
from google.genai.types import GenerateContentConfig

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.google_gemini import MindRoomGoogleGemini
from mindroom.model_loading import get_model_instance
from mindroom.openai_models import MindRoomOpenAIChat, MindRoomOpenAIResponses, MindRoomOpenRouter
from mindroom.openai_tool_search import install_openai_deferred_tool_search
from mindroom.provider_tool_policy import without_provider_tools
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.gemini_helpers import gemini_client

if TYPE_CHECKING:
    from pathlib import Path

    import httpx
    from agno.models.groq import Groq


def _function_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": "read_status", "parameters": {"type": "object", "properties": {}}},
    }


@pytest.mark.parametrize("tool_type", ["web_search", "code_interpreter", "mcp", "file_search"])
@pytest.mark.parametrize("source", ["tools", "request_params", "extra_body"])
def test_responses_disable_native_tools_after_authored_overrides(tool_type: str, source: str) -> None:
    """Native tools cannot execute through caller, authored, or SDK body overrides."""
    native_tool = {"type": tool_type}
    authored = {"tool_choice": "required"}
    model = MindRoomOpenAIResponses(id="gpt-6-astra", api_key="test-key", request_params=authored)
    tools = [native_tool, _function_tool()]
    if source == "request_params":
        authored["tools"] = [native_tool]
    elif source == "extra_body":
        authored["extra_body"] = {"tools": [native_tool], "tool_choice": {"type": tool_type}}
    before = deepcopy(authored)
    tools_before = deepcopy(tools)

    with without_provider_tools():
        request = model.get_request_params(messages=[], tools=tools, tool_choice="auto")

    assert request["tool_choice"] == "none"
    if source == "extra_body":
        assert request["extra_body"]["tool_choice"] == "none"
        assert request["extra_body"]["tools"] == [native_tool]
    assert authored == before
    assert tools == tools_before
    assert model.get_request_params(messages=[], tools=deepcopy(tools))["tool_choice"] == "required"


def test_responses_disable_deferred_search_without_changing_tool_prefix() -> None:
    """Hosted deferred search must be disabled even when injected after base parameters."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", api_key="test-key")
    install_openai_deferred_tool_search(model, deferred_tool_names=frozenset({"read_status"}))
    regular = model.get_request_params(messages=[], tools=[_function_tool()], tool_choice="auto")

    with without_provider_tools():
        decision = model.get_request_params(messages=[], tools=[_function_tool()], tool_choice="auto")

    assert decision["tool_choice"] == "none"
    assert decision["tools"] == regular["tools"]
    assert decision["tools"][0] == {"type": "tool_search"}
    assert decision["tools"][1]["defer_loading"] is True
    assert regular["tool_choice"] == "auto"


def test_responses_file_search_does_not_upload_before_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabling the final native tool must also prevent its eager upload preparation."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", api_key="test-key")
    uploaded: list[File] = []

    def upload(file: File) -> None:
        uploaded.append(file)

    monkeypatch.setattr(model, "_upload_file", upload)
    message = Message(role="user", content="Review attachment", files=[File(content=b"report", filename="report.txt")])
    with without_provider_tools():
        request = model.get_request_params(messages=[message], tools=[{"type": "file_search"}])

    assert uploaded == []
    assert request["tool_choice"] == "none"
    assert message.files is not None


@pytest.mark.parametrize("model_type", [MindRoomOpenAIChat, MindRoomOpenAIResponses])
@pytest.mark.parametrize("source", ["argument", "request_params", "extra_body"])
def test_function_only_decisions_disable_selection_after_overrides(model_type: type, source: str) -> None:
    """Function selection can invalidate a decision even though no tool is executed."""
    authored: dict[str, Any] = {} if source == "argument" else {"tool_choice": "required"}
    if source == "extra_body":
        authored = {"extra_body": {"tool_choice": "required"}}
    before = deepcopy(authored)
    model = model_type(id="gpt-6-astra", api_key="test-key", request_params=authored)
    regular = model.get_request_params(tools=[_function_tool()], tool_choice="auto")

    with without_provider_tools():
        decision = model.get_request_params(tools=[_function_tool()], tool_choice="auto")

    assert decision["tool_choice"] == "none"
    if source == "extra_body":
        assert decision["extra_body"]["tool_choice"] == "none"
    assert decision["tools"] == regular["tools"]
    assert authored == before
    assert model.get_request_params(tools=[_function_tool()], tool_choice="auto") == regular


@pytest.mark.parametrize(
    "options",
    [
        {"request_params": {"web_search_options": {}}},
        {"extra_body": {"web_search_options": {}}},
        {"request_params": {"extra_body": {"web_search_options": {}}}},
        {"extra_body": {"model": "gpt-5-search-api"}},
        {"id": "gpt-5-search-api"},
        {"id": "gpt-4o-search-preview"},
    ],
)
def test_chat_native_search_fails_closed_before_request(options: dict[str, Any]) -> None:
    """Chat search always executes; it cannot be made safe by tool_choice alone."""
    model = MindRoomOpenAIChat(api_key="test-key", **options)
    regular = model.get_request_params()

    with without_provider_tools(), pytest.raises(ValueError, match=r"native.*search"):
        model.get_request_params(tool_choice="none")

    assert model.get_request_params() == regular


@pytest.mark.parametrize("source", ["flags", "generation_config", "request_config", "request_dict"])
def test_gemini_removes_native_tools_without_mutating_authored_config(source: str) -> None:
    """Native grounding/code entries must be absent from the final GenerateContentConfig."""
    authored = {
        "system_instruction": "Stable agent instructions",
        "tools": [
            {"google_search": {}},
            {"code_execution": {}},
            {"url_context": {}},
            {"function_declarations": [{"name": "read_status", "description": "Read status"}]},
        ],
        "tool_config": {"function_calling_config": {"mode": "AUTO"}},
    }
    options: dict[str, Any] = {}
    if source == "flags":
        options.update(search=True, url_context=True)
    elif source == "generation_config":
        options["generation_config"] = authored
    else:
        options["request_params"] = {
            "config": GenerateContentConfig(**authored) if source == "request_config" else authored,
        }
    before = deepcopy(options)
    model = MindRoomGoogleGemini(id="gemini-2.5-pro", api_key="test-key", **options)

    with without_provider_tools():
        request = model.get_request_params()

    config = GenerateContentConfig.model_validate(request["config"])
    assert all(tool.function_declarations for tool in config.tools or [])
    if source != "flags":
        assert len(config.tools) == 1
        assert config.tools[0].function_declarations[0].name == "read_status"
        assert config.system_instruction == "Stable agent instructions"
        assert config.tool_config.function_calling_config.mode == "NONE"
    assert config.response_mime_type == "application/json"
    assert options == before
    regular = GenerateContentConfig.model_validate(model.get_request_params()["config"])
    assert any(tool.google_search is not None for tool in regular.tools)
    assert regular.response_mime_type is None


@pytest.mark.asyncio
@pytest.mark.parametrize("vertexai", [False, True], ids=["gemini_api", "vertex_ai"])
@pytest.mark.parametrize(
    ("authored_field", "authored_schema"),
    [("response_schema", {"type": "STRING"}), ("response_json_schema", {"type": "string"})],
)
@pytest.mark.parametrize("typed", [False, True], ids=["dict", "typed"])
async def test_gemini_decisions_drop_the_authored_output_schema(
    vertexai: bool,
    authored_field: str,
    authored_schema: dict[str, str],
    typed: bool,
) -> None:
    """Only the caller's decision schema may shape a decision; the reply's authored output schema never does."""

    def unreachable(_request: httpx.Request) -> httpx.Response:
        raise AssertionError

    decision_schema = {"type": "object", "properties": {"decision": {"type": "boolean"}}, "required": ["decision"]}
    options = {"response_mime_type": "application/json", authored_field: authored_schema}
    authored = GenerateContentConfig(**options) if typed else options
    before = deepcopy(authored)
    async with gemini_client(unreachable, vertexai=vertexai) as client:
        model = MindRoomGoogleGemini(id="gemini-2.5-pro", client=client, generation_config=authored)
        with without_provider_tools(response_schema=decision_schema):
            decision = model.get_request_params(tools=[_function_tool()], tool_choice="none")["config"]
        with without_provider_tools():
            schemaless = model.get_request_params(tools=[_function_tool()], tool_choice="none")["config"]
        regular = model.get_request_params(tools=[_function_tool()], tool_choice="auto")["config"]
    assert authored == before

    for config in (decision, schemaless):
        assert config.tools[0].function_declarations[0].name == "read_status"
        assert config.tool_config.function_calling_config.mode == "NONE"
        assert config.response_schema is None
        # Vertex AI never pairs JSON output with declarations, not even an authored JSON format.
        assert config.response_mime_type == (None if vertexai else "application/json")
    assert decision.response_json_schema == (None if vertexai else decision_schema)
    assert schemaless.response_json_schema is None
    assert regular.model_dump(exclude_none=True)[authored_field]
    assert regular.tool_config.function_calling_config.mode == "AUTO"


def test_gemini_models_sharing_one_generation_config_stay_independent(tmp_path: Path) -> None:
    """Instances loaded from one models entry share its dict; a reply must not leak its tools into a judgment."""
    authored = {"max_output_tokens": 256}
    config = bind_runtime_paths(
        Config(
            models={
                "gemini": ModelConfig(
                    provider="gemini",
                    id="test",
                    extra_kwargs={"api_key": "test-key", "generation_config": authored},
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    reply = get_model_instance(config, runtime_paths_for(config), "gemini")
    judge = get_model_instance(config, runtime_paths_for(config), "gemini")
    shared = config.models["gemini"].extra_kwargs["generation_config"]
    assert reply.generation_config is judge.generation_config is shared

    reply.get_request_params(system_message="Reply.", tools=[_function_tool()], tool_choice="auto")
    with without_provider_tools():
        judgment = judge.get_request_params(system_message="Judge.", tools=[], tool_choice="none")["config"]

    assert shared == {"max_output_tokens": 256}
    assert judgment.tools is None
    assert judgment.tool_config is None
    assert judgment.system_instruction == "Judge."
    assert judgment.response_mime_type == "application/json"


@pytest.mark.asyncio
async def test_vertex_gemini_requests_json_only_without_declarations() -> None:
    """Vertex AI acceptance of JSON beside disabled declarations is unverified, so declarations keep NONE only."""

    def unreachable(_request: httpx.Request) -> httpx.Response:
        raise AssertionError

    async with gemini_client(unreachable, vertexai=True) as client:
        model = MindRoomGoogleGemini(id="gemini-2.5-pro", client=client)
        with without_provider_tools(response_schema={"type": "object"}):
            declared = model.get_request_params(tools=[_function_tool()], tool_choice="none")["config"]
            undeclared = model.get_request_params(system_message="Judge.", tool_choice="none")["config"]

    assert declared.tools[0].function_declarations[0].name == "read_status"
    assert declared.tool_config.function_calling_config.mode == "NONE"
    assert declared.response_mime_type is None
    assert declared.response_json_schema is None
    assert undeclared.tools is None
    assert undeclared.tool_config is None
    assert undeclared.response_mime_type == "application/json"
    assert undeclared.response_json_schema == {"type": "object"}


def test_gemini_keeps_only_declarations_in_mixed_tool() -> None:
    """A native field sharing one Tool object with functions must still be removed."""
    config = GenerateContentConfig(
        tools=[{"google_search": {}, "function_declarations": [{"name": "read_status"}]}],
    )
    model = MindRoomGoogleGemini(api_key="test-key", request_params={"config": config})
    with without_provider_tools():
        decision = model.get_request_params()["config"]

    assert decision.tools[0].google_search is None
    assert decision.tools[0].function_declarations[0].name == "read_status"
    assert config.tools[0].google_search is not None


def test_gemini_explicit_cache_fails_closed() -> None:
    """Explicit caches may contain native tools invisible to request filtering."""
    model = MindRoomGoogleGemini(api_key="test-key", cached_content="cachedContents/example")
    with without_provider_tools(), pytest.raises(ValueError, match="cached"):
        model.get_request_params()

    assert model.get_request_params()["config"].cached_content == "cachedContents/example"


@pytest.mark.parametrize("source", ["request_config", "client_params"])
def test_gemini_raw_body_overrides_fail_closed(source: str) -> None:
    """SDK body overrides can restore native tools after generation config filtering."""
    options = {"http_options": {"extra_body": {"tools": [{"googleSearch": {}}]}}}
    kwargs = {"request_params": {"config": options}} if source == "request_config" else {"client_params": options}
    model = MindRoomGoogleGemini(api_key="test-key", **kwargs)

    with without_provider_tools(), pytest.raises(ValueError, match="body overrides"):
        model.get_request_params()


@pytest.mark.parametrize(
    "options",
    [
        {"id": "openai/gpt-6-astra:online"},
        {"id": "openai/gpt-6-astra:free:online"},
        {"models": ["openai/gpt-6-astra", "openai/gpt-6-astra:online"]},
        {"request_params": {"models": ["openai/gpt-6-astra:online"]}},
        {"extra_body": {"model": "openai/gpt-6-astra:online"}},
        {"extra_body": {"models": ["openai/gpt-6-astra:online"]}},
        {"request_params": {"plugins": [{"id": "web"}]}},
        {"extra_body": {"plugins": [{"id": "web", "enabled": True}]}},
        {"request_params": {"extra_body": {"plugins": [{"id": "web", "engine": "native"}]}}},
    ],
)
def test_openrouter_explicit_search_fails_closed(options: dict[str, Any]) -> None:
    """Online variants and web plugins run before the model can honor tool_choice."""
    model = MindRoomOpenRouter(api_key="test-key", **options)
    regular = model.get_request_params()

    with without_provider_tools(), pytest.raises(ValueError, match="OpenRouter search"):
        model.get_request_params(tool_choice="none")

    assert model.get_request_params() == regular


@pytest.mark.parametrize("source", ["request_params", "extra_body", "extra_body_override"])
def test_openrouter_disabled_search_still_suppresses_function_selection(source: str) -> None:
    """A disabled web plugin permits the check while ordinary functions remain unselectable."""
    plugins = [{"id": "web", "enabled": False}, {"id": "response-healing"}]
    if source == "request_params":
        model = MindRoomOpenRouter(api_key="test-key", request_params={"plugins": plugins})
    elif source == "extra_body":
        model = MindRoomOpenRouter(api_key="test-key", extra_body={"plugins": plugins})
    else:
        model = MindRoomOpenRouter(
            api_key="test-key",
            request_params={"plugins": [{"id": "web"}], "extra_body": {"plugins": plugins}},
        )
    regular = model.get_request_params(tools=[_function_tool()], tool_choice="auto")

    with without_provider_tools():
        decision = model.get_request_params(tools=[_function_tool()], tool_choice="auto")

    assert decision == {**regular, "tool_choice": "none"}
    assert regular["tool_choice"] == "auto"
    assert model.get_request_params(tools=[_function_tool()], tool_choice="auto") == regular
    assert plugins[0]["enabled"] is False


@pytest.mark.parametrize(
    ("model_id", "request_params"),
    [
        ("groq/compound", {}),
        ("groq/compound-mini", {}),
        ("compound-beta", {}),
        ("compound-beta-mini", {}),
        ("custom-alias", {"compound_custom": {"tools": {"enabled_tools": ["web_search"]}}}),
        ("custom-alias", {"extra_body": {"compound_custom": {"tools": {"enabled_tools": ["code_interpreter"]}}}}),
        ("custom-alias", {"extra_body": {"model": "groq/compound"}}),
    ],
)
def test_groq_compound_decision_fails_closed_through_model_loader(
    tmp_path: Path,
    model_id: str,
    request_params: dict[str, Any],
) -> None:
    """The configured Groq provider must reject its automatic native tool systems."""
    config = bind_runtime_paths(
        Config(
            models={
                "candidate": ModelConfig(
                    provider="groq",
                    id=model_id,
                    extra_kwargs={"api_key": "test-key", "request_params": request_params},
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    model = cast("Groq", get_model_instance(config, runtime_paths_for(config), "candidate"))
    regular = model.get_request_params()

    with without_provider_tools(), pytest.raises(ValueError, match="Groq Compound"):
        model.get_request_params(tools=[_function_tool()], tool_choice="none")

    assert model.get_request_params() == regular


@pytest.mark.parametrize("tool_type", ["function", "browser_search"])
def test_groq_explicit_tools_preserve_functions_and_disable_native_execution(tmp_path: Path, tool_type: str) -> None:
    """Ordinary Groq functions keep their prefix; hosted tools cannot execute during decisions."""
    config = bind_runtime_paths(
        Config(
            models={
                "candidate": ModelConfig(
                    provider="groq",
                    id="openai/gpt-oss-120b",
                    extra_kwargs={"api_key": "test-key"},
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    model = cast("Groq", get_model_instance(config, runtime_paths_for(config), "candidate"))
    tools = [_function_tool()] if tool_type == "function" else [{"type": tool_type}]
    regular = model.get_request_params(tools=tools, tool_choice="auto")

    with without_provider_tools():
        decision = model.get_request_params(tools=tools, tool_choice="auto")

    assert decision["tool_choice"] == "none"
    assert decision["tools"] == regular["tools"]
    assert model.get_request_params(tools=tools, tool_choice="auto") == regular


@pytest.mark.parametrize(
    ("provider", "source"),
    [
        ("cerebras", "tools"),
        ("cerebras", "request_params"),
        ("cerebras", "extra_body"),
        ("ollama", "tools"),
        ("ollama", "request_params"),
    ],
)
def test_loader_provider_decisions_cannot_select_functions(tmp_path: Path, provider: str, source: str) -> None:
    """Providers that discard Agno's tool_choice still need a final request restriction."""
    function = _function_tool()
    function["function"]["description"] = "Read current status"
    authored: dict[str, Any] = {}
    if source == "request_params":
        authored = {"tools": [function]}
        if provider == "cerebras":
            authored["tool_choice"] = "required"
    elif source == "extra_body":
        authored = {"extra_body": {"tools": [function], "tool_choice": "required"}}
    before = deepcopy(authored)
    options: dict[str, Any] = {"request_params": authored}
    if provider == "cerebras":
        options["api_key"] = "test-key"
    config = bind_runtime_paths(
        Config(models={"candidate": ModelConfig(provider=provider, id="test", extra_kwargs=options)}),
        test_runtime_paths(tmp_path),
    )
    model = get_model_instance(config, runtime_paths_for(config), "candidate")
    tools = [function] if source == "tools" else None
    regular = model.get_request_params(tools=tools)

    with without_provider_tools():
        decision = model.get_request_params(tools=tools)

    if provider == "ollama":
        assert "tools" not in decision
        assert "tools" in regular
    else:
        assert decision["tool_choice"] == "none"
        if source == "extra_body":
            assert decision["extra_body"]["tool_choice"] == "none"
            assert decision["extra_body"]["tools"] == regular["extra_body"]["tools"]
        else:
            assert decision["tools"] == regular["tools"]
    assert authored == before
    assert model.get_request_params(tools=tools) == regular
