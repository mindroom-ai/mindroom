"""Real provider batches must be persisted before a shell can run."""

# ruff: noqa: D103
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Never

import pytest
from agno.exceptions import ModelProviderError
from agno.media import Audio, Image
from agno.models.fallback import FallbackConfig, acall_model_with_fallback
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.base import RunStatus
from agno.tools.function import Function, FunctionCall, ToolResult

from mindroom.agent_cli.lifetime import response_cli_lifetime
from mindroom.agent_storage import create_state_storage
from mindroom.agno_compat_cli_checkpoint import ProviderBatchCheckpoint, inner_cli_dispatch
from mindroom.history_run_visibility import is_model_history_visible_run
from tests.test_agent_tool_calls import _catalog

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_checkpoint_captures_exact_batch_and_propagates_storage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    catalog = await _catalog(tmp_path, [])
    catalog.run_response.agent_id = "helper"
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")

    async def bash(command: str) -> str:
        return command

    function = Function.from_callable(bash)
    checkpoint = ProviderBatchCheckpoint(catalog)
    assistant = Message(
        role="assistant",
        content="working",
        tool_calls=[
            {"id": "bash-1", "type": "function", "function": {"name": "bash", "arguments": '{"command":"one"}'}},
            {"id": "bash-2", "type": "function", "function": {"name": "bash", "arguments": '{"command":"two"}'}},
        ],
        provider_data={"signature": "real-provider-data"},
    )
    messages = [Message(role="user", content="do work"), assistant]
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(checkpoint, function)
        calls = catalog.agent.model.get_function_calls_to_run(assistant, messages, {"bash": function})
        with pytest.raises(ValueError, match="captured"):
            await checkpoint.persist(FunctionCall(function=function, call_id="bash-1", arguments={"command": "one"}))
        original = deepcopy(messages)
        assistant.content = "mutated after capture"
        await checkpoint.persist(calls[0])
        saved = catalog.agent.db.get_session(session_id="session")
        assert [item.to_dict() for item in saved.runs[0].messages] == [item.to_dict() for item in original]
        # A crash before Agno's final save must not replay the unanswered Bash call.
        assert not is_model_history_visible_run(saved.runs[0])
        assert catalog.run_response.status != RunStatus.cancelled
        assert catalog.run_response.messages == []
        await checkpoint.persist(calls[1])

    def fail(**_kwargs: object) -> None:
        msg = "injected database failure"
        raise OSError(msg)

    monkeypatch.setattr(catalog.agent.db, "upsert_run", fail)
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(checkpoint, function)
        calls = catalog.agent.model.get_function_calls_to_run(assistant, messages, {"bash": function})
        with pytest.raises(OSError, match="injected"):
            await checkpoint.persist(calls[0])


@pytest.mark.asyncio
async def test_inner_dispatch_cannot_capture_provider_history(tmp_path: Path) -> None:

    catalog = await _catalog(tmp_path, [])

    async def bash(command: str) -> str:
        return command

    function = Function.from_callable(bash)
    checkpoint = ProviderBatchCheckpoint(catalog)
    message = Message(
        role="assistant",
        tool_calls=[
            {
                "id": "inner-forged",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command":"inner"}'},
            },
        ],
    )
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(checkpoint, function)
        with inner_cli_dispatch():
            calls = catalog.agent.model.get_function_calls_to_run(message, [message], {"bash": function})
        with pytest.raises(ValueError, match="captured"):
            await checkpoint.persist(calls[0])


@pytest.mark.asyncio
async def test_real_fallback_batch_checkpoints_resolved_model(tmp_path: Path) -> None:
    from agno.models.openai import OpenAIChat  # noqa: PLC0415 - keep optional provider/server imports deferred

    class Primary(OpenAIChat):
        async def ainvoke(self, *_args: object, **_kwargs: object) -> Never:
            msg = "fake primary unavailable"
            raise ModelProviderError(msg, status_code=503)

    class Fallback(OpenAIChat):
        async def ainvoke(self, messages: list[Message], *_args: object, **_kwargs: object) -> ModelResponse:
            content = (
                {"role": "assistant", "content": "fallback done"}
                if any(message.role == "tool" for message in messages)
                else {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "fallback-bash",
                            "type": "function",
                            "function": {"name": "bash", "arguments": '{"command":"work"}'},
                        },
                    ],
                }
            )
            return ModelResponse(**content)

    catalog = await _catalog(tmp_path, [])
    catalog.run_response.agent_id = "helper"
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")
    catalog.agent.model = Primary()
    fallback = Fallback()
    catalog.agent.fallback_config = FallbackConfig(on_error=[fallback])
    catalog.agent.fallback_config.resolve_models()
    assert catalog.agent.fallback_config.on_error[0] is not fallback
    checkpoint = ProviderBatchCheckpoint(catalog)
    effects = []

    async def bash(command: str, fc: FunctionCall) -> str:
        await checkpoint.persist(fc)
        effects.append(command)
        return "tool finished"

    function = Function.from_callable(bash)
    function.process_entrypoint()
    checkpoint.prepare_capture()  # Exercise Agno copying the already-wrapped fallback.
    catalog.agent.fallback_config.resolve_models()
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(checkpoint, function)
        response = await acall_model_with_fallback(
            catalog.agent.model,
            catalog.agent.fallback_config,
            messages=[Message(role="user", content="work")],
            tools=[function],
        )
    assert effects == ["work"]
    assert response.content == "fallback done"
    saved = catalog.agent.db.get_session("session")
    assert saved.runs[0].messages[-1].tool_calls[0]["id"] == "fallback-bash"


