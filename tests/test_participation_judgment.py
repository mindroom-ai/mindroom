"""Judgment backends decide participation at the real gate, falling back to the reply model's own check."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.run import run_agent
from mindroom.config.main import Config
from mindroom.config.participation import ParticipationConfig
from mindroom.hooks.enrichment import render_transient_context
from mindroom.judgment.client import JudgmentClient
from mindroom.judgment.typesafe import _PINNED_MODEL
from mindroom.llm.media import Image
from mindroom.llm.messages import Message
from mindroom.participation import ParticipationGate
from mindroom.participation_hook import participation_hook
from mindroom.participation_judgment import create_participation_decider
from tests.agent_loop.helpers import collect, context, spec
from tests.conftest import install_model_providers, test_runtime_paths
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.llm.runs import RunRecord
    from tests.fakes.scripted_model import ScriptStep

_RESPOND = '{"action":"respond","reason":"Useful help."}'


async def _fail_judge() -> None:
    msg = "Provider failed"
    raise RuntimeError(msg)


async def _cancel_judge() -> None:
    raise asyncio.CancelledError


def _response(probability: object = 0.9, *, model: str = _PINNED_MODEL) -> bytes:
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
    participation = ParticipationConfig.model_validate(
        {
            "instructions": "Offer technical help.",
            "judgment": {"provider": "typesafe", "threshold": threshold, "timeout_seconds": timeout}
            if backend == "typesafe"
            else {"provider": "llm", "model": "cheap", "timeout_seconds": timeout},
        },
    )
    return ParticipationGate(
        instructions=participation.instructions,
        decider=create_participation_decider(
            participation,
            Config(models={"cheap": {"provider": "test", "id": "cheap"}}),
            paths,
            agent_name="helper",
        ),
    )


async def _reply(
    model: ScriptedModel,
    gate: ParticipationGate,
    messages: Sequence[Message],
    *,
    run_id: str = "run-1",
) -> RunRecord:
    """Run one reply under the gate and return its record."""
    _, record = await collect(
        run_agent(
            spec(model),
            list(messages),
            ctx=context(run_id=run_id),
            hooks=LoopHooks(before_request=(participation_hook(gate, model),)),
        ),
    )
    return record


@pytest.mark.asyncio
@pytest.mark.parametrize(("score", "approved"), [(0.8, True), (0.79, False)])
async def test_typesafe_controls_real_gate_without_model_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    score: float,
    approved: bool,
) -> None:
    """Threshold mistakes or a redundant in-model check change the delivered answer."""
    posted: list[dict[str, Any]] = []

    async def post(_self: JudgmentClient, body: bytes) -> bytes:
        assert _self._api_key == "test-secret"
        posted.append(json.loads(body))
        return _response(score)

    monkeypatch.setattr(JudgmentClient, "_post", post)
    model = ScriptedModel([answer("Useful answer"), answer("Useful answer")])
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

    for run_id in ("run-1", "run-2"):
        record = await _reply(model, gate, messages, run_id=run_id)
        assert (record.content or "") == ("Useful answer" if approved else "")

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
    ],
)
async def test_typesafe_failure_uses_existing_model_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Provider failures and incomplete text must retain the current participation path."""
    posts: list[bytes] = []

    async def post(_self: JudgmentClient, body: bytes) -> bytes:
        posts.append(body)
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

    monkeypatch.setattr(JudgmentClient, "_post", post)
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
            Message(role="assistant", content="See image.", images=[Image(url="https://example.org/image.png")]),
        )
    model = ScriptedModel([answer(_RESPOND), answer("Useful answer")])

    record = await _reply(model, gate, messages)

    assert record.content == "Useful answer"
    assert gate.approved
    assert len(model.requests) == 2
    assert len(posts) == (1 if failure in {"timeout", "transport", "invalid", "drift"} else 0)


