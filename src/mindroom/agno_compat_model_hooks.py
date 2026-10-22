"""Explicit bindings for missing Agno model lifecycle extension points.

Owners supply transformations and execution policies; this module only binds
them to the Agno methods that currently expose the required lifecycle stages.
"""

from __future__ import annotations

from contextlib import aclosing
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

    from agno.models.base import Model
    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutputEvent
    from agno.run.team import TeamRunOutputEvent


def _with_tool_checkpoint(
    messages: list[Message],
    kwargs: dict[str, object],
    after_tools: Callable[[list[Message], ModelResponse], Awaitable[None]],
) -> dict[str, object]:
    """Run the owner's post-tool policy without discarding Agno's checkpoint."""
    previous = cast("Callable[[ModelResponse], Awaitable[None]] | None", kwargs.get("after_tool_results"))

    async def after_tool_results(result: ModelResponse) -> None:
        try:
            await after_tools(messages, result)
        finally:
            if previous is not None:
                await previous(result)

    return {**kwargs, "after_tool_results": after_tool_results}


# AGNO_COMPAT: Post-tool callbacks lack mutable messages and results.
# Reason: after_tool_results exists in Agno 3.0.9 but does not expose mutable
# input messages or raw tool-result messages to the owner through Agent/Team runs.
# Approved continuations append results directly before entering aresponse or
# aresponse_stream, bypassing both formatting and media callbacks.
# Mid-turn judgments must be awaited after a completed batch or at resumed entry,
# before the next provider request; existing checkpoints must run even on cancellation.
# Upstream issue: No matching public post-tool message callback issue identified.
# Upstream PR: None identified; existing checkpoint callbacks are only a partial API.
# Remove when: Public awaitable callbacks expose messages/results after formatting and media
# insertion, including resumed batches; retain the owner's notice deduplication
# and stop-after policy.
# Coverage: tests/test_mid_turn.py exercises streaming, resumed batches, terminal tools,
# and checkpoint cancellation; tests/test_queued_message_notify.py and
# tests/test_approval_queued_notice.py cover queued notices and approval resumes;
# tests/test_tool_job_notices.py covers background job notices on tool results.
def install_tool_result_callback(
    model: Model,
    *,
    marker: str,
    callback: Callable[[list[Message], list[Message]], None],
    before_response: Callable[[list[Message]], None],
    before_response_async: Callable[[list[Message]], Awaitable[None]],
    after_tools_async: Callable[[list[Message], ModelResponse], Awaitable[None]],
) -> None:
    """Observe tool-result stages and response entry after approved batches."""
    try:
        original_format = model.format_function_call_results
        model_dict = vars(model)
    except (AttributeError, TypeError):
        return
    if model_dict.get(marker) is True:
        return
    model_dict[marker] = True
    original_response = cast("Callable[..., Awaitable[ModelResponse]]", model.aresponse)
    original_stream = cast(
        "Callable[..., AsyncGenerator[ModelResponse | RunOutputEvent | TeamRunOutputEvent]]",
        model.aresponse_stream,
    )

    async def response(messages: list[Message], *args: object, **kwargs: object) -> ModelResponse:
        before_response(messages)
        await before_response_async(messages)
        return await original_response(messages, *args, **_with_tool_checkpoint(messages, kwargs, after_tools_async))

    async def response_stream(
        messages: list[Message],
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[ModelResponse | RunOutputEvent | TeamRunOutputEvent]:
        before_response(messages)
        await before_response_async(messages)
        async with aclosing(
            original_stream(messages, *args, **_with_tool_checkpoint(messages, kwargs, after_tools_async)),
        ) as stream:
            async for event in stream:
                yield event

    model_dict["aresponse"] = response
    model_dict["aresponse_stream"] = response_stream

    def format_results(
        messages: list[Message],
        function_call_results: list[Message],
        compress_tool_results: bool = False,
        **kwargs: object,
    ) -> None:
        original_format(
            messages=messages,
            function_call_results=function_call_results,
            compress_tool_results=compress_tool_results,
            **kwargs,
        )
        callback(messages, function_call_results)

    def handle_media(
        messages: list[Message],
        function_call_results: list[Message],
        send_media_to_model: bool = True,
    ) -> None:
        original_media(
            messages=messages,
            function_call_results=function_call_results,
            send_media_to_model=send_media_to_model,
        )
        callback(messages, function_call_results)

    model_dict["format_function_call_results"] = format_results
    try:
        original_media = model._handle_function_call_media
    except AttributeError:
        return
    model_dict["_handle_function_call_media"] = handle_media
