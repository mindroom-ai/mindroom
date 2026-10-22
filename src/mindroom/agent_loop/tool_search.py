"""Route a model's ``call_tool`` wrapper calls to the deferred functions they name.

Routes without native tool search see one fixed tool list and reach a deferred function through ``call_tool`` after
``tool_search`` returned its definition (``mindroom.providers.pipeline.deferred_tools`` projects the request). The
loop rewrites each resolvable ``call_tool`` call to the real function before resolving it, so approval, the hook
bridge, and stored history see the real name.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mindroom.llm.call_tool import CALL_TOOL_NAME, arguments_object

if TYPE_CHECKING:
    from collections.abc import Container


def resolve_call_tool_calls(tool_calls: list[dict[str, Any]], callable_names: Container[str]) -> list[dict[str, Any]]:
    """Rewrite each resolvable ``call_tool`` call to the function it names.

    A call naming an unknown function or carrying non-object arguments stays a ``call_tool`` call, whose fallback
    function tells the model how to retry.
    """
    return [_resolved_call(call, callable_names) for call in tool_calls]


def _resolved_call(call: dict[str, Any], callable_names: Container[str]) -> dict[str, Any]:
    function = call.get("function")
    if not isinstance(function, dict) or function.get("name") != CALL_TOOL_NAME:
        return call
    payload = arguments_object(function.get("arguments") or "{}")
    if payload is None:
        return call
    name = payload.get("name")
    if not isinstance(name, str) or name == CALL_TOOL_NAME or name not in callable_names:
        return call
    # Weaker models sometimes send the nested arguments as a JSON string.
    raw_arguments = payload.get("arguments")
    arguments = {} if raw_arguments in (None, "") else arguments_object(raw_arguments)
    if arguments is None:
        return call
    # Keys beside name and arguments, such as background_after or arguments flattened by a weaker model, belong to
    # the call too; nested arguments win a conflict, and a null extra means the key was omitted.
    extras = {key: value for key, value in payload.items() if key not in {"name", "arguments"} and value is not None}
    resolved_arguments = json.dumps({**extras, **arguments}, ensure_ascii=False)
    return {**call, "function": {**function, "name": name, "arguments": resolved_arguments}}


__all__ = ["resolve_call_tool_calls"]
