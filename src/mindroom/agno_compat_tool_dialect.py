"""Bind a tool dialect to one Agno model so canonical tools reach the provider in the model family's own shape."""

from __future__ import annotations

from contextvars import ContextVar
from functools import partial
from types import MethodType
from typing import TYPE_CHECKING, Any, cast

from agno.models.message import Message

from mindroom.agno_compat_model_hooks import install_async_invocation_hooks
from mindroom.model_instance_checks import OPENAI_RESPONSES_CLASS, isinstance_of_loaded
from mindroom.tool_dialects import canonical_tool_calls, wire_messages, wire_tools

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine

    from agno.models.base import Model
    from agno.models.response import ModelResponse
    from agno.tools.function import Function, FunctionCall

    from mindroom.tool_dialect_types import ToolDialect

_TOOL_DIALECT_MARKER = "_mindroom_tool_dialect"
# Set while a non-streamed invocation runs on wire-form messages, so a nested stream call does not render again.
_PROJECTED: ContextVar[bool] = ContextVar("mindroom_tool_dialect_projected", default=False)
_TOOL_DIALECT_INVOKE_MARKER = "_mindroom_tool_dialect_invoke"


# AGNO_COMPAT: Tool presentation is fixed to Function identity.
# Reason: Agno 3.0.9 sends each Function under its own name and schema and dispatches provider calls by
# that name, so a model cannot see a tool under its trained harness name while MindRoom approvals, hooks,
# worker routing, and stored history keep the canonical name; the binding overrides `_format_tools`,
# `get_function_calls_to_run`, and the async invocation entry points of one model instance.
# Upstream issue: Tracking gap; no public hook separates provider-visible tool names and arguments from
# the dispatched Function, and searching agno-agi/agno issues and PRs for tool aliases, tool presentation,
# and provider-specific tool names on October 9, 2026 found nothing.
# Upstream PR: None identified.
# Remove when: Agno exposes a per-model tool presentation hook that renames and reshapes definitions,
# history calls, and incoming calls while dispatching the original Function.
# Coverage: tests/test_tool_dialect_binding.py::test_wire_call_dispatches_canonical_function;
# tests/test_tool_dialect_binding.py::test_switching_dialect_rerenders_history;
# tests/test_tool_dialect_binding.py::test_chat_completions_payload_carries_no_wire_record;
# tests/test_tool_dialect_binding.py::test_overrides_bind_to_deepcopied_model.
def install_tool_dialect(model: Model, dialect: ToolDialect) -> None:
    """Present this model's canonical tools in *dialect*, as freeform tools where the Responses API allows them."""
    custom_tools = isinstance_of_loaded(model, OPENAI_RESPONSES_CLASS)
    model_dict = vars(model)
    if model_dict.get(_TOOL_DIALECT_MARKER) is not None:
        return
    model_dict[_TOOL_DIALECT_MARKER] = dialect.name
    # Bound methods survive Agno's model deepcopies, so each override calls its original on the copy.
    format_tools = model._format_tools.__func__
    get_function_calls_to_run = model.get_function_calls_to_run.__func__

    def format_wire_tools(
        bound_model: Model,
        tools: list[Function | dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        return format_tools(bound_model, wire_tools(dialect, tools or [], custom_tools=custom_tools))

    def get_canonical_function_calls_to_run(
        bound_model: Model,
        assistant_message: Message,
        messages: list[Message],
        functions: dict[str, Function] | None = None,
    ) -> list[FunctionCall]:
        if not dialect.functions or not assistant_message.tool_calls or functions is None:
            return get_function_calls_to_run(bound_model, assistant_message, messages, functions)
        translated, errors = canonical_tool_calls(dialect, assistant_message.tool_calls, functions)
        failed_calls = {id(error.call) for error in errors}
        assistant_message.tool_calls = [call for call in translated if id(call) not in failed_calls]
        try:
            function_calls = get_function_calls_to_run(bound_model, assistant_message, messages, functions)
        finally:
            assistant_message.tool_calls = translated
        messages.extend(
            Message(
                role=bound_model.tool_message_role,
                tool_call_id=error.call_id,
                tool_name=error.name,
                content=error.message,
            )
            for error in errors
        )
        return function_calls

    model_dict["_format_tools"] = MethodType(format_wire_tools, model)
    model_dict["get_function_calls_to_run"] = MethodType(get_canonical_function_calls_to_run, model)
    install_async_invocation_hooks(
        model,
        marker=_TOOL_DIALECT_INVOKE_MARKER,
        # Partials, unlike closures, rebind to a deepcopied model, so a copy invokes its own provider chain.
        wrap_invoke=lambda invoke: partial(_invoke_in_dialect, dialect, invoke),
        wrap_stream=lambda stream: partial(_stream_in_dialect, dialect, stream),
    )


async def _invoke_in_dialect(
    dialect: ToolDialect,
    invoke: Callable[..., Coroutine[object, object, ModelResponse]],
    *args: object,
    **kwargs: object,
) -> ModelResponse:
    if _PROJECTED.get():
        return await invoke(*args, **kwargs)
    # Some models answer a non-streamed call by streaming; their messages are already in wire form.
    token = _PROJECTED.set(True)
    try:
        return await invoke(*args, **_wire_kwargs(dialect, kwargs))
    finally:
        _PROJECTED.reset(token)


async def _stream_in_dialect(
    dialect: ToolDialect,
    stream: Callable[..., AsyncIterator[ModelResponse]],
    *args: object,
    **kwargs: object,
) -> AsyncIterator[ModelResponse]:
    async for chunk in stream(*args, **(kwargs if _PROJECTED.get() else _wire_kwargs(dialect, kwargs))):
        yield chunk


def _wire_kwargs(dialect: ToolDialect, kwargs: dict[str, object]) -> dict[str, object]:
    messages = kwargs.get("messages")
    if not isinstance(messages, list):
        return kwargs
    tools = cast("list[dict[str, Any]] | None", kwargs.get("tools"))
    return {**kwargs, "messages": wire_messages(dialect, cast("list[Message]", messages), tools or [])}
