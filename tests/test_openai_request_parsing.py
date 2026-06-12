"""Pure unit tests for /v1 request parsing and model-identity resolution.

Covers body parsing of malformed payloads, message conversion, and
agent vs team model-name validation against a config snapshot.
"""

from __future__ import annotations

import json

from fastapi.responses import JSONResponse

from mindroom.api.openai_request_parsing import (
    ChatCompletionRequest,
    ChatMessage,
    convert_messages,
    openai_compatible_agent_names,
    openai_incompatible_agents,
    parse_chat_completion_body,
    validate_chat_request,
)
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME


def _config() -> Config:
    return Config(
        agents={
            "general": AgentConfig(display_name="GeneralAgent", role="General-purpose assistant", rooms=[]),
            "code": AgentConfig(display_name="CodeAgent", role="Coder", rooms=[]),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
    )


def _request(model: str, messages: list[dict] | None = None) -> ChatCompletionRequest:
    parsed = parse_chat_completion_body(
        json.dumps(
            {
                "model": model,
                "messages": messages if messages is not None else [{"role": "user", "content": "hi"}],
            },
        ).encode(),
    )
    assert isinstance(parsed, ChatCompletionRequest)
    return parsed


def _error_body(response: JSONResponse) -> dict:
    return json.loads(bytes(response.body))["error"]


class TestParseChatCompletionBody:
    """Tests for parse_chat_completion_body()."""

    def test_valid_body(self) -> None:
        """A valid payload parses into a typed request."""
        req = _request("general")
        assert req.model == "general"
        assert req.messages == [ChatMessage(role="user", content="hi")]
        assert req.stream is False

    def test_unknown_fields_are_ignored(self) -> None:
        """Extra OpenAI parameters are accepted and ignored."""
        parsed = parse_chat_completion_body(
            json.dumps(
                {
                    "model": "general",
                    "messages": [{"role": "user", "content": "hi"}],
                    "some_future_param": {"x": 1},
                },
            ).encode(),
        )
        assert isinstance(parsed, ChatCompletionRequest)

    def test_utf8_bom_body(self) -> None:
        """A UTF-8 BOM-prefixed body parses (Windows/PowerShell and Java clients emit BOMs)."""
        body = json.dumps({"model": "general", "messages": [{"role": "user", "content": "hi"}]})
        parsed = parse_chat_completion_body(body.encode("utf-8-sig"))
        assert isinstance(parsed, ChatCompletionRequest)
        assert parsed.model == "general"

    def test_utf16_and_utf32_bodies(self) -> None:
        """UTF-16/UTF-32 bodies parse via json.detect_encoding, matching old json.loads behavior."""
        body = json.dumps({"model": "general", "messages": [{"role": "user", "content": "hi"}]})
        for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32"):
            parsed = parse_chat_completion_body(body.encode(encoding))
            assert isinstance(parsed, ChatCompletionRequest), encoding
            assert parsed.model == "general"

    def test_utf8_bom_invalid_and_non_object_bodies(self) -> None:
        """BOM-prefixed invalid JSON and non-object JSON still return 400."""
        for text in ("{not json", "null", "[]", "42"):
            response = parse_chat_completion_body(text.encode("utf-8-sig"))
            assert isinstance(response, JSONResponse), text
            assert response.status_code == 400
            assert _error_body(response)["message"] == "Invalid request body"

    def test_undecodable_body(self) -> None:
        """Bytes that cannot be decoded as detected encoding return 400."""
        response = parse_chat_completion_body(b"\xff\xfe\xff{bad}")
        assert isinstance(response, JSONResponse)
        assert response.status_code == 400
        assert _error_body(response)["message"] == "Invalid request body"

    def test_malformed_json(self) -> None:
        """Invalid JSON returns a 400 OpenAI-style error."""
        response = parse_chat_completion_body(b"{not json")
        assert isinstance(response, JSONResponse)
        assert response.status_code == 400
        assert _error_body(response)["message"] == "Invalid request body"

    def test_schema_violation(self) -> None:
        """Schema violations (messages not a list) return a 400 error."""
        response = parse_chat_completion_body(json.dumps({"model": "general", "messages": "nope"}).encode())
        assert isinstance(response, JSONResponse)
        assert response.status_code == 400

    def test_valid_json_non_object_bodies(self) -> None:
        """Valid JSON that is not an object (null, list, scalar) returns a 400 error."""
        for body in (b"null", b"[]", b'"hello"', b"42"):
            response = parse_chat_completion_body(body)
            assert isinstance(response, JSONResponse)
            assert response.status_code == 400
            assert _error_body(response)["message"] == "Invalid request body"

    def test_missing_model(self) -> None:
        """A payload without a model returns a 400 error."""
        response = parse_chat_completion_body(json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode())
        assert isinstance(response, JSONResponse)
        assert response.status_code == 400


class TestValidateChatRequest:
    """Tests for agent vs team model-identity validation."""

    def test_known_agent_is_valid(self) -> None:
        """A configured agent model passes validation."""
        assert validate_chat_request(_request("general"), _config()) is None

    def test_auto_is_valid(self) -> None:
        """The reserved auto model defers routing to the handler."""
        assert validate_chat_request(_request("auto"), _config()) is None

    def test_unknown_agent_404(self) -> None:
        """An unknown agent model returns 404 model_not_found."""
        response = validate_chat_request(_request("ghost"), _config())
        assert isinstance(response, JSONResponse)
        assert response.status_code == 404
        assert _error_body(response)["code"] == "model_not_found"

    def test_router_is_not_addressable(self) -> None:
        """The built-in router is never a /v1 model."""
        config = _config()
        config.agents[ROUTER_AGENT_NAME] = AgentConfig(display_name="Router", role="", rooms=[])
        response = validate_chat_request(_request(ROUTER_AGENT_NAME), config)
        assert isinstance(response, JSONResponse)
        assert response.status_code == 404

    def test_empty_messages_400(self) -> None:
        """An empty messages array is rejected before model resolution."""
        response = validate_chat_request(_request("general", messages=[]), _config())
        assert isinstance(response, JSONResponse)
        assert response.status_code == 400

    def test_known_team_is_valid(self) -> None:
        """A configured team model passes validation via the team/ prefix."""
        config = _config()
        config.teams = {
            "dev-team": TeamConfig(display_name="Dev Team", role="", agents=["general", "code"], mode="coordinate"),
        }
        assert validate_chat_request(_request("team/dev-team"), config) is None

    def test_unknown_team_404(self) -> None:
        """An unknown team model returns 404 model_not_found."""
        response = validate_chat_request(_request("team/ghost-team"), _config())
        assert isinstance(response, JSONResponse)
        assert response.status_code == 404
        assert _error_body(response)["code"] == "model_not_found"

    def test_non_shared_worker_scope_agent_rejected(self) -> None:
        """Agents requiring non-shared worker scopes are rejected on /v1."""
        config = _config()
        config.agents["code"].worker_scope = "user"
        response = validate_chat_request(_request("code"), config)
        assert isinstance(response, JSONResponse)
        assert response.status_code == 400
        assert _error_body(response)["code"] == "unsupported_worker_scope"

    def test_team_with_unsupported_member_rejected(self) -> None:
        """Teams containing unsupported agents are rejected on /v1."""
        config = _config()
        config.agents["code"].worker_scope = "user"
        config.teams = {
            "dev-team": TeamConfig(display_name="Dev Team", role="", agents=["general", "code"], mode="coordinate"),
        }
        response = validate_chat_request(_request("team/dev-team"), config)
        assert isinstance(response, JSONResponse)
        assert response.status_code == 400
        assert _error_body(response)["code"] == "unsupported_worker_scope"


class TestCompatibilityResolution:
    """Tests for the compatible/incompatible agent helpers."""

    def test_compatible_agent_names_excludes_scoped_agents(self) -> None:
        """Non-shared worker scopes drop agents from the /v1 model list."""
        config = _config()
        config.agents["code"].worker_scope = "user"
        assert openai_compatible_agent_names(config) == ["general"]

    def test_incompatible_agents_lists_only_scoped_agents(self) -> None:
        """Only agents whose closure needs unsupported scopes are flagged."""
        config = _config()
        config.agents["code"].worker_scope = "user_agent"
        assert openai_incompatible_agents(["general", "code"], config) == ["code"]


class TestConvertMessages:
    """Tests for OpenAI message conversion into prompt and thread history."""

    def test_single_user_message(self) -> None:
        """One user message becomes the prompt with no history."""
        prompt, history = convert_messages([ChatMessage(role="user", content="hi")])
        assert prompt == "hi"
        assert history is None

    def test_system_prompt_prefixes_last_user_message(self) -> None:
        """System and developer text is folded into the prompt."""
        prompt, history = convert_messages(
            [
                ChatMessage(role="system", content="Be terse."),
                ChatMessage(role="user", content="hi"),
            ],
        )
        assert prompt == "Be terse.\n\nhi"
        assert history is None

    def test_prior_turns_become_thread_history(self) -> None:
        """Earlier user/assistant turns become synthetic thread history."""
        prompt, history = convert_messages(
            [
                ChatMessage(role="user", content="first"),
                ChatMessage(role="assistant", content="answer"),
                ChatMessage(role="user", content="second"),
            ],
        )
        assert prompt == "second"
        assert history is not None
        assert [(m.sender, m.body) for m in history] == [("user", "first"), ("assistant", "answer")]

    def test_tool_messages_are_skipped(self) -> None:
        """Tool role messages never reach the prompt or history."""
        prompt, history = convert_messages(
            [
                ChatMessage(role="tool", content="raw tool output"),
                ChatMessage(role="user", content="hi"),
            ],
        )
        assert prompt == "hi"
        assert history is None

    def test_multimodal_content_extracts_text_parts(self) -> None:
        """Multimodal content lists contribute only their text parts."""
        prompt, _history = convert_messages(
            [
                ChatMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": "look at"},
                        {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                        {"type": "text", "text": "this"},
                    ],
                ),
            ],
        )
        assert prompt == "look at this"

    def test_no_user_message_yields_empty_prompt(self) -> None:
        """Assistant-only conversations produce no prompt."""
        prompt, history = convert_messages([ChatMessage(role="assistant", content="hello")])
        assert prompt == ""
        assert history is None