@pytest.mark.asyncio
async def test_approval_checkpoint_refreshes_mutable_session_state(tmp_path: Path) -> None:

    catalog = await _catalog(tmp_path, [])
    catalog.run_response.agent_id = "helper"
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")

    async def bash(command: str) -> str:
        return command

    function = Function.from_callable(bash)
    checkpoint = ProviderBatchCheckpoint(catalog)
    message = Message(
        role="assistant",
        tool_calls=[
            {"id": "bash-1", "type": "function", "function": {"name": "bash", "arguments": '{"command":"work"}'}},
        ],
    )
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(checkpoint, function)
        calls = catalog.agent.model.get_function_calls_to_run(message, [message], {"bash": function})
        await checkpoint.persist(calls[0])
        catalog.run_context.session_state["before_approval"] = "kept"
        await checkpoint.persist_approval("bash-1")
    saved = catalog.agent.db.get_session("session")
    assert saved.session_data["session_state"]["before_approval"] == "kept"
    assert saved.runs[0].messages[0].tool_calls[0]["id"] == "bash-1"


@pytest.mark.asyncio
async def test_checkpoint_retains_only_the_current_provider_batch(tmp_path: Path) -> None:
    """Long minimal turns keep one history snapshot, not one per Bash batch."""
    catalog = await _catalog(tmp_path, [])

    async def bash(command: str) -> str:
        return command

    function = Function.from_callable(bash)
    checkpoint = ProviderBatchCheckpoint(catalog)
    messages = [Message(role="user", content="do work")]
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(checkpoint, function)
        for index in range(3):
            message = Message(
                role="assistant",
                tool_calls=[
                    {"id": f"bash-{index}", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                ],
            )
            messages.append(message)
            calls = catalog.agent.model.get_function_calls_to_run(message, messages, {"bash": function})
            assert [call.call_id for call, _batch in checkpoint._calls.values()] == [calls[0].call_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("media", [False, True])
async def test_approval_checkpoint_keeps_completed_sibling_result(tmp_path: Path, media: bool) -> None:

    catalog = await _catalog(tmp_path, [])
    catalog.run_response.agent_id = "helper"
    catalog.agent.db = create_state_storage("helper", tmp_path, subdir="sessions", session_table="sessions")

    async def bash(command: str) -> str:
        return command

    function = Function.from_callable(bash)
    checkpoint = ProviderBatchCheckpoint(catalog)
    message = Message(
        role="assistant",
        tool_calls=[
            {"id": name, "type": "function", "function": {"name": "bash", "arguments": '{"command":"work"}'}}
            for name in ("done", "waiting")
        ],
    )
    async with response_cli_lifetime() as lifetime:
        lifetime.bind_provider(checkpoint, function)
        calls = catalog.agent.model.get_function_calls_to_run(message, [message], {"bash": function})
        await checkpoint.persist(calls[0])

        result = (
            ToolResult(
                content="real sibling result",
                images=[Image(content=b"sibling-image", mime_type="image/png")],
                audios=[Audio(url="https://example.test/sibling.wav")],
            )
            if media
            else "real sibling result"
        )
        checkpoint.complete(calls[0], result)
        await checkpoint.persist_approval("waiting")
    saved = catalog.agent.db.get_session("session").runs[0]
    assert saved.messages[-1].tool_call_id == "done"
    assert saved.messages[-1].content == "real sibling result"
    assert [(tool.tool_call_id, tool.result) for tool in saved.tools] == [
        ("done", "real sibling result"),
        ("waiting", None),
    ]
    if media:
        assert saved.messages[-1].images[0].content == b"sibling-image"
        assert saved.messages[-1].audio[0].url == "https://example.test/sibling.wav"


@pytest.mark.asyncio
async def test_lifetime_capture_resolves_owner_created_during_first_pull(tmp_path: Path) -> None:

    catalog = await _catalog(tmp_path, [])
    checkpoint = ProviderBatchCheckpoint(catalog)
    function = Function(name="bash", entrypoint=lambda: "done")
    model = catalog.agent.model
    message = Message(
        role="assistant",
        tool_calls=[{"id": "outer", "type": "function", "function": {"name": "bash", "arguments": "{}"}}],
    )
    async with response_cli_lifetime() as lifetime:
        # Preparation runs after the lifetime context was entered.
        lifetime.bind_provider(checkpoint, function)
        calls = model.get_function_calls_to_run(message, [message], {"bash": function})
        checkpoint.complete(calls[0], "done")
        with lifetime.bind():
            checkpoint.complete(calls[0], "same batch retained")
    with pytest.raises(ValueError, match="captured"):
        checkpoint.complete(calls[0], "closed")
