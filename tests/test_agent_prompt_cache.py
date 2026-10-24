"""Shared agent instructions remain cacheable when session context changes."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.history.request_messages import TurnMessages, build_request
from mindroom.history.types import ScopeSummary
from mindroom.hooks import EnrichmentItem, render_system_enrichment_block
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest
from mindroom.media_inputs import MediaInputs
from mindroom.providers.anthropic.messages import ClaudeProvider
from mindroom.providers.anthropic.prompt_cache import _count_cache_markers, prepare_request_body
from mindroom.system_prompt import render_session_context
from tests.conftest import test_runtime_paths
from tests.identity_helpers import persist_entity_accounts
from tests.providers.bare_adapter import bare_adapter

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.providers.anthropic.settings import ClaudeRouteKind


def _agent_request(
    tmp_path: Path,
    *,
    date: str,
    summary: str | None,
    instructions: str = "Follow the shared writing guidelines.",
    system_enrichment: str = "",
) -> tuple[str, dict[str, Any]]:
    """Return one turn's system prompt and the Claude request body it goes out in.

    The request is assembled from the production pieces a reply composes: the built agent's prompt with the turn's
    system enrichment, the scope summary ahead of the prompt, and the Claude adapter's wire preparation.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config(
        agents={
            "writer": AgentConfig(
                display_name="Writer",
                role="Write clear prose.",
                tools=[],
                instructions=[instructions],
            ),
        },
        defaults=DefaultsConfig(tools=[]),
        models={
            "default": ModelConfig(
                provider="anthropic",
                id="claude-sonnet-5",
                extra_kwargs={"api_key": "test-key"},
            ),
        },
    )
    persist_entity_accounts(config, runtime_paths)
    with patch("mindroom.agents.render_date_context", return_value=f"Current date: {date}"):
        build = create_agent("writer", config, runtime_paths, execution_identity=None)
    system = prompt_for_turn(build, system_enrichment)
    messages = [
        system,
        *build_request(
            summary=None if summary is None else ScopeSummary(text=summary, updated_at=datetime.now(UTC)),
            history=(),
            turn=TurnMessages(prompt=Message(role="user", content="Write an introduction.")),
            media=MediaInputs(),
        ),
    ]
    claude = bare_adapter(build.spec.model)
    assert isinstance(claude, ClaudeProvider)
    body = claude._prepare(ModelRequest(messages=tuple(messages))).kwargs
    assert isinstance(system.content, str)
    return system.content, body


def test_agent_cache_prefix_survives_date_and_compaction_changes(tmp_path: Path) -> None:
    """Fresh and compacted threads must write the same shared system boundary."""
    original_text, original = _agent_request(tmp_path, date="Monday", summary=None)
    compacted_text, compacted = _agent_request(
        tmp_path,
        date="Tuesday",
        summary="The user approved chapter one.",
        system_enrichment=render_system_enrichment_block(
            [EnrichmentItem(key="current_project", text="Editing chapter two.", cache_policy="volatile")],
        ),
    )

    assert len(original["system"]) == 2
    assert len(compacted["system"]) == 2
    assert original["system"][0] == compacted["system"][0]
    assert "Follow the shared writing guidelines." in original["system"][0]["text"]
    assert "Write clear prose." in original["system"][0]["text"]
    assert original["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "Current date: Monday" in original["system"][1]["text"]
    # The summary replays as the first history message, so it never changes the system prompt.
    assert "The user approved chapter one." not in compacted_text
    assert "The user approved chapter one." in str(compacted["messages"])
    assert "Editing chapter two." in compacted["system"][1]["text"]
    assert "cache_control" not in compacted["system"][1]
    assert "".join(block["text"] for block in original["system"]) == original_text
    assert "".join(block["text"] for block in compacted["system"]) == compacted_text
    assert _count_cache_markers(compacted) <= 4


def test_changed_agent_instructions_change_the_cache_prefix(tmp_path: Path) -> None:
    """A changed instruction must invalidate the shared system prefix."""
    _, original = _agent_request(tmp_path, date="Monday", summary=None)
    _, changed = _agent_request(
        tmp_path,
        date="Monday",
        summary=None,
        instructions="Use the revised writing guidelines.",
    )

    assert original["system"][0] != changed["system"][0]
    assert "Use the revised writing guidelines." in changed["system"][0]["text"]


@pytest.mark.parametrize("route", ["anthropic", "bedrock_claude", "vertexai_claude"])
@pytest.mark.parametrize("extended_cache_time", [False, True])
@pytest.mark.parametrize("cache_enabled", [False, True])
def test_system_boundary_preserves_custom_blocks_and_cache_budget(
    route: ClaudeRouteKind,
    *,
    extended_cache_time: bool,
    cache_enabled: bool,
) -> None:
    """Splitting must preserve custom blocks, TTL, input ownership, and the API marker limit."""
    cache_control = {"type": "ephemeral", **({"ttl": "1h"} if extended_cache_time else {})}
    original_system = {
        "type": "text",
        "text": "Shared instructions.\n\n" + render_session_context("Current date: Monday"),
        **({"cache_control": cache_control} if cache_enabled else {}),
    }
    custom_block = {"type": "text", "text": "Custom system block.", "cache_control": cache_control}
    request = {
        "system": [original_system, custom_block],
        "tools": [{"name": "demo", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "First request."}]},
            {"role": "assistant", "content": [{"type": "text", "text": "First answer."}]},
            {"role": "user", "content": [{"type": "text", "text": "Follow-up request."}]},
        ],
    }
    original = deepcopy(request)

    def prepare(body: dict[str, Any]) -> dict[str, Any]:
        return prepare_request_body(
            body,
            system="",
            route=route,
            deferred_tool_names=frozenset(),
            tool_choice=None,
            extended_cache_time=extended_cache_time,
            cache_system_prompt=cache_enabled,
        )

    prepared = prepare(request)

    assert request == original
    assert prepared["system"][-1] == custom_block
    assert _count_cache_markers(prepared) <= 4
    assert prepare(prepared) == prepared
    if cache_enabled:
        assert len(prepared["system"]) == 3
        assert prepared["system"][0] == {
            "type": "text",
            "text": "Shared instructions.\n\n",
            "cache_control": cache_control,
        }
        assert "cache_control" not in prepared["system"][1]
        assert prepared["messages"][-1]["content"][0]["cache_control"] == cache_control
    else:
        assert prepared == request
