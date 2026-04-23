"""Regression tests for streamed-response finalization."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.metrics import RunMetrics
from agno.run.agent import ModelRequestCompletedEvent, RunCompletedEvent, RunContentEvent

from mindroom.constants import AI_RUN_METADATA_KEY, STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.delivery_gateway import DeliveryGateway, DeliveryGatewayDeps
from mindroom.hooks import ResponseDraft
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.matrix.message_builder import markdown_to_html
from tests.conftest import bind_runtime_paths
from tests.test_ai_user_id import (
    _build_response_runner,
    _config,
    _make_bot,
    _prepared_prompt_result,
    _response_request,
    _runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_NO_VISIBLE_TEXT_AFTER_THINKING_NOTE = "**[Model emitted no visible text content after thinking. Please retry.]**"


@asynccontextmanager
async def _noop_typing(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
    yield


def _make_streaming_agent(*events: object) -> MagicMock:
    agent = MagicMock()
    agent.model = MagicMock()
    agent.model.__class__.__name__ = "OpenAIChat"
    agent.model.id = "test-model"
    agent.name = "GeneralAgent"
    agent.add_history_to_context = False

    def fake_arun(*_args: object, **kwargs: object) -> AsyncIterator[object]:
        assert kwargs["stream"] is True
        assert kwargs["stream_events"] is True

        async def stream() -> AsyncIterator[object]:
            for event in events:
                yield event

        return stream()

    agent.arun = MagicMock(side_effect=fake_arun)
    return agent


async def _run_streaming_finalize_scenario(
    tmp_path: Path,
    *events: object,
    before_response_hook: object | None = None,
) -> tuple[object, list[dict[str, object]]]:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(_config(), runtime_paths)
    bot = _make_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    coordinator = _build_response_runner(
        bot,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=tmp_path,
        requester_id="@alice:localhost",
    )
    conversation_cache = SimpleNamespace(
        get_latest_thread_event_id_if_needed=AsyncMock(return_value=None),
        notify_outbound_message=MagicMock(),
    )
    bot._conversation_resolver.deps = SimpleNamespace(conversation_cache=conversation_cache)
    captured_edits: list[dict[str, object]] = []

    async def record_edit(
        _client: object,
        _room_id: str,
        _event_id: str,
        new_content: dict[str, object],
        _new_text: str,
    ) -> DeliveredMatrixEvent:
        captured_edits.append(new_content)
        return DeliveredMatrixEvent(event_id="$edit", content_sent=new_content)

    async def apply_before_response(
        *,
        correlation_id: str,
        envelope: object,
        response_text: str,
        response_kind: str,
        tool_trace: object,
        extra_content: dict[str, object] | None,
    ) -> ResponseDraft:
        if before_response_hook is not None:
            return await before_response_hook(
                correlation_id=correlation_id,
                envelope=envelope,
                response_text=response_text,
                response_kind=response_kind,
                tool_trace=tool_trace,
                extra_content=extra_content,
            )
        return ResponseDraft(
            response_text=response_text,
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=extra_content,
            envelope=envelope,
        )

    agent = _make_streaming_agent(*events)
    delivery_gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=coordinator.deps.runtime,
            runtime_paths=runtime_paths,
            agent_name=coordinator.deps.agent_name,
            logger=coordinator.deps.logger,
            redact_message_event=AsyncMock(return_value=True),
            sender_domain="localhost",
            resolver=coordinator.deps.resolver,
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(side_effect=apply_before_response),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        ),
    )
    coordinator.deps = replace(coordinator.deps, delivery_gateway=delivery_gateway)
    request = replace(
        _response_request(prompt="Hello", user_id="@alice:localhost"),
        existing_event_id="$thinking",
        existing_event_is_placeholder=True,
    )
    request = replace(
        request,
        response_envelope=coordinator._response_envelope_for_request(
            request,
            resolved_target=coordinator.deps.resolver.build_message_target.return_value,
        ),
        correlation_id=coordinator._correlation_id_for_request(request),
    )

    with (
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=_prepared_prompt_result(agent))),
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.typing_indicator", new=_noop_typing),
        patch("mindroom.delivery_gateway.edit_message_result", new=AsyncMock(side_effect=record_edit)),
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
    ):
        delivery = await coordinator.process_and_respond_streaming(request)

    return delivery, captured_edits


@pytest.mark.asyncio
async def test_streaming_finalize_surfaces_reasoning_only_run_as_error(tmp_path: Path) -> None:
    """A run that only emits thinking blocks should finalize as a visible error."""
    delivery, captured_edits = await _run_streaming_finalize_scenario(
        tmp_path,
        RunContentEvent(reasoning_content="pondering"),
        ModelRequestCompletedEvent(input_tokens=6, output_tokens=0, cache_read_tokens=46449),
        RunCompletedEvent(
            content=None,
            reasoning_content="pondering",
            metrics=RunMetrics(input_tokens=6, output_tokens=0, cache_read_tokens=46449),
        ),
    )

    final_content = captured_edits[-1]
    ai_run = final_content[AI_RUN_METADATA_KEY]

    assert delivery.response_text == _NO_VISIBLE_TEXT_AFTER_THINKING_NOTE
    assert final_content["body"] == _NO_VISIBLE_TEXT_AFTER_THINKING_NOTE
    assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR
    assert ai_run["status"] == "error"
    assert final_content["formatted_body"] == markdown_to_html(_NO_VISIBLE_TEXT_AFTER_THINKING_NOTE)


@pytest.mark.asyncio
async def test_streaming_finalize_keeps_error_status_after_hook_reedit(tmp_path: Path) -> None:
    """A hook-driven final re-edit must preserve the error stream status."""

    async def mutate_before_response(
        *,
        correlation_id: str,
        envelope: object,
        response_text: str,
        response_kind: str,
        tool_trace: object,
        extra_content: dict[str, object] | None,
    ) -> ResponseDraft:
        del correlation_id
        mutated_extra_content = dict(extra_content or {})
        mutated_extra_content.pop(STREAM_STATUS_KEY, None)
        return ResponseDraft(
            response_text=f"{response_text}\n\nHook footer.",
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=mutated_extra_content,
            envelope=envelope,
        )

    delivery, captured_edits = await _run_streaming_finalize_scenario(
        tmp_path,
        RunContentEvent(reasoning_content="pondering"),
        ModelRequestCompletedEvent(input_tokens=6, output_tokens=0, cache_read_tokens=46449),
        RunCompletedEvent(content=None, reasoning_content="pondering"),
        before_response_hook=mutate_before_response,
    )

    final_content = captured_edits[-1]
    ai_run = final_content[AI_RUN_METADATA_KEY]

    assert delivery.response_text.endswith("Hook footer.")
    assert final_content["body"].endswith("Hook footer.")
    assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR
    assert ai_run["status"] == "error"
