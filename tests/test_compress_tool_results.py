"""Regression coverage for ISSUE-184 compress_tool_results handling.

These tests cover MindRoom's config plumbing and the disabled-compression
no-mutation invariant.
They intentionally avoid asserting Agno's current enabled-compression bug so
the suite stays focused on MindRoom's downstream guarantee.
"""

from __future__ import annotations

import json
from textwrap import dedent
from typing import TYPE_CHECKING
from unittest.mock import patch

from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse

from mindroom.agents import create_agent
from mindroom.config.main import Config, load_config
from mindroom.constants import RuntimePaths, resolve_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from agno.agent import Agent


class FakeModel(Model):
    """Minimal model for deterministic compression tests."""

    def invoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        """Return one successful fake response."""
        return ModelResponse(content="ok")

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        """Return one successful fake async response."""
        return ModelResponse(content="ok")

    def invoke_stream(self, *_args: object, **_kwargs: object) -> Iterator[ModelResponse]:
        """Yield one successful fake streaming response."""
        yield ModelResponse(content="ok")

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        """Yield one successful fake async streaming response."""
        yield ModelResponse(content="ok")

    def _parse_provider_response(
        self,
        response: ModelResponse,
        *_args: object,
        **_kwargs: object,
    ) -> ModelResponse:
        return response

    def _parse_provider_response_delta(
        self,
        response: ModelResponse,
        *_args: object,
        **_kwargs: object,
    ) -> ModelResponse:
        return response


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _load_test_config(
    tmp_path: Path,
    *,
    defaults_compress_tool_results: bool | None,
    agent_compress_tool_results: bool | None = None,
) -> tuple[Config, RuntimePaths]:
    runtime_paths = _runtime_paths(tmp_path)
    defaults_override = (
        f"  compress_tool_results: {str(defaults_compress_tool_results).lower()}\n"
        if defaults_compress_tool_results is not None
        else ""
    )
    agent_override = (
        f"    compress_tool_results: {str(agent_compress_tool_results).lower()}\n"
        if agent_compress_tool_results is not None
        else ""
    )
    runtime_paths.config_path.write_text(
        dedent(
            f"""\
            defaults:
              tools: []
            {defaults_override}

            models:
              default:
                provider: openai
                id: test-model

            agents:
              general:
                display_name: GeneralAgent
                include_default_tools: false
                rooms: []
            {agent_override}""",
        ),
        encoding="utf-8",
    )
    return load_config(runtime_paths), runtime_paths


def _create_test_agent(
    tmp_path: Path,
    *,
    defaults_compress_tool_results: bool | None,
    agent_compress_tool_results: bool | None = None,
) -> Agent:
    config, runtime_paths = _load_test_config(
        tmp_path,
        defaults_compress_tool_results=defaults_compress_tool_results,
        agent_compress_tool_results=agent_compress_tool_results,
    )
    with patch(
        "mindroom.model_loading.get_model_instance",
        return_value=FakeModel(id="fake-model", provider="fake"),
    ):
        return create_agent(
            "general",
            config,
            runtime_paths,
            execution_identity=None,
            include_interactive_questions=False,
        )


def _canonical_message(message: Message) -> str:
    return json.dumps(message.to_dict(), sort_keys=True, separators=(",", ":"))


def test_defaults_compress_tool_results_false_propagates_to_agent(tmp_path: Path) -> None:
    """defaults.compress_tool_results=false should reach the created agent."""
    agent = _create_test_agent(tmp_path, defaults_compress_tool_results=False)

    assert agent.compress_tool_results is False


def test_omitted_defaults_compress_tool_results_uses_shipped_false_default(tmp_path: Path) -> None:
    """Omitting defaults.compress_tool_results should keep the shipped safe default."""
    config, _runtime_paths = _load_test_config(tmp_path, defaults_compress_tool_results=None)
    agent = _create_test_agent(tmp_path, defaults_compress_tool_results=None)

    assert config.defaults.compress_tool_results is False
    assert agent.compress_tool_results is False


def test_agent_override_can_reenable_compress_tool_results(tmp_path: Path) -> None:
    """Per-agent compress_tool_results should override the safer default."""
    agent = _create_test_agent(
        tmp_path,
        defaults_compress_tool_results=False,
        agent_compress_tool_results=True,
    )
    agent.initialize_agent()

    assert agent.compress_tool_results is True
    assert agent.compression_manager is not None


def test_disabled_compression_keeps_tool_message_bytes_stable(tmp_path: Path) -> None:
    """With compression disabled, Agno's response path must not mutate prior tool messages."""
    agent = _create_test_agent(tmp_path, defaults_compress_tool_results=False)
    agent.initialize_agent()

    messages = [
        Message(id="user-1", role="user", content="trigger", created_at=1),
        Message(id="tool-1", role="tool", content="alpha", tool_name="shell", created_at=2),
        Message(id="tool-2", role="tool", content="beta", tool_name="shell", created_at=3),
        Message(id="tool-3", role="tool", content="gamma", tool_name="shell", created_at=4),
        Message(id="tool-4", role="tool", content="delta", tool_name="shell", created_at=5),
    ]
    oldest_tool_before = _canonical_message(messages[1])

    agent.model.response(
        messages=messages,
        compression_manager=agent.compression_manager if agent.compress_tool_results else None,
    )

    assert messages[1].compressed_content is None
    assert _canonical_message(messages[1]) == oldest_tool_before
