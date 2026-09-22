"""TypeSafe participation at the real gate, using deterministic provider boundaries."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.media import Image
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from groq import AsyncGroq

from mindroom import model_loading
from mindroom.agno_participation import participation_model
from mindroom.config.main import Config
from mindroom.config.participation import RoomParticipationConfig
from mindroom.groq_model import MindRoomGroq
from mindroom.hooks.enrichment import render_transient_context
from mindroom.judgment.client import PINNED_MODEL, SystemOneClient
from mindroom.participation import ParticipationGate
from mindroom.participation_judgment import create_participation_decider
from tests.conftest import test_runtime_paths
from tests.participation_helpers import ParticipationModel

if TYPE_CHECKING:
    from pathlib import Path


def _response(probability: object = 0.9, *, model: str = PINNED_MODEL) -> bytes:
    return json.dumps(
        {
            "model": model,
            "answers": {"participation": {"type": "noul", "noul": probability}},
            "usage": {"input_tokens": 100, "output_tokens": 1},
        },
    ).encode()


def _gate(
    tmp_path: Path,
    *,
    threshold: float = 0.8,
    key: str = "test-secret",
    timeout: float = 1.5,
    backend: str = "typesafe",
) -> ParticipationGate:
    paths = replace(test_runtime_paths(tmp_path), process_env={"TYPESAFE_API_KEY": key})
    room = RoomParticipationConfig.model_validate(
        {
            "agent": "helper",
            "instructions": "Offer technical help.",
            "judgment": {"provider": "typesafe", "threshold": threshold, "timeout_seconds": timeout}
            if backend == "typesafe"
            else {"provider": "llm", "model": "cheap", "timeout_seconds": timeout},
        },
    )
    return ParticipationGate(
        instructions=room.instructions,
        decider=create_participation_decider(
            room,
            Config(models={"cheap": {"provider": "test", "id": "cheap"}}),
            paths,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("score", "approved"), [(0.8, True), (0.79, False)])
async def test_typesafe_controls_real_gate_without_model_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    score: float,
    approved: bool,
) -> None:
    """Threshold mistakes or a redundant in-model check change the delivered answer."""
    posted: list[dict[str, Any]] = []

    async def post(_self: SystemOneClient, body: bytes) -> bytes:
        assert _self._api_key == "test-secret"
        posted.append(json.loads(body))
        return _response(score)

    monkeypatch.setattr(SystemOneClient, "_post", post)
    model = ParticipationModel(ModelResponse(content="Useful answer"))
    gate = _gate(tmp_path, key=" \ttest-secret\n")
    messages = [
        Message(role="system", content="Private workspace instructions"),
        Message(
            role="user",
            content=render_transient_context(["Private memory and hook context"]),
            add_to_agent_memory=False,
        ),
        Message(role="user", content="Can anyone explain this?"),
    ]
    with participation_model(model, gate, run_id="primary"):
        for _ in range(2):
            if stream:
                chunks = [
                    item async for item in model.aresponse_stream(messages, run_response=RunOutput(run_id="primary"))
                ]
                content = "".join(item.content or "" for item in chunks)
            else:
                result = await model.aresponse(messages, run_response=RunOutput(run_id="primary"))
                content = result.content or ""
            assert content == ("Useful answer" if approved else "")
    assert gate.approved is approved
    assert len(posted) == 1
    assert posted[0]["questions"]["participation"]["type"] == "noul"
    assert "Offer technical help." in json.dumps(posted[0])
    assert "Private workspace" not in json.dumps(posted[0])
    assert "Private memory" not in json.dumps(posted[0])
    assert len(model.requests) == (2 if approved else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing_key",
        "timeout",
        "transport",
        "invalid",
        "drift",
        "redacted",
        "oversized",
        "unicode",
        "media",
        "attachment",
        "history_attachment",
        "output_media",
        "compressed",
    ],
)
async def test_typesafe_failure_uses_existing_model_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Provider failures and incomplete text must retain the current participation path."""
    calls: list[bytes] = []

    async def post(_self: SystemOneClient, body: bytes) -> bytes:
        calls.append(body)
        if failure == "timeout":
            await asyncio.Event().wait()
        if failure == "transport":
            message = "unavailable"
            raise httpx.ConnectError(message)
        if failure == "invalid":
            return _response(True)
        if failure == "drift":
            return _response(model="unexpected-version")
        return _response(0.0)

    monkeypatch.setattr(SystemOneClient, "_post", post)
    gate = _gate(tmp_path, key="" if failure == "missing_key" else "test-secret", timeout=0.01)
    content = {
        "redacted": "api_key=secret-value",
        "oversized": "x" * 20_000,
        "unicode": "\ud800",
        "attachment": "Attachments sent with the current message (use tool calls to inspect or process them by ID):\nprivate.txt",
        "history_attachment": "Can you inspect this? [attachments: att_123 (private.txt)]",
    }.get(failure, "Any ideas?")
    messages = [Message(role="user", content=content)]
    if failure == "media":
        messages[0].content = [{"type": "image_url", "image_url": {"url": "https://example.org/image.png"}}]
    if failure == "output_media":
        messages.insert(
            0,
            Message(role="assistant", content="See image.", image_output=Image(url="https://example.org/image.png")),
        )
    if failure == "compressed":
        messages[0].compressed_content = "Different provider-visible context."
    model = ParticipationModel(ModelResponse(content='{"action":"respond","reason":"Useful help."}'))
    with participation_model(model, gate, run_id="primary"):
        response = await model.aresponse(messages, run_response=RunOutput(run_id="primary"))
    assert response.content == "Useful answer"
    assert gate.approved
    assert len(model.requests) == 2
    assert len(calls) == (1 if failure in {"timeout", "transport", "invalid", "drift"} else 0)


