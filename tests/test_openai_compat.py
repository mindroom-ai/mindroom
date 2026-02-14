"""Tests for the OpenAI-compatible chat completions API."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent, RunOutput, RunPausedEvent
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement
from fastapi import Request
from fastapi.testclient import TestClient

from mindroom.api import openai_compat as openai_compat_module
from mindroom.api.openai_compat import (
    ChatMessage,
    _convert_messages,
    _derive_session_id,
    _extract_content_text,
    _is_error_response,
)
from mindroom.config import AgentConfig, Config, ModelConfig, RouterConfig, TeamConfig


@pytest.fixture
def test_config() -> Config:
    """Create a minimal test config with a few agents."""
    return Config(
        agents={
            "general": AgentConfig(
                display_name="GeneralAgent",
                role="General-purpose assistant",
                rooms=[],
            ),
            "code": AgentConfig(
                display_name="CodeAgent",
                role="Generate code and manage files",
                tools=["file", "shell"],
                rooms=[],
            ),
            "research": AgentConfig(
                display_name="ResearchAgent",
                role="",
                rooms=[],
            ),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
    )


@pytest.fixture
def app_client(test_config: Config) -> Iterator[TestClient]:
    """Create a FastAPI test client with mocked config."""
    from fastapi import FastAPI  # noqa: PLC0415

    from mindroom.api.openai_compat import router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)

    with (
        patch("mindroom.api.openai_compat._load_config", return_value=(test_config, Path(__file__))),
        patch.dict("os.environ", {"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"}),
    ):
        yield TestClient(app)


@pytest.fixture
def authed_client(test_config: Config) -> Iterator[TestClient]:
    """Create a test client with API key auth enabled."""
    from fastapi import FastAPI  # noqa: PLC0415

    from mindroom.api.openai_compat import router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)

    with (
        patch("mindroom.api.openai_compat._load_config", return_value=(test_config, Path(__file__))),
        patch.dict("os.environ", {"OPENAI_COMPAT_API_KEYS": "test-key-1,test-key-2"}),
    ):
        yield TestClient(app)


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------


class TestListModels:
    """Tests for GET /v1/models."""

    def test_lists_agents(self, app_client: TestClient) -> None:
        """Lists all configured agents as models, plus auto."""
        response = app_client.get("/v1/models")
        assert response.status_code == 200

        data = response.json()
        assert data["object"] == "list"
        model_ids = [m["id"] for m in data["data"]]
        assert "auto" in model_ids
        assert "general" in model_ids
        assert "code" in model_ids
        assert "research" in model_ids

    def test_includes_name_and_description(self, app_client: TestClient) -> None:
        """Models include display name and role description."""
        response = app_client.get("/v1/models")
        data = response.json()

        general = next(m for m in data["data"] if m["id"] == "general")
        assert general["name"] == "GeneralAgent"
        assert general["description"] == "General-purpose assistant"
        assert general["owned_by"] == "mindroom"
        assert general["object"] == "model"

    def test_empty_role_is_none(self, app_client: TestClient) -> None:
        """Agents with empty role have description=None."""
        response = app_client.get("/v1/models")
        data = response.json()

        research = next(m for m in data["data"] if m["id"] == "research")
        assert research["description"] is None

    def test_excludes_router(self, app_client: TestClient) -> None:
        """Router agent is not listed."""
        response = app_client.get("/v1/models")
        data = response.json()
        model_ids = [m["id"] for m in data["data"]]
        assert "router" not in model_ids

    def test_auto_model_listed_first(self, app_client: TestClient) -> None:
        """Auto model is listed first with description."""
        response = app_client.get("/v1/models")
        data = response.json()
        first = data["data"][0]
        assert first["id"] == "auto"
        assert first["name"] == "Auto"
        assert "routes" in first["description"].lower() or "auto" in first["description"].lower()

    def test_empty_agents_still_has_auto(self) -> None:
        """With no agents configured, only auto is listed."""
        from fastapi import FastAPI  # noqa: PLC0415

        from mindroom.api.openai_compat import router  # noqa: PLC0415

        app = FastAPI()
        app.include_router(router)

        empty_config = Config(
            agents={},
            models={"default": ModelConfig(provider="ollama", id="test")},
            router=RouterConfig(model="default"),
        )
        with (
            patch("mindroom.api.openai_compat._load_config", return_value=(empty_config, Path(__file__))),
            patch.dict("os.environ", {"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"}),
        ):
            client = TestClient(app)
            response = client.get("/v1/models")
            assert response.status_code == 200
            data = response.json()["data"]
            assert len(data) == 1
            assert data[0]["id"] == "auto"


class TestChatCompletions:
    """Tests for POST /v1/chat/completions (non-streaming)."""

    def test_basic_completion(self, app_client: TestClient) -> None:
        """Basic non-streaming completion returns correct shape."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = "Hello! How can I help?"

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "chat.completion"
        assert data["model"] == "general"
        assert data["id"].startswith("chatcmpl-")
        assert len(data["choices"]) == 1
        assert data["choices"][0]["message"]["role"] == "assistant"
        assert data["choices"][0]["message"]["content"] == "Hello! How can I help?"
        assert data["choices"][0]["finish_reason"] == "stop"
        assert data["usage"]["prompt_tokens"] == 0

    def test_passes_include_default_tools_false(self, app_client: TestClient) -> None:
        """Passes include_default_tools=False to exclude scheduler."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = "Response"

            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

            assert mock_ai.call_args.kwargs["include_default_tools"] is False
            assert mock_ai.call_args.kwargs["include_interactive_questions"] is False

    def test_passes_knowledge_none(self, app_client: TestClient) -> None:
        """Passes knowledge=None when agent has no knowledge_bases."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = "Response"

            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

            assert mock_ai.call_args.kwargs["knowledge"] is None

    def test_passes_user_id(self, app_client: TestClient) -> None:
        """Passes user field as user_id to ai_response."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = "Response"

            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "user": "user-123",
                },
            )

            assert mock_ai.call_args.kwargs["user_id"] == "user-123"

    def test_explicit_session_id_header_is_stable_across_turns(self, app_client: TestClient) -> None:
        """Repeated requests with the same X-Session-Id should reuse one derived session ID."""
        observed_session_ids: list[str] = []

        async def _capture(*args: object, **kwargs: object) -> str:  # noqa: ARG001
            observed_session_ids.append(kwargs["session_id"])
            return "Response"

        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.side_effect = _capture

            app_client.post(
                "/v1/chat/completions",
                headers={"X-Session-Id": "shared-session"},
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Turn one"}],
                },
            )
            app_client.post(
                "/v1/chat/completions",
                headers={"X-Session-Id": "shared-session"},
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Turn two"}],
                },
            )

        assert len(observed_session_ids) == 2
        assert observed_session_ids[0] == observed_session_ids[1]
        assert observed_session_ids[0].endswith(":shared-session")

    def test_explicit_session_id_header_is_namespaced_by_api_key(self, authed_client: TestClient) -> None:
        """Same X-Session-Id with different API keys should map to different derived IDs."""
        observed_session_ids: list[str] = []

        async def _capture(*args: object, **kwargs: object) -> str:  # noqa: ARG001
            observed_session_ids.append(kwargs["session_id"])
            return "Response"

        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.side_effect = _capture

            authed_client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer test-key-1",
                    "X-Session-Id": "shared-session",
                },
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Turn one"}],
                },
            )
            authed_client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer test-key-2",
                    "X-Session-Id": "shared-session",
                },
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Turn two"}],
                },
            )

        assert len(observed_session_ids) == 2
        assert observed_session_ids[0] != observed_session_ids[1]
        assert observed_session_ids[0].endswith(":shared-session")
        assert observed_session_ids[1].endswith(":shared-session")

    def test_unknown_model_404(self, app_client: TestClient) -> None:
        """Unknown model returns 404 with OpenAI error format."""
        response = app_client.post(
            "/v1/chat/completions",
            json={
                "model": "nonexistent",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

        assert response.status_code == 404
        data = response.json()
        assert data["error"]["code"] == "model_not_found"
        assert data["error"]["param"] == "model"
        assert "nonexistent" in data["error"]["message"]

    def test_router_model_404(self, app_client: TestClient) -> None:
        """Router agent cannot be used as a model."""
        response = app_client.post(
            "/v1/chat/completions",
            json={
                "model": "router",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

        assert response.status_code == 404

    def test_unknown_team_404(self, app_client: TestClient) -> None:
        """Unknown team models return 404 (no teams in test_config)."""
        response = app_client.post(
            "/v1/chat/completions",
            json={
                "model": "team/nonexistent",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

        assert response.status_code == 404
        assert "nonexistent" in response.json()["error"]["message"]
        assert response.json()["error"]["code"] == "model_not_found"

    def test_empty_messages_400(self, app_client: TestClient) -> None:
        """Empty messages array returns 400."""
        response = app_client.post(
            "/v1/chat/completions",
            json={
                "model": "general",
                "messages": [],
            },
        )

        assert response.status_code == 400

    def test_extra_fields_ignored(self, app_client: TestClient) -> None:
        """Extra/unknown fields don't cause 422."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = "Response"

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "temperature": 0.7,
                    "max_tokens": 100,
                    "logit_bias": {"42": 10},
                    "seed": 42,
                    "unknown_field": "should be ignored",
                },
            )

        assert response.status_code == 200

    def test_error_response_detection(self, app_client: TestClient) -> None:
        """Error strings from ai_response() become HTTP 500 with sanitized message."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = "❌ Authentication failed (openai): Invalid API key"

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 500
        error = response.json()["error"]
        assert error["type"] == "server_error"
        # Error message is sanitized — raw backend details are not exposed
        assert error["message"] == "Agent execution failed"

    def test_agent_prefix_error_detection(self, app_client: TestClient) -> None:
        """Error strings with [agent] prefix are detected."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = "[general] ⚠️ Error: something went wrong"

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 500


