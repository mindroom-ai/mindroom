"""Caller-authored Claude policies retain checkpoint replay without automatic edits."""
# ruff: noqa: S106

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from agno.models.message import Message
from agno.session.summary import SessionSummary
from anthropic import AsyncAnthropic, AsyncAnthropicVertex
from anthropic.types.beta import BetaMessage
from google.oauth2.credentials import Credentials

from mindroom.anthropic_claude import MindRoomAnthropicClaude
from mindroom.history.native import restore_native_history
from mindroom.openai_models import MindRoomOpenAIResponses
from mindroom.vertex_claude_compat import MindroomVertexAIClaude
from tests.history_helpers import _completed_run, _session
from tests.test_claude_native_compaction import _CHECKPOINT, _TEXT, _response

_POLICY = {"edits": [{"type": "compact_20260112", "instructions": "Keep launch facts."}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("vertex", [False, True])
@pytest.mark.parametrize("raw", [False, True])
async def test_authored_checkpoint_replays_after_rebuild_without_foreign_checkpoint(*, vertex: bool, raw: bool) -> None:
    """Dropping authored blocks or bypassing the route check breaks the second request."""
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = {"input_tokens": 100} if len(requests) == 3 else _response([_CHECKPOINT, _TEXT])
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = (
            AsyncAnthropicVertex(
                project_id="test-project",
                region="global",
                credentials=Credentials(token="test-token"),
                http_client=http_client,
            )
            if vertex
            else AsyncAnthropic(api_key="test-key", http_client=http_client)
        )
        model_type = MindroomVertexAIClaude if vertex else MindRoomAnthropicClaude
        foreign = model_type(id="claude-opus-5", async_client=client)
        foreign.configure_native_compaction(threshold=60000)
        foreign_block = {"type": "compaction", "content": "Foreign model summary"}
        old = foreign._parse_provider_response(BetaMessage.model_validate(_response([foreign_block, _TEXT])))
        messages = [
            Message(role="user", content="Original facts"),
            Message(role="assistant", content=old.content, provider_data=old.provider_data),
            Message(role="user", content="Continue"),
        ]
        kwargs = (
            {"request_params": {"extra_body": {"context_management": _POLICY}}}
            if raw
            else {"context_management": _POLICY}
        )
        model = model_type(id="claude-sonnet-5", async_client=client, betas=["compact-2026-01-12"], **kwargs)
        parsed = await model.ainvoke(messages, Message(role="assistant"))
        assistant = Message(role="assistant", content=parsed.content, provider_data=parsed.provider_data)
        messages.extend(
            [Message.model_validate_json(assistant.model_dump_json()), Message(role="user", content="Again")],
        )
        rebuilt = model_type(id="claude-sonnet-5", async_client=client, betas=["compact-2026-01-12"], **kwargs)
        await rebuilt.ainvoke(messages, Message(role="assistant"))
        if vertex:
            assert (
                await rebuilt._count_request_input_tokens(
                    messages,
                    tools=None,
                    response_format=None,
                    compress_tool_results=False,
                )
                == 100
            )

    assert requests[0]["context_management"] == _POLICY
    assert "Foreign model summary" not in json.dumps(requests[0]["messages"])
    assert requests[1]["context_management"] == _POLICY
    blocks = [block for message in requests[1]["messages"] for block in message["content"]]
    assert _CHECKPOINT in blocks
    assert foreign_block not in blocks
    if vertex:
        assert "context_management" not in requests[2]
        assert "Launch port 4321." in json.dumps(requests[2]["messages"])
        assert "Foreign model summary" not in json.dumps(requests[2]["messages"])
    assert rebuilt.native_compaction is not None
    assert rebuilt.native_compaction.threshold is None


@pytest.mark.parametrize("change", ["none", "model", "summary", "missing_threshold", "openai"])
def test_authored_approval_restore_requires_compatible_saved_policy(change: str) -> None:
    """A saved authored policy must survive a pause without enabling automatic or foreign replay."""
    model = MindRoomAnthropicClaude(id="claude-sonnet-5", context_management=_POLICY)
    parsed = model._parse_provider_response(BetaMessage.model_validate(_response([_CHECKPOINT, _TEXT])))
    latest = model._parse_provider_response(BetaMessage.model_validate(_response([_TEXT])))
    messages = [
        Message(role="assistant", content=parsed.content, provider_data=parsed.provider_data),
        Message(role="user", content="Continue"),
        Message(role="assistant", content=latest.content, provider_data=latest.provider_data),
    ]
    if change == "missing_threshold" and latest.provider_data.get("mindroom_native_compaction"):
        latest.provider_data["mindroom_native_compaction"].pop("threshold")
    run = _completed_run("paused", messages=messages)
    session = _session("session", runs=[run])
    rebuilt = (
        MindRoomOpenAIResponses(id="gpt-6-astra")
        if change == "openai"
        else MindRoomAnthropicClaude(
            id="claude-opus-5" if change == "model" else "claude-sonnet-5",
            context_management=_POLICY,
        )
    )
    if change == "summary":
        session.summary = SessionSummary(summary="New portable summary")
    restore_native_history(rebuilt, persisted_run=run, session=session)
    if change == "none":
        assert rebuilt.native_compaction is not None
        assert rebuilt.native_compaction.threshold is None
        assert rebuilt.get_request_params()["context_management"] == _POLICY
    else:
        assert rebuilt.native_compaction is None