@pytest.mark.asyncio
async def test_typesafe_cancellation_does_not_fall_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled turn must not start another provider call or acquire approval."""

    async def post(_self: SystemOneClient, _body: bytes) -> bytes:
        raise asyncio.CancelledError

    monkeypatch.setattr(SystemOneClient, "_post", post)
    gate = _gate(tmp_path)
    model = ParticipationModel(ModelResponse(content="Should never be called"))
    with pytest.raises(asyncio.CancelledError), participation_model(model, gate, run_id="primary"):
        await model.aresponse([Message(role="user", content="Any ideas?")], run_response=RunOutput(run_id="primary"))
    assert gate.decision is None
    assert model.requests == []


@pytest.mark.asyncio
async def test_typesafe_drops_prompt_metadata_and_preserves_speaker_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending raw Matrix metadata would expose stable IDs and display names unnecessarily."""
    posted: list[dict[str, Any]] = []

    async def post(_self: SystemOneClient, body: bytes) -> bytes:
        posted.append(json.loads(body))
        return _response(0.0)

    monkeypatch.setattr(SystemOneClient, "_post", post)
    gate = _gate(tmp_path)
    model = ParticipationModel(ModelResponse(content="Should not run"))
    messages = [
        Message(
            role="user",
            content='<msg event_id="$a" from="@alice:example.org" display_name="Alice"><![CDATA[Question?]]></msg>',
        ),
        Message(role="user", content='<msg event_id="$b" from="@bob:example.org"><![CDATA[Answer.]]></msg>'),
        Message(role="user", content='<msg event_id="$c" from="@alice:example.org"><![CDATA[Thanks.]]></msg>'),
    ]
    with participation_model(model, gate, run_id="primary"):
        await model.aresponse(messages, run_response=RunOutput(run_id="primary"))
    wire = json.dumps(posted[0])
    assert not any(value in wire for value in ("@alice", "@bob", "Alice", "$a", "$b", "$c"))
    texts = [message["text"] for message in posted[0]["state"]["conversation"]]
    assert texts == [
        '<msg from="speaker_1"><![CDATA[Question?]]></msg>',
        '<msg from="speaker_2"><![CDATA[Answer.]]></msg>',
        '<msg from="speaker_1"><![CDATA[Thanks.]]></msg>',
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [0.95, 0.05, None])
async def test_typesafe_gates_groq_native_tools_before_any_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    score: float | None,
) -> None:
    """Compound may run native tools only after approval; failed fallback stays quiet."""
    requests: list[dict[str, Any]] = []

    async def post(_self: SystemOneClient, _body: bytes) -> bytes:
        return _response(score)

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "reply",
                "object": "chat.completion",
                "created": 1,
                "model": "groq/compound",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "Useful answer"}, "finish_reason": "stop"},
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    monkeypatch.setattr(SystemOneClient, "_post", post)
    gate = _gate(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http_client:
        model = MindRoomGroq(
            id="groq/compound",
            api_key="test-key",
            async_client=AsyncGroq(api_key="test-key", http_client=http_client),
        )
        with participation_model(model, gate, run_id="primary"):
            response = await model.aresponse(
                [Message(role="user", content="Any ideas?")],
                run_response=RunOutput(run_id="primary"),
            )
    assert (response.content or "") == ("Useful answer" if score == 0.95 else "")
    assert gate.approved is (score == 0.95)
    assert len(requests) == (1 if score == 0.95 else 0)
    if requests:
        assert requests[0]["model"] == "groq/compound"
        assert requests[0].get("tool_choice") != "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("approved", [False, True])
async def test_dedicated_llm_uses_same_gate_without_calling_reply_model_for_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    approved: bool,
) -> None:
    """Changing the judge backend must preserve visible behavior and isolate the reply model."""
    judge = ParticipationModel(ModelResponse(content=json.dumps({"decision": approved})))
    selected: list[str] = []

    def load(_config: Config, _paths: object, model_name: str) -> ParticipationModel:
        selected.append(model_name)
        return judge

    monkeypatch.setattr(model_loading, "get_model_instance", load)
    reply = ParticipationModel(ModelResponse(content="Useful answer"))
    gate = _gate(tmp_path, backend="llm", key="")
    messages = [Message(role="system", content="Private system prompt"), Message(role="user", content="Any ideas?")]
    with participation_model(reply, gate, run_id="primary"):
        if stream:
            content = "".join(
                [
                    item.content or ""
                    async for item in reply.aresponse_stream(messages, run_response=RunOutput(run_id="primary"))
                ],
            )
        else:
            content = (await reply.aresponse(messages, run_response=RunOutput(run_id="primary"))).content
    assert content == ("Useful answer" if approved else "")
    assert gate.approved is approved
    assert selected == ["cheap"]
    assert len(judge.requests) == 1
    assert len(reply.requests) == int(approved)
    request = judge.requests[0]
    assert not request["tools"]
    assert request["tool_choice"] == "none"
    assert "Private system prompt" not in str(request["messages"])
    assert "Offer technical help." in str(request["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        ModelResponse(content='{"decision": null}'),
        ModelResponse(content='{"decision": "yes"}'),
        ModelResponse(
            content='{"decision": true}',
            tool_calls=[{"id": "call", "type": "function", "function": {"name": "unsafe", "arguments": "{}"}}],
        ),
        RuntimeError("Provider failed"),
    ],
)
async def test_dedicated_llm_abstention_uses_existing_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: ModelResponse | BaseException,
) -> None:
    """Both explicit abstention and judge failures preserve the existing fallback policy."""
    judge = ParticipationModel(output)
    monkeypatch.setattr(model_loading, "get_model_instance", lambda *_: judge)
    reply = ParticipationModel(ModelResponse(content='{"action":"respond","reason":"Useful help."}'))
    gate = _gate(tmp_path, backend="llm")
    with participation_model(reply, gate, run_id="primary"):
        response = await reply.aresponse(
            [Message(role="user", content="Any ideas?")],
            run_response=RunOutput(run_id="primary"),
        )
    assert response.content == "Useful answer"
    assert gate.approved
    assert len(judge.requests) == 1
    assert len(reply.requests) == 2


@pytest.mark.asyncio
async def test_dedicated_llm_cancellation_does_not_start_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation has the same unsettled outcome for either backend."""
    judge = ParticipationModel(asyncio.CancelledError())
    monkeypatch.setattr(model_loading, "get_model_instance", lambda *_: judge)
    reply = ParticipationModel(ModelResponse(content="Must not run"))
    gate = _gate(tmp_path, backend="llm")
    with pytest.raises(asyncio.CancelledError), participation_model(reply, gate, run_id="primary"):
        await reply.aresponse([Message(role="user", content="Any ideas?")], run_response=RunOutput(run_id="primary"))
    assert gate.decision is None
    assert reply.requests == []