@pytest.mark.asyncio
async def test_typesafe_cancellation_does_not_fall_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled turn must not start another provider call or acquire approval."""

    async def post(_self: JudgmentClient, _body: bytes) -> bytes:
        raise asyncio.CancelledError

    monkeypatch.setattr(JudgmentClient, "_post", post)
    gate = _gate(tmp_path)
    model = ScriptedModel([answer("Should never be called")])
    with pytest.raises(asyncio.CancelledError):
        await _reply(model, gate, [Message(role="user", content="Any ideas?")])
    assert gate.decision is None
    assert model.requests == []


@pytest.mark.asyncio
async def test_typesafe_drops_prompt_metadata_and_preserves_speaker_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending raw Matrix metadata would expose stable IDs and display names unnecessarily."""
    posted: list[dict[str, Any]] = []

    async def post(_self: JudgmentClient, body: bytes) -> bytes:
        posted.append(json.loads(body))
        return _response(0.0)

    monkeypatch.setattr(JudgmentClient, "_post", post)
    gate = _gate(tmp_path)
    model = ScriptedModel([answer("Should not run")])
    messages = [
        Message(
            role="user",
            content='<msg event_id="$a" from="@alice:example.org" display_name="Alice"><![CDATA[Question?]]></msg>',
        ),
        Message(role="user", content='<msg event_id="$b" from="@bob:example.org"><![CDATA[Answer.]]></msg>'),
        Message(role="user", content='<msg event_id="$c" from="@alice:example.org"><![CDATA[Thanks.]]></msg>'),
    ]

    await _reply(model, gate, messages)

    wire = json.dumps(posted[0])
    assert not any(value in wire for value in ("@alice", "@bob", "Alice", "$a", "$b", "$c"))
    texts = [message["text"] for message in posted[0]["state"]["conversation"]]
    assert texts == [
        '<msg from="speaker_1"><![CDATA[Question?]]></msg>',
        '<msg from="speaker_2"><![CDATA[Answer.]]></msg>',
        '<msg from="speaker_1"><![CDATA[Thanks.]]></msg>',
    ]
    assert model.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("score", "requests"), [(0.95, 1), (0.05, 0), (None, 1)])
async def test_typesafe_decides_before_any_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    score: float | None,
    requests: int,
) -> None:
    """An approved reply reaches the provider with its tools selectable, and a refused one never does.

    An abstention falls back to the model's own decision, which an ordinary answer fails.
    """

    async def post(_self: JudgmentClient, _body: bytes) -> bytes:
        return _response(score)

    monkeypatch.setattr(JudgmentClient, "_post", post)
    gate = _gate(tmp_path)
    model = ScriptedModel([answer("Useful answer")])

    record = await _reply(model, gate, [Message(role="user", content="Any ideas?")])

    assert (record.content or "") == ("Useful answer" if score == 0.95 else "")
    assert gate.approved is (score == 0.95)
    assert len(model.requests) == requests
    if score == 0.95:
        assert model.requests[0].tool_choice is None


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True])
async def test_dedicated_llm_uses_same_gate_without_calling_reply_model_for_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approved: bool,
) -> None:
    """Changing the judge backend must preserve visible behavior and isolate the reply model."""
    judge = ScriptedModel([answer(json.dumps({"decision": approved}))])
    selected: list[str] = []

    def load(_config: Config, _paths: object, model_name: str) -> ScriptedModel:
        selected.append(model_name)
        return judge

    monkeypatch.setattr("mindroom.providers.registry.build_model_provider", load)
    reply = ScriptedModel([answer("Useful answer")])
    gate = _gate(tmp_path, backend="llm", key="")
    messages = [Message(role="system", content="Private system prompt"), Message(role="user", content="Any ideas?")]

    record = await _reply(reply, gate, messages)

    assert (record.content or "") == ("Useful answer" if approved else "")
    assert gate.approved is approved
    assert selected == ["cheap"]
    assert len(judge.requests) == 1
    assert len(reply.requests) == int(approved)
    request = judge.requests[0]
    assert not request.tools
    assert request.tool_choice == "none"
    assert "Private system prompt" not in str(request.messages)
    assert "Offer technical help." in str(request.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turn",
    [
        answer('{"decision": null}'),
        answer('{"decision": "yes"}'),
        calls(tool_call("unsafe", "call"), content='{"decision": true}'),
        [Await(_fail_judge)],
    ],
)
async def test_dedicated_llm_abstention_uses_existing_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turn: list[ScriptStep],
) -> None:
    """Both explicit abstention and judge failures preserve the existing fallback policy."""
    judge = ScriptedModel([turn])
    install_model_providers(monkeypatch, {"cheap": judge})
    reply = ScriptedModel([answer(_RESPOND), answer("Useful answer")])
    gate = _gate(tmp_path, backend="llm")

    record = await _reply(reply, gate, [Message(role="user", content="Any ideas?")])

    assert record.content == "Useful answer"
    assert gate.approved
    assert len(judge.requests) == 1
    assert len(reply.requests) == 2


@pytest.mark.asyncio
async def test_dedicated_llm_cancellation_does_not_start_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation has the same unsettled outcome for either backend."""
    judge = ScriptedModel([[Await(_cancel_judge)]])
    install_model_providers(monkeypatch, {"cheap": judge})
    reply = ScriptedModel([answer("Must not run")])
    gate = _gate(tmp_path, backend="llm")
    with pytest.raises(asyncio.CancelledError):
        await _reply(reply, gate, [Message(role="user", content="Any ideas?")])
    assert gate.decision is None
    assert reply.requests == []
