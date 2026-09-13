"""Shared agent instructions remain cacheable when session context changes."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from agno.agent._messages import aget_run_messages, get_run_messages
from agno.db.in_memory import InMemoryDb
from agno.models.anthropic import Claude
from agno.models.aws.claude import Claude as BedrockClaude
from agno.models.vertexai.claude import Claude as VertexClaude
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.session import AgentSession
from agno.session.summary import SessionSummary
from agno.utils.models.claude import format_messages

from mindroom.agents import create_agent
from mindroom.claude_prompt_cache import _count_cache_markers, prepare_claude_request_kwargs
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.hooks import EnrichmentItem, render_system_enrichment_block
from mindroom.system_prompt import render_session_context
from tests.conftest import test_runtime_paths
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path


async def _agent_request(
    tmp_path: Path,
    *,
    session_id: str,
    date: str,
    summary: str | None,
    instructions: str = "Follow the shared writing guidelines.",
    async_mode: bool = False,
    extra_context: str = "",
) -> tuple[str, dict[str, Any]]:
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
        defaults=DefaultsConfig(tools=[], learning=False),
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
        agent = create_agent(
            "writer",
            config,
            runtime_paths,
            execution_identity=None,
            session_id=session_id,
            history_storage=InMemoryDb(),
        )
    if extra_context:
        agent.additional_context = f"{agent.additional_context or ''}\n{extra_context}"
    session = AgentSession(
        session_id=session_id,
        agent_id=agent.id,
        summary=SessionSummary(summary=summary) if summary is not None else None,
    )
    kwargs = {
        "run_response": RunOutput(),
        "run_context": RunContext(run_id="run", session_id=session_id, session_state={}),
        "input": "Write an introduction.",
        "session": session,
    }
    run_messages = await aget_run_messages(agent, **kwargs) if async_mode else get_run_messages(agent, **kwargs)
    chat_messages, system_text = format_messages(run_messages.messages)
    model = agent.model
    assert isinstance(model, Claude)
    request = model._prepare_request_kwargs(system_text, messages=run_messages.messages)
    request["messages"] = chat_messages
    return system_text, prepare_claude_request_kwargs(model, request)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
async def test_agent_cache_prefix_survives_date_and_compaction_changes(tmp_path: Path, *, async_mode: bool) -> None:
    """Fresh and compacted threads must write the same shared system boundary."""
    original_text, original = await _agent_request(
        tmp_path,
        session_id="thread-a",
        date="Monday",
        summary=None,
        async_mode=async_mode,
    )
    compacted_text, compacted = await _agent_request(
        tmp_path,
        session_id="thread-b",
        date="Tuesday",
        summary="The user approved chapter one.",
        async_mode=async_mode,
        extra_context=render_system_enrichment_block(
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
    assert "The user approved chapter one." in compacted["system"][1]["text"]
    assert "Editing chapter two." in compacted["system"][1]["text"]
    assert "cache_control" not in compacted["system"][1]
    assert "".join(block["text"] for block in original["system"]) == original_text
    assert "".join(block["text"] for block in compacted["system"]) == compacted_text
    assert _count_cache_markers(compacted) <= 4


@pytest.mark.asyncio
async def test_changed_agent_instructions_change_the_cache_prefix(tmp_path: Path) -> None:
    """A changed instruction must invalidate the shared system prefix."""
    _, original = await _agent_request(tmp_path, session_id="a", date="Monday", summary=None)
    _, changed = await _agent_request(
        tmp_path,
        session_id="b",
        date="Monday",
        summary=None,
        instructions="Use the revised writing guidelines.",
    )

    assert original["system"][0] != changed["system"][0]
    assert "Use the revised writing guidelines." in changed["system"][0]["text"]


@pytest.mark.parametrize("model_type", [Claude, BedrockClaude, VertexClaude])
@pytest.mark.parametrize("extended_cache_time", [False, True])
@pytest.mark.parametrize("cache_enabled", [False, True])
def test_system_boundary_preserves_custom_blocks_and_cache_budget(
    model_type: type[Claude],
    *,
    extended_cache_time: bool,
    cache_enabled: bool,
) -> None:
    """Splitting must preserve custom blocks, TTL, input ownership, and the API marker limit."""
    model = model_type(
        id="claude-sonnet-5",
        cache_system_prompt=cache_enabled,
        extended_cache_time=extended_cache_time,
    )
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

    prepared = prepare_claude_request_kwargs(model, request)

    assert request == original
    assert prepared["system"][-1] == custom_block
    assert _count_cache_markers(prepared) <= 4
    assert prepare_claude_request_kwargs(model, prepared) == prepared
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