class TestStreamingCompletion:
    """Tests for POST /v1/chat/completions with stream=true."""

    def test_streaming_sse_format(self, app_client: TestClient) -> None:
        """Streaming returns valid SSE format."""
        from agno.run.agent import RunContentEvent  # noqa: PLC0415

        async def mock_stream(**_kw: object) -> AsyncIterator[RunContentEvent]:
            yield RunContentEvent(content="Hello ")
            yield RunContentEvent(content="world!")

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        # Parse SSE lines
        lines = response.text.strip().split("\n\n")
        assert len(lines) >= 4  # role + 2 content + finish + [DONE]

        # First chunk: role announcement
        first = json.loads(lines[0].removeprefix("data: "))
        assert first["choices"][0]["delta"] == {"role": "assistant"}
        assert first["object"] == "chat.completion.chunk"

        # Content chunks
        second = json.loads(lines[1].removeprefix("data: "))
        assert second["choices"][0]["delta"]["content"] == "Hello "

        third = json.loads(lines[2].removeprefix("data: "))
        assert third["choices"][0]["delta"]["content"] == "world!"

        # Finish chunk
        fourth = json.loads(lines[3].removeprefix("data: "))
        assert fourth["choices"][0]["finish_reason"] == "stop"
        assert fourth["choices"][0]["delta"] == {}

        # [DONE] terminator
        assert lines[4] == "data: [DONE]"

    def test_streaming_passes_include_interactive_questions_false(self, app_client: TestClient) -> None:
        """Streaming disables interactive question prompting for OpenAI compatibility."""
        from agno.run.agent import RunContentEvent  # noqa: PLC0415

        async def mock_stream(**_kw: object) -> AsyncIterator[RunContentEvent]:
            yield RunContentEvent(content="ok")

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream) as mock_stream_fn:
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert mock_stream_fn.call_args.kwargs["include_default_tools"] is False
        assert mock_stream_fn.call_args.kwargs["include_interactive_questions"] is False

    def test_streaming_consistent_id(self, app_client: TestClient) -> None:
        """All streaming chunks have the same completion ID."""
        from agno.run.agent import RunContentEvent  # noqa: PLC0415

        async def mock_stream(**_kw: object) -> AsyncIterator[RunContentEvent]:
            yield RunContentEvent(content="test")

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
            )

        lines = response.text.strip().split("\n\n")
        ids = []
        for line in lines:
            text = line.removeprefix("data: ")
            if text == "[DONE]":
                continue
            chunk = json.loads(text)
            ids.append(chunk["id"])

        assert len(set(ids)) == 1  # All same ID
        assert ids[0].startswith("chatcmpl-")

    def test_streaming_cached_response(self, app_client: TestClient) -> None:
        """Cached full response (string) is streamed correctly."""

        async def mock_stream(**_kw: object) -> AsyncIterator[str]:
            yield "This is a cached response"

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        lines = response.text.strip().split("\n\n")
        content_chunk = json.loads(lines[1].removeprefix("data: "))
        assert content_chunk["choices"][0]["delta"]["content"] == "This is a cached response"

    def test_streaming_first_event_error_returns_500(self, app_client: TestClient) -> None:
        """If first stream event is an error string, return HTTP 500 instead of SSE."""

        async def mock_stream(**_kw: object) -> AsyncIterator[str]:
            yield "❌ Authentication failed (openai): Invalid API key"

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
            )

        assert response.status_code == 500
        error = response.json()["error"]
        assert error["type"] == "server_error"
        assert error["message"] == "Agent execution failed"

    def test_streaming_tool_events(self, app_client: TestClient) -> None:
        """Streaming emits start/done tool blocks with a stable per-stream tool id."""
        from agno.models.response import ToolExecution  # noqa: PLC0415
        from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent  # noqa: PLC0415

        tool_started = ToolExecution(
            tool_name="search",
            tool_args={"query": "X"},
            tool_call_id="tc-stream-1",
        )
        tool_completed = ToolExecution(
            tool_name="search",
            tool_args={"query": "X"},
            tool_call_id="tc-stream-1",
            result="3 results",
        )

        async def mock_stream(**_kw: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="Let me search. ")
            yield ToolCallStartedEvent(tool=tool_started)
            yield ToolCallCompletedEvent(tool=tool_completed)
            yield RunContentEvent(content="Found it!")

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Search for X"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        # Collect all content from chunks
        lines = response.text.strip().split("\n\n")
        contents = []
        for line in lines:
            text = line.removeprefix("data: ")
            if text == "[DONE]":
                continue
            chunk = json.loads(text)
            delta = chunk["choices"][0]["delta"]
            if "content" in delta:
                contents.append(delta["content"])

        full_content = "".join(contents)
        assert "Let me search. " in full_content
        assert '<tool id="1" state="start">search(query=X)</tool>' in full_content
        assert '<tool id="1" state="done">search(query=X)\n3 results</tool>' in full_content
        assert "Result:" not in full_content

    def test_streaming_tool_ids_increment_for_multiple_calls(self, app_client: TestClient) -> None:
        """Tool ids start at 1 and increment for each new started tool call in a stream."""
        from agno.models.response import ToolExecution  # noqa: PLC0415
        from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent  # noqa: PLC0415

        first_started = ToolExecution(
            tool_name="search",
            tool_args={"query": "one"},
            tool_call_id="tc-stream-1",
        )
        first_completed = ToolExecution(
            tool_name="search",
            tool_args={"query": "one"},
            tool_call_id="tc-stream-1",
            result="one-result",
        )
        second_started = ToolExecution(
            tool_name="search",
            tool_args={"query": "two"},
            tool_call_id="tc-stream-2",
        )
        second_completed = ToolExecution(
            tool_name="search",
            tool_args={"query": "two"},
            tool_call_id="tc-stream-2",
            result="two-result",
        )

        async def mock_stream(**_kw: object) -> AsyncIterator[object]:
            yield RunContentEvent(content="Start ")
            yield ToolCallStartedEvent(tool=first_started)
            yield ToolCallCompletedEvent(tool=first_completed)
            yield ToolCallStartedEvent(tool=second_started)
            yield ToolCallCompletedEvent(tool=second_completed)
            yield RunContentEvent(content="End")

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Run two searches"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        lines = response.text.strip().split("\n\n")
        contents: list[str] = []
        for line in lines:
            text = line.removeprefix("data: ")
            if text == "[DONE]":
                continue
            chunk = json.loads(text)
            delta = chunk["choices"][0]["delta"]
            if "content" in delta:
                contents.append(delta["content"])

        full_content = "".join(contents)
        assert '<tool id="1" state="start">search(query=one)</tool>' in full_content
        assert '<tool id="1" state="done">search(query=one)\none-result</tool>' in full_content
        assert '<tool id="2" state="start">search(query=two)</tool>' in full_content
        assert '<tool id="2" state="done">search(query=two)\ntwo-result</tool>' in full_content


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthentication:
    """Tests for bearer token authentication."""

    def test_valid_key_accepted(self, authed_client: TestClient) -> None:
        """Valid API key allows access."""
        response = authed_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer test-key-1"},
        )
        assert response.status_code == 200

    def test_second_key_accepted(self, authed_client: TestClient) -> None:
        """Second key from comma-separated list works."""
        response = authed_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer test-key-2"},
        )
        assert response.status_code == 200

    def test_missing_key_401(self, authed_client: TestClient) -> None:
        """Missing key returns 401."""
        response = authed_client.get("/v1/models")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"

    def test_wrong_key_401(self, authed_client: TestClient) -> None:
        """Wrong key returns 401."""
        response = authed_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert response.status_code == 401

    def test_auth_required_when_keys_unset(self, test_config: Config) -> None:
        """Auth is required by default when OPENAI_COMPAT_API_KEYS is unset."""
        from fastapi import FastAPI  # noqa: PLC0415

        from mindroom.api.openai_compat import router  # noqa: PLC0415

        app = FastAPI()
        app.include_router(router)
        with (
            patch("mindroom.api.openai_compat._load_config", return_value=(test_config, Path(__file__))),
            patch.dict(
                "os.environ",
                {
                    "OPENAI_COMPAT_API_KEYS": "",
                    "OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "false",
                },
            ),
        ):
            client = TestClient(app)
            response = client.get("/v1/models")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"

    def test_no_auth_when_explicitly_allowed(self, app_client: TestClient) -> None:
        """Unauthenticated mode works when explicitly opted in."""
        response = app_client.get("/v1/models")
        assert response.status_code == 200

    def test_auth_on_completions(self, authed_client: TestClient) -> None:
        """Auth is checked on completions endpoint too."""
        response = authed_client.post(
            "/v1/chat/completions",
            json={
                "model": "general",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


class TestMessageConversion:
    """Tests for _convert_messages()."""

    def test_simple_user_message(self) -> None:
        """Single user message becomes prompt with no history."""
        messages = [ChatMessage(role="user", content="Hello")]
        prompt, history = _convert_messages(messages)
        assert prompt == "Hello"
        assert history is None

    def test_multi_turn_conversation(self) -> None:
        """Multi-turn conversation splits into history + prompt."""
        messages = [
            ChatMessage(role="user", content="Hi"),
            ChatMessage(role="assistant", content="Hello!"),
            ChatMessage(role="user", content="How are you?"),
        ]
        prompt, history = _convert_messages(messages)
        assert prompt == "How are you?"
        assert history == [
            {"sender": "user", "body": "Hi"},
            {"sender": "assistant", "body": "Hello!"},
        ]

    def test_system_message_prepended(self) -> None:
        """System message is prepended to prompt."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Hello"),
        ]
        prompt, history = _convert_messages(messages)
        assert "You are helpful." in prompt
        assert "Hello" in prompt
        assert history is None

    def test_developer_role_treated_as_system(self) -> None:
        """Developer role is treated same as system."""
        messages = [
            ChatMessage(role="developer", content="Be concise."),
            ChatMessage(role="user", content="Hello"),
        ]
        prompt, _ = _convert_messages(messages)
        assert "Be concise." in prompt
        assert "Hello" in prompt

    def test_tool_messages_skipped(self) -> None:
        """Tool role messages are skipped."""
        messages = [
            ChatMessage(role="user", content="Run search"),
            ChatMessage(role="assistant", content="I'll search for that."),
            ChatMessage(role="tool", content="Search results: ..."),
            ChatMessage(role="user", content="Thanks"),
        ]
        prompt, history = _convert_messages(messages)
        assert prompt == "Thanks"
        # tool message should not appear in history
        assert history is not None
        assert all(h["sender"] != "tool" for h in history)

    def test_multimodal_content(self) -> None:
        """Multimodal content extracts text parts."""
        messages = [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "What is this?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    {"type": "text", "text": "Describe it."},
                ],
            ),
        ]
        prompt, _ = _convert_messages(messages)
        assert "What is this?" in prompt
        assert "Describe it." in prompt

    def test_none_content_skipped(self) -> None:
        """Messages with None content are skipped."""
        messages = [
            ChatMessage(role="assistant", content=None),
            ChatMessage(role="user", content="Hello"),
        ]
        prompt, history = _convert_messages(messages)
        assert prompt == "Hello"
        assert history is None

    def test_only_system_messages(self) -> None:
        """Only system messages become the prompt."""
        messages = [
            ChatMessage(role="system", content="Be helpful."),
        ]
        prompt, history = _convert_messages(messages)
        assert prompt == "Be helpful."
        assert history is None

    def test_conversation_ending_with_assistant(self) -> None:
        """Prompt uses last user message even when conversation ends with assistant."""
        messages = [
            ChatMessage(role="user", content="Hi"),
            ChatMessage(role="assistant", content="Hello! How can I help?"),
        ]
        prompt, history = _convert_messages(messages)
        # Last user message is "Hi", not the trailing assistant message
        assert prompt == "Hi"
        assert history is None

    def test_empty_messages(self) -> None:
        """Empty messages returns empty prompt."""
        prompt, history = _convert_messages([])
        assert prompt == ""
        assert history is None


# ---------------------------------------------------------------------------
# Session ID derivation
# ---------------------------------------------------------------------------


class TestSessionIdDerivation:
    """Tests for _derive_session_id()."""

    @staticmethod
    def _mock_request(headers: dict[str, str] | None = None) -> Request:
        """Create a mock Request with given headers."""
        mock = MagicMock(spec=Request)
        header_dict = headers or {}
        mock.headers = {k.lower(): v for k, v in header_dict.items()}
        return mock

    def test_explicit_session_id_header(self) -> None:
        """X-Session-Id header takes highest priority (namespaced with key)."""
        request = self._mock_request({"X-Session-Id": "my-session"})
        sid = _derive_session_id("general", request)
        # Session ID is namespaced with API key hash prefix
        assert sid.endswith(":my-session")
        assert sid.startswith("noauth:")  # No auth header

    def test_explicit_session_id_namespaced_by_key(self) -> None:
        """Different API keys produce different session namespaces."""
        req1 = self._mock_request({"X-Session-Id": "sess", "Authorization": "Bearer key-1"})
        req2 = self._mock_request({"X-Session-Id": "sess", "Authorization": "Bearer key-2"})
        sid1 = _derive_session_id("general", req1)
        sid2 = _derive_session_id("general", req2)
        # Same session ID but different keys → different derived IDs
        assert sid1 != sid2
        assert sid1.endswith(":sess")
        assert sid2.endswith(":sess")

    def test_librechat_conversation_id(self) -> None:
        """X-LibreChat-Conversation-Id header is used when no X-Session-Id."""
        request = self._mock_request({"X-LibreChat-Conversation-Id": "conv-123"})
        sid = _derive_session_id("general", request)
        assert "conv-123" in sid
        assert "general" in sid

    def test_session_id_takes_priority_over_librechat(self) -> None:
        """X-Session-Id takes priority over X-LibreChat-Conversation-Id."""
        request = self._mock_request(
            {
                "X-Session-Id": "explicit",
                "X-LibreChat-Conversation-Id": "libre",
            },
        )
        sid = _derive_session_id("general", request)
        assert "explicit" in sid
        assert "libre" not in sid

    def test_fallback_generates_ephemeral_session_id(self) -> None:
        """Fallback generates an ephemeral namespaced session ID."""
        request = self._mock_request()
        sid1 = _derive_session_id("general", request)
        assert sid1.startswith("noauth:ephemeral:")
        assert len(sid1) > len("noauth:ephemeral:")

    def test_fallback_is_not_deterministic(self) -> None:
        """Fallback IDs differ across requests to avoid cross-chat collisions."""
        request = self._mock_request()
        sid1 = _derive_session_id("general", request)
        sid2 = _derive_session_id("general", request)
        assert sid1 != sid2

    def test_fallback_ignores_user_message_content(self, app_client: TestClient) -> None:
        """Without explicit conversation IDs, each request gets a distinct session ID."""
        session_ids: list[str] = []

        original_derive = _derive_session_id

        def capture_session_id(*args: object, **kwargs: object) -> str:
            sid = original_derive(*args, **kwargs)
            session_ids.append(sid)
            return sid

        with (
            patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai,
            patch("mindroom.api.openai_compat._derive_session_id", side_effect=capture_session_id),
        ):
            mock_ai.return_value = "Response"

            # First request
            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
            # Second request with the same first message and extra history
            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi!"},
                        {"role": "user", "content": "Different follow-up"},
                    ],
                },
            )

        # Fallback sessions are intentionally distinct to avoid collisions.
        assert len(session_ids) == 2
        assert session_ids[0] != session_ids[1]


# ---------------------------------------------------------------------------
# Error detection
# ---------------------------------------------------------------------------


class TestErrorDetection:
    """Tests for _is_error_response()."""

    @pytest.mark.parametrize(
        "text",
        [
            "❌ Authentication failed (openai): Invalid API key",
            "⏱️ Rate limited. Please wait a moment and try again.",
            "⏰ Request timed out. Please try again.",
            "⚠️ Error: something went wrong",
        ],
    )
    def test_detects_error_prefixes(self, text: str) -> None:
        """Detects all error emoji prefixes."""
        assert _is_error_response(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "[general] ❌ Authentication failed",
            "[code] ⚠️ Error: model not available",
            "[research] ⏱️ Rate limited",
        ],
    )
    def test_detects_agent_prefix_errors(self, text: str) -> None:
        """Detects errors with [agent_name] prefix."""
        assert _is_error_response(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Error code: 404 - {'type': 'error', 'error': {'type': 'not_found_error', 'message': 'model: foo'}}",
            "Error code: 500 - Internal Server Error",
            "openai.NotFoundError: Error code: 404 - {'error': {'message': 'model not found'}}",
            '{"error": {"message": "model not found", "type": "not_found_error"}}',
        ],
    )
    def test_detects_raw_provider_errors(self, text: str) -> None:
        """Detects raw provider error strings surfaced by agno."""
        assert _is_error_response(text) is True

    def test_normal_response_not_error(self) -> None:
        """Normal response text is not detected as error."""
        assert _is_error_response("Hello! How can I help you?") is False

    def test_empty_string_not_error(self) -> None:
        """Empty string is not detected as error."""
        assert _is_error_response("") is False


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------


class TestContentExtraction:
    """Tests for _extract_content_text()."""

    def test_string_content(self) -> None:
        """String content is returned as-is."""
        assert _extract_content_text("Hello") == "Hello"

    def test_none_content(self) -> None:
        """None content returns empty string."""
        assert _extract_content_text(None) == ""

    def test_multimodal_content(self) -> None:
        """Multimodal content concatenates text parts."""
        content: list[dict] = [
            {"type": "text", "text": "First"},
            {"type": "image_url", "image_url": {"url": "..."}},
            {"type": "text", "text": "Second"},
        ]
        assert _extract_content_text(content) == "First Second"

    def test_empty_list(self) -> None:
        """Empty list returns empty string."""
        assert _extract_content_text([]) == ""

    def test_malformed_content_part(self) -> None:
        """Malformed content parts are skipped."""
        content: list[dict] = [
            {"type": "text"},  # missing "text" key
            {"type": "text", "text": "Valid"},
            "not a dict",
        ]
        assert _extract_content_text(content) == "Valid"

    def test_non_string_text_coerced(self) -> None:
        """Non-string text values are coerced to str."""
        content: list[dict] = [
            {"type": "text", "text": 123},
            {"type": "text", "text": "Hello"},
        ]
        assert _extract_content_text(content) == "123 Hello"


# ---------------------------------------------------------------------------
# Auto-routing (Phase 2)
# ---------------------------------------------------------------------------


class TestAutoRouting:
    """Tests for auto-routing via model='auto'."""

    def test_auto_routes_to_suggested_agent(self, app_client: TestClient) -> None:
        """Auto model routes to the agent suggested by suggest_agent()."""
        with (
            patch("mindroom.api.openai_compat.suggest_agent", new_callable=AsyncMock) as mock_route,
            patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai,
        ):
            mock_route.return_value = "code"
            mock_ai.return_value = "Here is your code"

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Write Python code"}],
                },
            )

        assert response.status_code == 200
        data = response.json()
        # Response model field shows the resolved agent, not "auto"
        assert data["model"] == "code"
        assert data["choices"][0]["message"]["content"] == "Here is your code"

    def test_auto_fallback_when_routing_fails(self, app_client: TestClient) -> None:
        """When suggest_agent returns None, falls back to first agent."""
        with (
            patch("mindroom.api.openai_compat.suggest_agent", new_callable=AsyncMock) as mock_route,
            patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai,
        ):
            mock_route.return_value = None
            mock_ai.return_value = "Fallback response"

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 200
        # Falls back to first agent in config (dict insertion order)
        assert response.json()["model"] == "general"

    def test_auto_passes_thread_history(self, app_client: TestClient) -> None:
        """Auto-routing passes thread_history to suggest_agent for context."""
        with (
            patch("mindroom.api.openai_compat.suggest_agent", new_callable=AsyncMock) as mock_route,
            patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai,
        ):
            mock_route.return_value = "general"
            mock_ai.return_value = "Response"

            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [
                        {"role": "user", "content": "Hi"},
                        {"role": "assistant", "content": "Hello!"},
                        {"role": "user", "content": "Write code"},
                    ],
                },
            )

            # suggest_agent should receive thread_history as 4th positional arg
            call_args = mock_route.call_args
            assert call_args[0][0] == "Write code"  # prompt
            thread_history = call_args[0][3]
            assert thread_history == [
                {"sender": "user", "body": "Hi"},
                {"sender": "assistant", "body": "Hello!"},
            ]

    def test_auto_streaming(self, app_client: TestClient) -> None:
        """Auto model works with streaming, chunks carry resolved agent name."""
        from agno.run.agent import RunContentEvent  # noqa: PLC0415

        async def mock_stream(**_kw: object) -> AsyncIterator[RunContentEvent]:
            yield RunContentEvent(content="Streamed!")

        with (
            patch("mindroom.api.openai_compat.suggest_agent", new_callable=AsyncMock) as mock_route,
            patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream),
        ):
            mock_route.return_value = "research"

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Research this"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        # Verify SSE chunks carry the resolved agent name, not "auto"
        lines = response.text.strip().split("\n\n")
        first_chunk = json.loads(lines[0].removeprefix("data: "))
        assert first_chunk["model"] == "research"

    def test_auto_no_agents_returns_500(self) -> None:
        """Auto with no configured agents returns 500."""
        from fastapi import FastAPI  # noqa: PLC0415

        from mindroom.api.openai_compat import router  # noqa: PLC0415

        app = FastAPI()
        app.include_router(router)

        empty_config = Config(
            agents={},
            models={"default": ModelConfig(provider="ollama", id="test")},
            router=RouterConfig(model="default"),
        )
        with (
            patch("mindroom.api.openai_compat._load_config", return_value=(empty_config, Path(__file__))),
            patch("mindroom.api.openai_compat.suggest_agent", new_callable=AsyncMock) as mock_route,
            patch.dict("os.environ", {"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"}),
        ):
            mock_route.return_value = None
            client = TestClient(app)
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 500
        assert "no agents" in response.json()["error"]["message"].lower()

    def test_auto_session_id_uses_resolved_agent(self, app_client: TestClient) -> None:
        """Session ID derivation uses the resolved agent name, not 'auto'."""
        with (
            patch("mindroom.api.openai_compat.suggest_agent", new_callable=AsyncMock) as mock_route,
            patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai,
        ):
            mock_route.return_value = "code"
            mock_ai.return_value = "Response"

            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Write code"}],
                },
                headers={"X-LibreChat-Conversation-Id": "conv-abc"},
            )

            # ai_response should receive agent_name="code", not "auto"
            assert mock_ai.call_args.kwargs["agent_name"] == "code"
            # Session ID should use the resolved model name with LibreChat IDs.
            session_id = mock_ai.call_args.kwargs["session_id"]
            assert session_id.endswith(":conv-abc:code")
            assert "auto" not in session_id

    def test_auto_routing_exception_falls_back(self, app_client: TestClient) -> None:
        """If suggest_agent raises an exception, it should still fall back gracefully."""
        with (
            patch("mindroom.api.openai_compat.suggest_agent", new_callable=AsyncMock) as mock_route,
            patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai,
        ):
            # suggest_agent catches exceptions internally and returns None
            mock_route.return_value = None
            mock_ai.return_value = "Fallback response"

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 200
        assert response.json()["model"] == "general"


# ---------------------------------------------------------------------------
# Team completion (Phase 3)
# ---------------------------------------------------------------------------


@pytest.fixture
def team_config() -> Config:
    """Create a test config with agents and a team."""
    return Config(
        agents={
            "general": AgentConfig(
                display_name="GeneralAgent",
                role="General-purpose assistant",
                rooms=[],
            ),
            "code": AgentConfig(
                display_name="CodeAgent",
                role="Generate code",
                rooms=[],
            ),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
        teams={
            "super_team": TeamConfig(
                display_name="Super Team",
                role="Collaborative engineering team",
                agents=["general", "code"],
                mode="coordinate",
            ),
        },
    )


@pytest.fixture
def team_app_client(team_config: Config) -> Iterator[TestClient]:
    """Create a FastAPI test client with team-enabled config."""
    from fastapi import FastAPI  # noqa: PLC0415

    from mindroom.api.openai_compat import router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)
    with (
        patch("mindroom.api.openai_compat._load_config", return_value=(team_config, Path(__file__))),
        patch.dict("os.environ", {"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"}),
    ):
        yield TestClient(app)


class TestTeamCompletion:
    """Tests for team model support (Phase 3)."""

    def test_team_listed_in_models(self, team_app_client: TestClient) -> None:
        """Teams appear in /v1/models with team/ prefix."""
        response = team_app_client.get("/v1/models")
        assert response.status_code == 200
        models = response.json()["data"]
        team_models = [m for m in models if m["id"].startswith("team/")]
        assert len(team_models) == 1
        assert team_models[0]["id"] == "team/super_team"
        assert team_models[0]["name"] == "Super Team"
        assert team_models[0]["description"] == "Collaborative engineering team"

    def test_unknown_team_404(self, team_app_client: TestClient) -> None:
        """Unknown team name returns 404."""
        response = team_app_client.post(
            "/v1/chat/completions",
            json={"model": "team/nonexistent", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert response.status_code == 404
        assert "nonexistent" in response.json()["error"]["message"]

    def test_team_non_streaming(self, team_app_client: TestClient) -> None:
        """Non-streaming team completion returns proper OpenAI response."""
        from agno.run.team import TeamRunOutput  # noqa: PLC0415

        from mindroom.teams import TeamMode  # noqa: PLC0415

        mock_team = MagicMock()
        mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="Team consensus result"))
        mock_agents = [MagicMock(name="GeneralAgent"), MagicMock(name="CodeAgent")]

        with patch(
            "mindroom.api.openai_compat._build_team",
            return_value=(mock_agents, mock_team, TeamMode.COORDINATE),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Build a feature"}],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["model"] == "team/super_team"
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["finish_reason"] == "stop"
        assert "Team consensus result" in data["choices"][0]["message"]["content"]

    def test_team_streaming(self, team_app_client: TestClient) -> None:
        """Streaming team completion returns SSE events."""
        from agno.run.team import RunContentEvent as TeamContentEvent  # noqa: PLC0415

        from mindroom.teams import TeamMode  # noqa: PLC0415

        mock_team = MagicMock()
        mock_agents = [MagicMock(name="GeneralAgent")]

        async def mock_stream_events(*_a: object, **_kw: object) -> AsyncIterator[object]:
            yield TeamContentEvent(content="Team ")
            yield TeamContentEvent(content="response!")

        mock_team.arun = mock_stream_events

        with patch(
            "mindroom.api.openai_compat._build_team",
            return_value=(mock_agents, mock_team, TeamMode.COORDINATE),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Build it"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        lines = response.text.strip().split("\n\n")
        # Role announcement + content chunks + finish + [DONE]
        assert len(lines) >= 4

        # First chunk is role announcement
        first = json.loads(lines[0].removeprefix("data: "))
        assert first["choices"][0]["delta"] == {"role": "assistant"}
        assert first["model"] == "team/super_team"

        # Verify content chunks contain the expected text
        content_parts = []
        for line in lines:
            if line.startswith("data: ") and line != "data: [DONE]":
                chunk = json.loads(line.removeprefix("data: "))
                delta = chunk["choices"][0]["delta"]
                if "content" in delta:
                    content_parts.append(delta["content"])
        assert "".join(content_parts) == "Team response!"

    def test_team_streaming_tool_events_emit_start_and_done_with_ids(
        self,
        team_app_client: TestClient,
    ) -> None:
        """Team streaming emits start/done tool blocks sharing the same stable id."""
        from agno.models.response import ToolExecution  # noqa: PLC0415
        from agno.run.team import RunContentEvent as TeamContentEvent  # noqa: PLC0415
        from agno.run.team import ToolCallCompletedEvent as TeamToolCallCompletedEvent  # noqa: PLC0415
        from agno.run.team import ToolCallStartedEvent as TeamToolCallStartedEvent  # noqa: PLC0415

        from mindroom.teams import TeamMode  # noqa: PLC0415

        tool_started = ToolExecution(
            tool_name="search",
            tool_args={"query": "X"},
            tool_call_id="tc-team-stream-1",
        )
        tool_completed = ToolExecution(
            tool_name="search",
            tool_args={"query": "X"},
            tool_call_id="tc-team-stream-1",
            result="3 results",
        )

        mock_team = MagicMock()
        mock_agents = [MagicMock(name="GeneralAgent")]

        async def mock_stream_events(*_a: object, **_kw: object) -> AsyncIterator[object]:
            yield TeamContentEvent(content="Team says: ")
            yield TeamToolCallStartedEvent(tool=tool_started)
            yield TeamToolCallCompletedEvent(tool=tool_completed)
            yield TeamContentEvent(content="done.")

        mock_team.arun = mock_stream_events

        with patch(
            "mindroom.api.openai_compat._build_team",
            return_value=(mock_agents, mock_team, TeamMode.COORDINATE),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Search for X"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        lines = response.text.strip().split("\n\n")
        contents: list[str] = []
        for line in lines:
            text = line.removeprefix("data: ")
            if text == "[DONE]":
                continue
            chunk = json.loads(text)
            delta = chunk["choices"][0]["delta"]
            if "content" in delta:
                contents.append(delta["content"])

        full_content = "".join(contents)
        assert "Team says: " in full_content
        assert '<tool id="1" state="start">search(query=X)</tool>' in full_content
        assert full_content.count('<tool id="1" state="done">search(query=X)\n3 results</tool>') == 1
        assert "done." in full_content

    def test_team_streaming_first_event_error_returns_500(self, team_app_client: TestClient) -> None:
        """Team stream returns HTTP 500 when first event is an explicit run error."""
        from agno.run.team import RunErrorEvent as TeamRunErrorEvent  # noqa: PLC0415

        from mindroom.teams import TeamMode  # noqa: PLC0415

        mock_team = MagicMock()
        mock_agents = [MagicMock(name="GeneralAgent")]

        async def mock_stream_events(*_a: object, **_kw: object) -> AsyncIterator[object]:
            yield TeamRunErrorEvent(content="Error code: 404 - {'error': {'message': 'model not found'}}")

        mock_team.arun = mock_stream_events

        with patch(
            "mindroom.api.openai_compat._build_team",
            return_value=(mock_agents, mock_team, TeamMode.COORDINATE),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Build it"}],
                    "stream": True,
                },
            )

        assert response.status_code == 500
        assert response.json()["error"]["type"] == "server_error"
        assert response.json()["error"]["message"] == "Team execution failed"

    def test_team_streaming_midstream_error_emits_failure_chunk(self, team_app_client: TestClient) -> None:
        """When stream error occurs after start, emit a failure chunk and finish stream."""
        from agno.run.team import RunContentEvent as TeamContentEvent  # noqa: PLC0415
        from agno.run.team import RunErrorEvent as TeamRunErrorEvent  # noqa: PLC0415

        from mindroom.teams import TeamMode  # noqa: PLC0415

        mock_team = MagicMock()
        mock_agents = [MagicMock(name="GeneralAgent")]

        async def mock_stream_events(*_a: object, **_kw: object) -> AsyncIterator[object]:
            yield TeamContentEvent(content="Team started. ")
            yield TeamRunErrorEvent(content="Error code: 500 - Internal Server Error")

        mock_team.arun = mock_stream_events

        with patch(
            "mindroom.api.openai_compat._build_team",
            return_value=(mock_agents, mock_team, TeamMode.COORDINATE),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Build it"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        lines = response.text.strip().split("\n\n")
        contents: list[str] = []
        for line in lines:
            if line.startswith("data: ") and line != "data: [DONE]":
                chunk = json.loads(line.removeprefix("data: "))
                delta = chunk["choices"][0]["delta"]
                if "content" in delta:
                    contents.append(delta["content"])

        assert "Team started. " in contents
        assert "Team execution failed." in contents
        assert lines[-1] == "data: [DONE]"

    def test_team_no_valid_agents_500(self, team_app_client: TestClient) -> None:
        """Team with no valid agents returns 500."""
        from mindroom.teams import TeamMode  # noqa: PLC0415

        # _build_team returns None for team when no agents created
        with patch(
            "mindroom.api.openai_compat._build_team",
            return_value=([], None, TeamMode.COORDINATE),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 500
        assert "no valid agents" in response.json()["error"]["message"].lower()

    def test_team_execution_failure_500(self, team_app_client: TestClient) -> None:
        """Team execution exception returns 500."""
        from mindroom.teams import TeamMode  # noqa: PLC0415

        mock_team = MagicMock()
        mock_team.arun = AsyncMock(side_effect=RuntimeError("Model error"))
        mock_agents = [MagicMock(name="GeneralAgent")]

        with patch(
            "mindroom.api.openai_compat._build_team",
            return_value=(mock_agents, mock_team, TeamMode.COORDINATE),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 500
        assert response.json()["error"]["type"] == "server_error"

    def test_team_streaming_execution_failure_500(self, team_app_client: TestClient) -> None:
        """Team streaming exceptions before first chunk return 500."""
        from mindroom.teams import TeamMode  # noqa: PLC0415

        mock_team = MagicMock()
        mock_team.arun = MagicMock(side_effect=RuntimeError("boom"))
        mock_agents = [MagicMock(name="GeneralAgent")]

        with patch(
            "mindroom.api.openai_compat._build_team",
            return_value=(mock_agents, mock_team, TeamMode.COORDINATE),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            )

        assert response.status_code == 500
        assert response.json()["error"]["type"] == "server_error"

    def test_team_non_streaming_includes_thread_history(self, team_app_client: TestClient) -> None:
        """Team prompt includes prior messages converted from request history."""
        from agno.run.team import TeamRunOutput  # noqa: PLC0415

        from mindroom.teams import TeamMode  # noqa: PLC0415

        mock_team = MagicMock()
        mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="ok"))
        mock_agents = [MagicMock(name="GeneralAgent"), MagicMock(name="CodeAgent")]

        with patch(
            "mindroom.api.openai_compat._build_team",
            return_value=(mock_agents, mock_team, TeamMode.COORDINATE),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [
                        {"role": "user", "content": "Start"},
                        {"role": "assistant", "content": "Ack"},
                        {"role": "user", "content": "Follow-up"},
                    ],
                },
            )

        assert response.status_code == 200
        prompt = mock_team.arun.call_args.args[0]
        assert "Previous conversation in this thread:" in prompt
        assert "user: Start" in prompt
        assert "assistant: Ack" in prompt
        assert "Current message:\nFollow-up" in prompt

    def test_collaborate_mode_delegates_to_all(self) -> None:
        """Collaborate mode sets delegate_to_all_members=True on Team."""
        collaborate_config = Config(
            agents={
                "general": AgentConfig(display_name="GeneralAgent", role="General", rooms=[]),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
            teams={
                "collab_team": TeamConfig(
                    display_name="Collab Team",
                    role="Collaborative team",
                    agents=["general"],
                    mode="collaborate",
                ),
            },
        )
        with (
            patch("mindroom.api.openai_compat.create_agent") as mock_create,
            patch("mindroom.api.openai_compat.get_model_instance"),
            patch("agno.team.Team.__init__", return_value=None) as mock_team_init,
        ):
            mock_create.return_value = MagicMock(name="GeneralAgent")

            from mindroom.api.openai_compat import _build_team  # noqa: PLC0415

            _build_team("collab_team", collaborate_config)

            mock_team_init.assert_called_once()
            assert mock_team_init.call_args.kwargs["delegate_to_all_members"] is True

    def test_coordinate_mode_no_delegate_all(self) -> None:
        """Coordinate mode sets delegate_to_all_members=False on Team."""
        with (
            patch("mindroom.api.openai_compat.create_agent") as mock_create,
            patch("mindroom.api.openai_compat.get_model_instance"),
            patch("agno.team.Team.__init__", return_value=None) as mock_team_init,
        ):
            mock_create.return_value = MagicMock(name="GeneralAgent")

            from mindroom.api.openai_compat import _build_team  # noqa: PLC0415

            # team_config fixture uses coordinate mode
            config = Config(
                agents={"general": AgentConfig(display_name="GeneralAgent", role="General", rooms=[])},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
                teams={
                    "coord_team": TeamConfig(
                        display_name="Coord Team",
                        role="Coordinated team",
                        agents=["general"],
                        mode="coordinate",
                    ),
                },
            )
            _build_team("coord_team", config)

            mock_team_init.assert_called_once()
            assert mock_team_init.call_args.kwargs["delegate_to_all_members"] is False

    def test_build_team_passes_knowledge_to_member_agents(self) -> None:
        """Team member creation resolves and passes configured knowledge."""
        from mindroom.config import KnowledgeBaseConfig  # noqa: PLC0415

        config = Config(
            agents={
                "research": AgentConfig(
                    display_name="Research",
                    role="Research role",
                    rooms=[],
                    knowledge_bases=["docs"],
                ),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
            teams={
                "team_with_kb": TeamConfig(
                    display_name="KB Team",
                    role="Team with KB",
                    agents=["research"],
                    mode="coordinate",
                ),
            },
            knowledge_bases={"docs": KnowledgeBaseConfig(path="./docs")},
        )
        mock_knowledge = MagicMock()
        mock_manager = MagicMock()
        mock_manager.get_knowledge.return_value = mock_knowledge

        with (
            patch("mindroom.api.openai_compat.create_agent") as mock_create,
            patch("mindroom.api.openai_compat.get_model_instance"),
            patch("mindroom.api.openai_compat.get_knowledge_manager", return_value=mock_manager),
            patch("agno.team.Team.__init__", return_value=None),
        ):
            mock_create.return_value = MagicMock(name="Research")

            from mindroom.api.openai_compat import _build_team  # noqa: PLC0415

            _build_team("team_with_kb", config)

            assert mock_create.call_args.kwargs["knowledge"] is mock_knowledge
            assert mock_create.call_args.kwargs["include_interactive_questions"] is False


# ---------------------------------------------------------------------------
# Knowledge base integration (Phase 4)
# ---------------------------------------------------------------------------


@pytest.fixture
def knowledge_config() -> Config:
    """Config with an agent that has knowledge_bases assigned."""
    from mindroom.config import KnowledgeBaseConfig  # noqa: PLC0415

    return Config(
        agents={
            "general": AgentConfig(
                display_name="GeneralAgent",
                role="General-purpose assistant",
                rooms=[],
            ),
            "research": AgentConfig(
                display_name="ResearchAgent",
                role="Research assistant with knowledge base",
                rooms=[],
                knowledge_bases=["docs"],
            ),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
        knowledge_bases={
            "docs": KnowledgeBaseConfig(path="./test_docs"),
        },
    )


@pytest.fixture
def knowledge_app_client(knowledge_config: Config) -> Iterator[TestClient]:
    """Create a FastAPI test client with knowledge-enabled config."""
    from fastapi import FastAPI  # noqa: PLC0415

    from mindroom.api.openai_compat import router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)
    with (
        patch("mindroom.api.openai_compat._load_config", return_value=(knowledge_config, Path(__file__))),
        patch.dict("os.environ", {"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"}),
    ):
        yield TestClient(app)


class TestKnowledgeIntegration:
    """Tests for knowledge base integration (Phase 4)."""

    def test_knowledge_passed_when_configured(self, knowledge_app_client: TestClient) -> None:
        """Knowledge is passed to ai_response when agent has knowledge_bases."""
        mock_knowledge = MagicMock()
        mock_manager = MagicMock()
        mock_manager.get_knowledge.return_value = mock_knowledge

        with (
            patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai,
            patch("mindroom.api.openai_compat.initialize_knowledge_managers", new_callable=AsyncMock),
            patch("mindroom.api.openai_compat.get_knowledge_manager", return_value=mock_manager),
        ):
            mock_ai.return_value = "Response with knowledge"

            response = knowledge_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "research",
                    "messages": [{"role": "user", "content": "What do the docs say?"}],
                },
            )

        assert response.status_code == 200
        assert mock_ai.call_args.kwargs["knowledge"] is mock_knowledge

    def test_knowledge_none_when_not_configured(self, knowledge_app_client: TestClient) -> None:
        """Knowledge is None when agent has no knowledge_bases."""
        with (
            patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai,
            patch("mindroom.api.openai_compat.initialize_knowledge_managers", new_callable=AsyncMock),
        ):
            mock_ai.return_value = "Response"

            knowledge_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert mock_ai.call_args.kwargs["knowledge"] is None

    def test_knowledge_initialization_called(self, knowledge_app_client: TestClient) -> None:
        """_ensure_knowledge_initialized is called for configs with knowledge_bases."""
        with (
            patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai,
            patch("mindroom.api.openai_compat.initialize_knowledge_managers", new_callable=AsyncMock) as mock_init,
        ):
            mock_ai.return_value = "Response"

            knowledge_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        mock_init.assert_called_once()

    def test_knowledge_unavailable_returns_none(self, knowledge_app_client: TestClient) -> None:
        """When knowledge manager is not found, knowledge is None."""
        with (
            patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai,
            patch("mindroom.api.openai_compat.initialize_knowledge_managers", new_callable=AsyncMock),
            patch("mindroom.api.openai_compat.get_knowledge_manager", return_value=None),
        ):
            mock_ai.return_value = "Response without knowledge"

            response = knowledge_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "research",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 200
        assert mock_ai.call_args.kwargs["knowledge"] is None

    def test_streaming_with_knowledge(self, knowledge_app_client: TestClient) -> None:
        """Knowledge is passed through in streaming mode too."""
        from agno.run.agent import RunContentEvent  # noqa: PLC0415

        mock_knowledge = MagicMock()
        mock_manager = MagicMock()
        mock_manager.get_knowledge.return_value = mock_knowledge

        async def mock_stream(**_kw: object) -> AsyncIterator[RunContentEvent]:
            yield RunContentEvent(content="Streamed!")

        with (
            patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream) as mock_stream_fn,
            patch("mindroom.api.openai_compat.initialize_knowledge_managers", new_callable=AsyncMock),
            patch("mindroom.api.openai_compat.get_knowledge_manager", return_value=mock_manager),
        ):
            response = knowledge_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "research",
                    "messages": [{"role": "user", "content": "Stream with knowledge"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert mock_stream_fn.call_args.kwargs["knowledge"] is mock_knowledge

    def test_multi_knowledge_bases_merged(self, knowledge_config: Config) -> None:
        """Agent with multiple knowledge_bases gets a merged Knowledge object."""
        from fastapi import FastAPI  # noqa: PLC0415

        from mindroom.api.openai_compat import router  # noqa: PLC0415
        from mindroom.config import KnowledgeBaseConfig  # noqa: PLC0415

        # Add a second knowledge base and assign both to the research agent
        knowledge_config.knowledge_bases["wiki"] = KnowledgeBaseConfig(path="./test_wiki")
        knowledge_config.agents["research"].knowledge_bases = ["docs", "wiki"]

        app = FastAPI()
        app.include_router(router)

        mock_manager_docs = MagicMock()
        mock_knowledge_docs = MagicMock()
        mock_knowledge_docs.vector_db = MagicMock()
        mock_knowledge_docs.max_results = 5
        mock_manager_docs.get_knowledge.return_value = mock_knowledge_docs

        mock_manager_wiki = MagicMock()
        mock_knowledge_wiki = MagicMock()
        mock_knowledge_wiki.vector_db = MagicMock()
        mock_knowledge_wiki.max_results = 10
        mock_manager_wiki.get_knowledge.return_value = mock_knowledge_wiki

        def fake_get_manager(base_id: str) -> MagicMock | None:
            return {"docs": mock_manager_docs, "wiki": mock_manager_wiki}.get(base_id)

        with (
            patch("mindroom.api.openai_compat._load_config", return_value=(knowledge_config, Path(__file__))),
            patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai,
            patch("mindroom.api.openai_compat.initialize_knowledge_managers", new_callable=AsyncMock),
            patch("mindroom.api.openai_compat.get_knowledge_manager", side_effect=fake_get_manager),
            patch.dict("os.environ", {"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"}),
        ):
            mock_ai.return_value = "Merged knowledge response"

            client = TestClient(app)
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "research",
                    "messages": [{"role": "user", "content": "Multi-KB query"}],
                },
            )

        assert response.status_code == 200
        knowledge_arg = mock_ai.call_args.kwargs["knowledge"]
        assert knowledge_arg is not None
        # Should be a merged Knowledge with MultiKnowledgeVectorDb
        from mindroom.knowledge_utils import MultiKnowledgeVectorDb  # noqa: PLC0415

        assert isinstance(knowledge_arg.vector_db, MultiKnowledgeVectorDb)
        assert knowledge_arg.max_results == 10  # max(5, 10)

    def test_knowledge_init_failure_graceful_fallback(self, knowledge_app_client: TestClient) -> None:
        """When knowledge initialization fails, request proceeds with knowledge=None."""
        with (
            patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai,
            patch(
                "mindroom.api.openai_compat.initialize_knowledge_managers",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB connection failed"),
            ),
        ):
            mock_ai.return_value = "Response without knowledge"

            response = knowledge_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "research",
                    "messages": [{"role": "user", "content": "Query with broken KB"}],
                },
            )

        assert response.status_code == 200
        assert mock_ai.call_args.kwargs["knowledge"] is None
        data = response.json()
        assert data["choices"][0]["message"]["content"] == "Response without knowledge"


def _extract_sse_data_events(response: object) -> list[str]:
    """Extract SSE data payloads from a response."""
    events: list[str] = []
    for block in response.text.strip().split("\n\n"):
        if not block.strip():
            continue
        data_lines = [line.removeprefix("data:").lstrip() for line in block.splitlines() if line.startswith("data:")]
        if data_lines:
            events.append("\n".join(data_lines))
    return events


def _extract_sse_contents(response: object) -> list[str]:
    """Extract content strings from an SSE streaming response."""
    contents: list[str] = []
    for text in _extract_sse_data_events(response):
        if text == "[DONE]":
            continue
        chunk = json.loads(text)
        delta = chunk["choices"][0]["delta"]
        if "content" in delta:
            contents.append(delta["content"])
    return contents


def _extract_sse_chunks(response: object) -> list[dict]:
    """Extract parsed JSON SSE chunks (excluding [DONE])."""
    chunks: list[dict] = []
    for text in _extract_sse_data_events(response):
        if text == "[DONE]":
            continue
        chunks.append(json.loads(text))
    return chunks


class TestStrictOpenAIToolCalling:
    """Tests for X-Tool-Event-Format: openai strict tool-calling mode."""

    @pytest.fixture(autouse=True)
    def _clear_pending_openai_state(self) -> Iterator[None]:
        openai_compat_module._openai_state.pending_runs.clear()
        openai_compat_module._openai_state.session_locks.clear()
        openai_compat_module._openai_state.active_stream_sessions.clear()
        yield
        openai_compat_module._openai_state.pending_runs.clear()
        openai_compat_module._openai_state.session_locks.clear()
        openai_compat_module._openai_state.active_stream_sessions.clear()

    @staticmethod
    def _external_requirement(
        *,
        tool_call_id: str | None,
        tool_name: str = "search",
        tool_args: dict[str, object] | None = None,
    ) -> RunRequirement:
        return RunRequirement(
            tool_execution=ToolExecution(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                tool_args=tool_args or {"q": "test"},
                external_execution_required=True,
            ),
        )

    def test_openai_mode_requires_session_id(self, app_client: TestClient) -> None:
        """Strict openai mode requires X-Session-Id for continuation state."""
        response = app_client.post(
            "/v1/chat/completions",
            json={
                "model": "general",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            headers={"X-Tool-Event-Format": "openai"},
        )
        assert response.status_code == 400
        assert "X-Session-Id" in response.json()["error"]["message"]

    def test_openai_mode_rejects_team_models(self, team_app_client: TestClient) -> None:
        """Strict openai mode is limited to single-agent models."""
        response = team_app_client.post(
            "/v1/chat/completions",
            json={
                "model": "team/super_team",
                "messages": [{"role": "user", "content": "Hello team"}],
            },
            headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-1"},
        )
        assert response.status_code == 400
        assert "single-agent" in response.json()["error"]["message"]

    def test_openai_mode_rejects_when_stream_is_active(self, app_client: TestClient) -> None:
        """Strict mode returns 409 when another stream is active for the session."""
        openai_compat_module._openai_state.active_stream_sessions.add("noauth:sess-busy")
        response = app_client.post(
            "/v1/chat/completions",
            json={
                "model": "general",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-busy"},
        )
        assert response.status_code == 409
        assert "already active" in response.json()["error"]["message"]

    def test_openai_mode_non_stream_returns_tool_calls(self, app_client: TestClient) -> None:
        """Paused non-stream runs return OpenAI tool_calls payloads."""
        requirement = self._external_requirement(tool_call_id="call_123")
        paused_run = RunOutput(run_id="run-1", status=RunStatus.paused, requirements=[requirement])
        mock_agent = MagicMock()
        mock_agent.arun = AsyncMock(return_value=paused_run)

        with (
            patch("mindroom.api.openai_compat.create_agent", return_value=mock_agent),
            patch(
                "mindroom.api.openai_compat.build_memory_enhanced_prompt",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
        ):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-2"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["choices"][0]["finish_reason"] == "tool_calls"
        tool_call = data["choices"][0]["message"]["tool_calls"][0]
        assert tool_call["id"] == "call_123"
        assert tool_call["function"]["name"] == "search"
        assert json.loads(tool_call["function"]["arguments"]) == {"q": "test"}

    def test_openai_mode_continuation_resumes_with_tool_result(self, app_client: TestClient) -> None:
        """Tool result messages resume paused runs and produce final assistant text."""
        requirement = self._external_requirement(tool_call_id="call_abc")
        paused_run = RunOutput(run_id="run-abc", status=RunStatus.paused, requirements=[requirement])
        completed_run = RunOutput(run_id="run-abc", status=RunStatus.completed, content="Final answer")

        first_agent = MagicMock()
        first_agent.arun = AsyncMock(return_value=paused_run)
        second_agent = MagicMock()
        second_agent.acontinue_run = AsyncMock(return_value=completed_run)

        with (
            patch("mindroom.api.openai_compat.create_agent", side_effect=[first_agent, second_agent]),
            patch(
                "mindroom.api.openai_compat.build_memory_enhanced_prompt",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
        ):
            first_response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-3"},
            )
            assert first_response.status_code == 200
            assert first_response.json()["choices"][0]["finish_reason"] == "tool_calls"

            second_response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "tool", "tool_call_id": "call_abc", "content": "tool result"},
                    ],
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-3"},
            )

        assert second_response.status_code == 200
        assert second_response.json()["choices"][0]["message"]["content"] == "Final answer"
        requirements = second_agent.acontinue_run.call_args.kwargs["requirements"]
        assert requirements[0].external_execution_result == "tool result"

    def test_openai_mode_missing_tool_result_errors(self, app_client: TestClient) -> None:
        """Continuation without required role=tool results returns HTTP 400."""
        requirement = self._external_requirement(tool_call_id="call_miss")
        paused_run = RunOutput(run_id="run-miss", status=RunStatus.paused, requirements=[requirement])
        first_agent = MagicMock()
        first_agent.arun = AsyncMock(return_value=paused_run)

        with (
            patch("mindroom.api.openai_compat.create_agent", return_value=first_agent),
            patch(
                "mindroom.api.openai_compat.build_memory_enhanced_prompt",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
        ):
            first_response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-4"},
            )
            assert first_response.status_code == 200

            second_response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-4"},
            )

        assert second_response.status_code == 400
        assert "Tool result messages are required" in second_response.json()["error"]["message"]

    def test_openai_mode_synthesizes_missing_tool_call_id(self, app_client: TestClient) -> None:
        """Missing upstream tool_call_id values are synthesized as call_* IDs."""
        requirement = self._external_requirement(tool_call_id=None)
        paused_run = RunOutput(run_id="run-synth", status=RunStatus.paused, requirements=[requirement])
        mock_agent = MagicMock()
        mock_agent.arun = AsyncMock(return_value=paused_run)

        with (
            patch("mindroom.api.openai_compat.create_agent", return_value=mock_agent),
            patch(
                "mindroom.api.openai_compat.build_memory_enhanced_prompt",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
        ):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-5"},
            )

        assert response.status_code == 200
        tool_call = response.json()["choices"][0]["message"]["tool_calls"][0]
        assert tool_call["id"].startswith("call_")

    def test_openai_mode_streaming_emits_delta_tool_calls(self, app_client: TestClient) -> None:
        """Streaming strict mode emits delta.tool_calls and tool_calls finish reason."""
        requirement = self._external_requirement(tool_call_id="call_stream")
        mock_agent = MagicMock()

        async def mock_stream(*_args: object, **_kwargs: object) -> AsyncIterator[RunPausedEvent]:
            yield RunPausedEvent(run_id="run-stream", requirements=[requirement])

        mock_agent.arun = mock_stream

        with (
            patch("mindroom.api.openai_compat.create_agent", return_value=mock_agent),
            patch(
                "mindroom.api.openai_compat.build_memory_enhanced_prompt",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
        ):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-6"},
            )

        assert response.status_code == 200
        chunks = _extract_sse_chunks(response)
        assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
        tool_chunks = [chunk for chunk in chunks if "tool_calls" in chunk["choices"][0]["delta"]]
        assert len(tool_chunks) == 1
        tool_call = tool_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        assert tool_call["id"] == "call_stream"
        assert tool_call["function"]["name"] == "search"
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"

    def test_openai_mode_streaming_continuation_resumes_with_tool_result(self, app_client: TestClient) -> None:
        """Streaming continuation resumes paused runs and emits final assistant text."""
        requirement = self._external_requirement(tool_call_id="call_stream_resume")
        first_agent = MagicMock()
        second_agent = MagicMock()

        async def first_stream(*_args: object, **_kwargs: object) -> AsyncIterator[RunPausedEvent]:
            yield RunPausedEvent(run_id="run-stream-resume", requirements=[requirement])

        async def continuation_stream() -> AsyncIterator[RunContentEvent]:
            yield RunContentEvent(content="Final streamed answer")

        second_agent.acontinue_run = MagicMock(side_effect=lambda *_a, **_kw: continuation_stream())
        first_agent.arun = first_stream

        with (
            patch("mindroom.api.openai_compat.create_agent", side_effect=[first_agent, second_agent]),
            patch(
                "mindroom.api.openai_compat.build_memory_enhanced_prompt",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
        ):
            first_response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-7"},
            )
            assert first_response.status_code == 200
            first_chunks = _extract_sse_chunks(first_response)
            assert first_chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"

            second_response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {
                            "role": "tool",
                            "tool_call_id": "call_stream_resume",
                            "content": "stream tool result",
                        },
                    ],
                    "stream": True,
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-7"},
            )

        assert second_response.status_code == 200
        chunks = _extract_sse_chunks(second_response)
        assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
        contents = [
            chunk["choices"][0]["delta"]["content"] for chunk in chunks if "content" in chunk["choices"][0]["delta"]
        ]
        assert "".join(contents) == "Final streamed answer"
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        requirements = second_agent.acontinue_run.call_args.kwargs["requirements"]
        assert requirements[0].external_execution_result == "stream tool result"

    def test_openai_mode_streaming_missing_tool_result_errors(self, app_client: TestClient) -> None:
        """Streaming continuation preflight returns HTTP 400 when tool results are missing."""
        requirement = self._external_requirement(tool_call_id="call_stream_missing")
        first_agent = MagicMock()

        async def first_stream(*_args: object, **_kwargs: object) -> AsyncIterator[RunPausedEvent]:
            yield RunPausedEvent(run_id="run-stream-missing", requirements=[requirement])

        first_agent.arun = first_stream

        with (
            patch("mindroom.api.openai_compat.create_agent", return_value=first_agent),
            patch(
                "mindroom.api.openai_compat.build_memory_enhanced_prompt",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
        ):
            first_response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-8"},
            )
            assert first_response.status_code == 200

            second_response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-8"},
            )

        assert second_response.status_code == 400
        assert "Tool result messages are required" in second_response.json()["error"]["message"]

    def test_openai_mode_cleanup_prunes_expired_pending_and_orphan_locks(self) -> None:
        """Strict-mode cleanup removes expired pending runs and idle orphan session locks."""
        stale_requirement = self._external_requirement(tool_call_id="call_stale")
        fresh_requirement = self._external_requirement(tool_call_id="call_fresh")

        openai_compat_module._openai_state.pending_runs["sess-expired"] = openai_compat_module._PendingOpenAIRun(
            run_id="run-expired",
            agent_name="general",
            session_id="sess-expired",
            requirements=[stale_requirement],
            created_at=0,
        )
        openai_compat_module._openai_state.pending_runs["sess-fresh"] = openai_compat_module._PendingOpenAIRun(
            run_id="run-fresh",
            agent_name="general",
            session_id="sess-fresh",
            requirements=[fresh_requirement],
            created_at=time.time(),
        )
        openai_compat_module._openai_state.session_locks["sess-expired"] = asyncio.Lock()
        openai_compat_module._openai_state.session_locks["sess-fresh"] = asyncio.Lock()
        openai_compat_module._openai_state.session_locks["sess-orphan"] = asyncio.Lock()

        openai_compat_module._cleanup_expired_openai_runs()

        assert "sess-expired" not in openai_compat_module._openai_state.pending_runs
        assert "sess-fresh" in openai_compat_module._openai_state.pending_runs
        assert "sess-orphan" not in openai_compat_module._openai_state.session_locks

    def test_openai_mode_auto_model_continues_with_pending_agent(self, app_client: TestClient) -> None:
        """model=auto on continuation uses the pending run's agent instead of re-routing."""
        requirement = self._external_requirement(tool_call_id="call_auto")
        paused_run = RunOutput(run_id="run-auto", status=RunStatus.paused, requirements=[requirement])
        completed_run = RunOutput(run_id="run-auto", status=RunStatus.completed, content="Done")

        first_agent = MagicMock()
        first_agent.arun = AsyncMock(return_value=paused_run)
        second_agent = MagicMock()
        second_agent.acontinue_run = AsyncMock(return_value=completed_run)

        with (
            patch("mindroom.api.openai_compat.create_agent", side_effect=[first_agent, second_agent]),
            patch(
                "mindroom.api.openai_compat.build_memory_enhanced_prompt",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
            patch("mindroom.api.openai_compat.suggest_agent", new_callable=AsyncMock, return_value="general"),
        ):
            # First request with model=auto resolves to "general"
            first_response = app_client.post(
                "/v1/chat/completions",
                json={"model": "auto", "messages": [{"role": "user", "content": "Hello"}]},
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-auto"},
            )
            assert first_response.status_code == 200
            assert first_response.json()["choices"][0]["finish_reason"] == "tool_calls"

            # Continuation with model=auto (could resolve differently) should still use "general"
            second_response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "tool", "tool_call_id": "call_auto", "content": "result"},
                    ],
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-auto"},
            )

        assert second_response.status_code == 200
        assert second_response.json()["choices"][0]["message"]["content"] == "Done"

    def test_openai_mode_explicit_model_mismatch_still_errors(self, app_client: TestClient) -> None:
        """Continuation with an explicit different model returns 400 mismatch."""
        requirement = self._external_requirement(tool_call_id="call_model_mismatch")
        paused_run = RunOutput(run_id="run-model-mismatch", status=RunStatus.paused, requirements=[requirement])
        completed_run = RunOutput(run_id="run-model-mismatch", status=RunStatus.completed, content="Done")

        first_agent = MagicMock()
        first_agent.arun = AsyncMock(return_value=paused_run)
        second_agent = MagicMock()
        second_agent.acontinue_run = AsyncMock(return_value=completed_run)

        with (
            patch("mindroom.api.openai_compat.create_agent", side_effect=[first_agent, second_agent]),
            patch(
                "mindroom.api.openai_compat.build_memory_enhanced_prompt",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
        ):
            first_response = app_client.post(
                "/v1/chat/completions",
                json={"model": "general", "messages": [{"role": "user", "content": "Hello"}]},
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-model-mismatch"},
            )
            assert first_response.status_code == 200
            assert first_response.json()["choices"][0]["finish_reason"] == "tool_calls"

            second_response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "code",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "tool", "tool_call_id": "call_model_mismatch", "content": "result"},
                    ],
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-model-mismatch"},
            )

        assert second_response.status_code == 400
        assert "different model" in second_response.json()["error"]["message"]

    def test_openai_mode_pending_run_does_not_bypass_team_rejection(self, team_app_client: TestClient) -> None:
        """Pending strict state must not allow team/* continuations in openai mode."""
        requirement = self._external_requirement(tool_call_id="call_team_block")
        paused_run = RunOutput(run_id="run-team-block", status=RunStatus.paused, requirements=[requirement])
        completed_run = RunOutput(run_id="run-team-block", status=RunStatus.completed, content="Done")

        first_agent = MagicMock()
        first_agent.arun = AsyncMock(return_value=paused_run)
        second_agent = MagicMock()
        second_agent.acontinue_run = AsyncMock(return_value=completed_run)

        with (
            patch("mindroom.api.openai_compat.create_agent", side_effect=[first_agent, second_agent]),
            patch(
                "mindroom.api.openai_compat.build_memory_enhanced_prompt",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
        ):
            first_response = team_app_client.post(
                "/v1/chat/completions",
                json={"model": "general", "messages": [{"role": "user", "content": "Hello"}]},
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-team-block"},
            )
            assert first_response.status_code == 200
            assert first_response.json()["choices"][0]["finish_reason"] == "tool_calls"

            second_response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "tool", "tool_call_id": "call_team_block", "content": "result"},
                    ],
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-team-block"},
            )

        assert second_response.status_code == 400
        assert "single-agent" in second_response.json()["error"]["message"]
        second_agent.acontinue_run.assert_not_called()

    def test_openai_mode_rejects_unknown_tool_call_ids(self, app_client: TestClient) -> None:
        """Extra tool_call_id results not in pending requirements return 400."""
        requirement = self._external_requirement(tool_call_id="call_expected")
        paused_run = RunOutput(run_id="run-extra", status=RunStatus.paused, requirements=[requirement])
        first_agent = MagicMock()
        first_agent.arun = AsyncMock(return_value=paused_run)

        with (
            patch("mindroom.api.openai_compat.create_agent", return_value=first_agent),
            patch(
                "mindroom.api.openai_compat.build_memory_enhanced_prompt",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
        ):
            first_response = app_client.post(
                "/v1/chat/completions",
                json={"model": "general", "messages": [{"role": "user", "content": "Hello"}]},
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-extra"},
            )
            assert first_response.status_code == 200

            second_response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "tool", "tool_call_id": "call_expected", "content": "ok"},
                        {"role": "tool", "tool_call_id": "call_bogus", "content": "extra"},
                    ],
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-extra"},
            )

        assert second_response.status_code == 400
        assert "Unknown tool_call_id" in second_response.json()["error"]["message"]
        assert "call_bogus" in second_response.json()["error"]["message"]

    def test_openai_mode_accepts_empty_string_tool_result(self, app_client: TestClient) -> None:
        """Empty-string tool result content is accepted (not treated as missing)."""
        requirement = self._external_requirement(tool_call_id="call_empty")
        paused_run = RunOutput(run_id="run-empty", status=RunStatus.paused, requirements=[requirement])
        completed_run = RunOutput(run_id="run-empty", status=RunStatus.completed, content="Done")

        first_agent = MagicMock()
        first_agent.arun = AsyncMock(return_value=paused_run)
        second_agent = MagicMock()
        second_agent.acontinue_run = AsyncMock(return_value=completed_run)

        with (
            patch("mindroom.api.openai_compat.create_agent", side_effect=[first_agent, second_agent]),
            patch(
                "mindroom.api.openai_compat.build_memory_enhanced_prompt",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
        ):
            first_response = app_client.post(
                "/v1/chat/completions",
                json={"model": "general", "messages": [{"role": "user", "content": "Hello"}]},
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-empty"},
            )
            assert first_response.status_code == 200

            second_response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "tool", "tool_call_id": "call_empty", "content": ""},
                    ],
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-empty"},
            )

        assert second_response.status_code == 200
        assert second_response.json()["choices"][0]["message"]["content"] == "Done"
        requirements = second_agent.acontinue_run.call_args.kwargs["requirements"]
        assert requirements[0].external_execution_result == ""

    def test_openai_mode_streaming_unsupported_pause_returns_400(self, app_client: TestClient) -> None:
        """Streaming: non-tool pause as first event returns HTTP 400, not SSE error chunk."""
        non_tool_pause = RunPausedEvent(
            run_id="run-nontool",
            requirements=[],
        )

        agent = MagicMock()

        async def _fake_stream() -> AsyncIterator[RunPausedEvent]:
            yield non_tool_pause

        agent.arun = MagicMock(return_value=_fake_stream())

        with (
            patch("mindroom.api.openai_compat.create_agent", return_value=agent),
            patch(
                "mindroom.api.openai_compat.build_memory_enhanced_prompt",
                new_callable=AsyncMock,
                return_value="Hello",
            ),
        ):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
                headers={"X-Tool-Event-Format": "openai", "X-Session-Id": "sess-nontool"},
            )

        assert response.status_code == 400
        assert "external tool execution" in response.json()["error"]["message"]


