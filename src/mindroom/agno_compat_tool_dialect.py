"""Bind a tool dialect to one Agno model so canonical tools reach the provider in the model family's own shape."""

from __future__ import annotations

from types import MethodType
from typing import TYPE_CHECKING, Any, cast

from agno.models.message import Message

from mindroom.agno_compat_model_hooks import install_async_invocation_hooks
from mindroom.model_instance_checks import OPENAI_RESPONSES_CLASS, isinstance_of_loaded
from mindroom.tool_dialects import canonical_tool_calls, tool_dict_name, wire_messages, wire_tools

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine

    from agno.models.base import Model
    from agno.models.response import ModelResponse
    from agno.tools.function import Function, FunctionCall

    from mindroom.tool_dialect_types import ToolDialect

_TOOL_DIALECT_MARKER = "_mindroom_tool_dialect"
_TOOL_DIALECT_INVOKE_MARKER = "_mindroom_tool_dialect_invoke"


# AGNO_COMPAT: Tool presentation is fixed to Function identity.
# Reason: Agno 3.0.9 sends each Function under its own name and schema and dispatches provider calls by
# that name, so a model cannot see a tool under its trained harness name while MindRoom approvals, hooks,
# worker routing, and stored history keep the canonical name; the binding overrides `_format_tools`,
# `get_function_calls_to_run`, and the async invocation entry points of one model instance.
# Upstream issue: Tracking gap; no public hook separates provider-visible tool names and arguments from
# the dispatched Function.
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
        if not assistant_message.tool_calls or functions is None:
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

    def wrap_invoke(
        invoke: Callable[..., Coroutine[object, object, ModelResponse]],
    ) -> Callable[..., Coroutine[object, object, ModelResponse]]:
        async def invoke_in_dialect(*args: object, **kwargs: object) -> ModelResponse:
            return await invoke(*args, **_wire_kwargs(dialect, kwargs))

        return invoke_in_dialect

    def wrap_stream(
        stream: Callable[..., AsyncIterator[ModelResponse]],
    ) -> Callable[..., AsyncIterator[ModelResponse]]:
        async def stream_in_dialect(*args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
            async for chunk in stream(*args, **_wire_kwargs(dialect, kwargs)):
                yield chunk

        return stream_in_dialect

    model_dict["_format_tools"] = MethodType(format_wire_tools, model)
    model_dict["get_function_calls_to_run"] = MethodType(get_canonical_function_calls_to_run, model)
    install_async_invocation_hooks(
        model,
        marker=_TOOL_DIALECT_INVOKE_MARKER,
        wrap_invoke=wrap_invoke,
        wrap_stream=wrap_stream,
    )


def _wire_kwargs(dialect: ToolDialect, kwargs: dict[str, object]) -> dict[str, object]:
    messages = kwargs.get("messages")
    if not isinstance(messages, list):
        return kwargs
    tools = cast("list[dict[str, Any]] | None", kwargs.get("tools"))
    presented = {tool_dict_name(tool) for tool in tools or []}
    return {**kwargs, "messages": wire_messages(dialect, cast("list[Message]", messages), presented)}