# ---------------------------------------------------------------------------
# POST /v1/tools/execute
# ---------------------------------------------------------------------------


class TestToolExecute:
    """Tests for POST /v1/tools/execute endpoint."""

    def test_execute_happy_path(self, app_client: TestClient) -> None:
        """Successful tool execution returns result with tool_call_id."""
        mock_fn = MagicMock()
        mock_fn.name = "web_search"
        mock_fn.entrypoint = AsyncMock(return_value="3 results found")

        mock_agent = MagicMock()
        mock_agent.tools = [mock_fn]

        mock_exec_result = MagicMock()
        mock_exec_result.status = "success"
        mock_exec_result.result = "3 results found"

        with (
            patch("mindroom.api.openai_compat.create_agent", return_value=mock_agent),
            patch("mindroom.api.openai_compat._find_function_on_agent", return_value=mock_fn),
            patch("mindroom.api.openai_compat.FunctionCall") as mock_fc_cls,
        ):
            mock_fc = MagicMock()
            mock_fc.aexecute = AsyncMock(return_value=mock_exec_result)
            mock_fc_cls.return_value = mock_fc

            response = app_client.post(
                "/v1/tools/execute",
                json={
                    "agent": "general",
                    "tool_name": "web_search",
                    "arguments": {"query": "test"},
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["tool_call_id"].startswith("call_")
        assert data["result"] == "3 results found"

    def test_execute_unknown_agent(self, app_client: TestClient) -> None:
        """Unknown agent returns 404."""
        response = app_client.post(
            "/v1/tools/execute",
            json={
                "agent": "nonexistent",
                "tool_name": "search",
                "arguments": {},
            },
        )
        assert response.status_code == 404
        assert "nonexistent" in response.json()["error"]["message"]

    def test_execute_unknown_tool(self, app_client: TestClient) -> None:
        """Unknown tool on a valid agent returns 404."""
        mock_agent = MagicMock()
        mock_agent.tools = []

        with patch("mindroom.api.openai_compat.create_agent", return_value=mock_agent):
            response = app_client.post(
                "/v1/tools/execute",
                json={
                    "agent": "general",
                    "tool_name": "nonexistent_tool",
                    "arguments": {},
                },
            )

        assert response.status_code == 404
        assert "nonexistent_tool" in response.json()["error"]["message"]

    def test_execute_auth_required(self, authed_client: TestClient) -> None:
        """Auth is checked on tool execution endpoint."""
        response = authed_client.post(
            "/v1/tools/execute",
            json={
                "agent": "general",
                "tool_name": "search",
                "arguments": {},
            },
        )
        assert response.status_code == 401

    def test_execute_auth_accepted(self, authed_client: TestClient) -> None:
        """Valid auth allows tool execution."""
        mock_fn = MagicMock()
        mock_fn.name = "web_search"

        mock_agent = MagicMock()
        mock_agent.tools = [mock_fn]

        mock_exec_result = MagicMock()
        mock_exec_result.status = "success"
        mock_exec_result.result = "done"

        with (
            patch("mindroom.api.openai_compat.create_agent", return_value=mock_agent),
            patch("mindroom.api.openai_compat._find_function_on_agent", return_value=mock_fn),
            patch("mindroom.api.openai_compat.FunctionCall") as mock_fc_cls,
        ):
            mock_fc = MagicMock()
            mock_fc.aexecute = AsyncMock(return_value=mock_exec_result)
            mock_fc_cls.return_value = mock_fc

            response = authed_client.post(
                "/v1/tools/execute",
                json={
                    "agent": "general",
                    "tool_name": "web_search",
                    "arguments": {},
                },
                headers={"Authorization": "Bearer test-key-1"},
            )

        assert response.status_code == 200

    def test_execute_invalid_body(self, app_client: TestClient) -> None:
        """Invalid request body returns 400."""
        response = app_client.post(
            "/v1/tools/execute",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400

    def test_execute_tool_failure(self, app_client: TestClient) -> None:
        """Tool execution failure returns 500."""
        mock_fn = MagicMock()
        mock_fn.name = "web_search"

        mock_agent = MagicMock()
        mock_agent.tools = [mock_fn]

        mock_exec_result = MagicMock()
        mock_exec_result.status = "failure"
        mock_exec_result.error = "Connection timeout"

        with (
            patch("mindroom.api.openai_compat.create_agent", return_value=mock_agent),
            patch("mindroom.api.openai_compat._find_function_on_agent", return_value=mock_fn),
            patch("mindroom.api.openai_compat.FunctionCall") as mock_fc_cls,
        ):
            mock_fc = MagicMock()
            mock_fc.aexecute = AsyncMock(return_value=mock_exec_result)
            mock_fc_cls.return_value = mock_fc

            response = app_client.post(
                "/v1/tools/execute",
                json={
                    "agent": "general",
                    "tool_name": "web_search",
                    "arguments": {},
                },
            )

        assert response.status_code == 500
        assert "Connection timeout" in response.json()["error"]["message"]

    def test_execute_router_agent_rejected(self, app_client: TestClient) -> None:
        """Router agent cannot be used for tool execution."""
        response = app_client.post(
            "/v1/tools/execute",
            json={
                "agent": "router",
                "tool_name": "search",
                "arguments": {},
            },
        )
        assert response.status_code == 404
