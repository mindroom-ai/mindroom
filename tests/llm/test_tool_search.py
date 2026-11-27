"""The loop resolves each ``call_tool`` wrapper call to the deferred function it names."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from mindroom.llm.messages import Message
from mindroom.llm.provider import DeferredTools, ModelRequest, ToolSpec
from mindroom.llm.tool_search import portable_tool_search_request, resolve_call_tool_calls


def _call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _resolved_arguments(arguments: str) -> object:
    [resolved] = resolve_call_tool_calls([_call("c1", "call_tool", arguments)], {"crm_lookup"})
    assert resolved["function"]["name"] == "crm_lookup"
    return json.loads(resolved["function"]["arguments"])


def test_a_wrapped_call_leaves_the_namespace_of_the_function_it_wraps() -> None:
    """``call_tool`` lives in the default namespace, so a namespaced deferred call it wraps drops its namespace.

    Other provider keys beside ``function`` stay, as they do when a call resolves.
    """
    call = {**_call("c1", "crm_lookup", '{"id": 7}'), "namespace": "crm", "thought_signature": "sig"}
    request = ModelRequest(
        messages=[Message(role="assistant", tool_calls=[call])],
        tools=[ToolSpec(name="crm_lookup", description=None, parameters={"type": "object"}, strict=None)],
        deferred_tools=DeferredTools(names=frozenset({"crm_lookup"}), portable=True),
    )

    [wrapped] = portable_tool_search_request(request).messages[0].tool_calls or []

    assert wrapped["function"]["name"] == "call_tool"
    assert "namespace" not in wrapped
    assert (wrapped["id"], wrapped["thought_signature"]) == ("c1", "sig")


def test_a_resolved_call_keeps_its_id_and_extra_keys() -> None:
    """Provider keys beside ``function``, such as a Gemini thought signature, survive the rewrite."""
    call = {**_call("c1", "call_tool", '{"name": "crm_lookup", "arguments": {"id": 7}}'), "thought_signature": "sig"}

    [resolved] = resolve_call_tool_calls([call], {"crm_lookup", "call_tool"})

    assert resolved["function"] == {"name": "crm_lookup", "arguments": '{"id": 7}'}
    assert (resolved["id"], resolved["thought_signature"]) == ("c1", "sig")


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (json.dumps({"name": "crm_lookup", "arguments": '{"id": 7}'}), {"id": 7}),
        ("{'name': 'crm_lookup', 'arguments': {'id': 7}}", {"id": 7}),
        ('{"name": "crm_lookup"}', {}),
        ('{"name": "crm_lookup", "arguments": ""}', {}),
        (json.dumps({"name": "crm_lookup", "arguments": []}), {}),
        (json.dumps({"name": "crm_lookup", "arguments": "[]"}), {}),
        ('{"name": "crm_lookup", "customer_id": 7, "background_after": 0}', {"customer_id": 7, "background_after": 0}),
        ('{"name": "crm_lookup", "arguments": {"id": 7}, "background_after": null}', {"id": 7}),
        ('{"name": "crm_lookup", "customer_id": 1, "arguments": {"customer_id": 7}}', {"customer_id": 7}),
    ],
    ids=[
        "json-string-arguments",
        "python-literal",
        "missing-arguments",
        "empty-string-arguments",
        "empty-list",
        "empty-list-string",
        "flattened-extras",
        "null-extra-dropped",
        "nested-arguments-win",
    ],
)
def test_wrapper_arguments_become_the_real_call_arguments(arguments: str, expected: dict[str, Any]) -> None:
    """Nested, stringified, literal, empty, and flattened arguments all reach the real function as one object."""
    assert _resolved_arguments(arguments) == expected


@pytest.mark.parametrize(
    "arguments",
    [
        '{{"name": "crm_lookup"}}',
        "{'name': 'crm_lookup', 'arguments': {'id': {1, 2}}}",
        "[" * 100_000 + "]" * 100_000,
        '{"name": "nope"}',
        '{"name": "crm_lookup", "arguments": [1]}',
        '{"name": "crm_lookup", "arguments": "not json"}',
        "not json",
        '{"name": "call_tool"}',
        '{"arguments": {}}',
    ],
)
def test_unresolvable_calls_stay_wrapper_calls_for_the_fallback(arguments: str) -> None:
    """Weak models emit these; they must reach the guidance fallback instead of failing the run."""
    call = _call("c1", "call_tool", arguments)

    [resolved] = resolve_call_tool_calls([call], {"crm_lookup", "call_tool"})

    assert resolved is call


def test_a_mixed_batch_rewrites_only_the_wrapper() -> None:
    """Eager calls in the same batch pass through as the same objects."""
    eager_call = _call("c2", "shell", '{"command": "ls"}')
    calls = [_call("c1", "call_tool", '{"name": "crm_lookup", "arguments": {"id": 7}}'), eager_call]

    resolved = resolve_call_tool_calls(calls, {"crm_lookup", "shell"})

    assert [call["function"]["name"] for call in resolved] == ["crm_lookup", "shell"]
    assert resolved[1] is eager_call


def test_dotted_toolkit_prefix_resolves_only_when_owner_matches() -> None:
    """``call_tool(name='<toolkit>.<fn>')`` resolves when ``<toolkit>`` owns ``<fn>``, without misrouting."""
    offered = {
        "show_canvas": SimpleNamespace(toolkit="chat_ui", registered_toolkit="chat_ui"),
        "browser": SimpleNamespace(toolkit="browser", registered_toolkit="browser_control"),
        "call_tool": SimpleNamespace(toolkit="dynamic_tools", registered_toolkit="dynamic_tools"),
        "a.b": SimpleNamespace(toolkit="literal", registered_toolkit="literal"),
    }

    resolved = resolve_call_tool_calls(
        [
            _call("c1", "call_tool", '{"name": "chat_ui.show_canvas", "arguments": {"title": "Status"}}'),
            _call("c2", "call_tool", '{"name": "browser_control.browser", "arguments": {"action": "open"}}'),
            _call("c3", "call_tool", '{"name": "a.b", "arguments": {}}'),
        ],
        offered,
    )
    assert [call["function"]["name"] for call in resolved] == ["show_canvas", "browser", "a.b"]
    assert json.loads(resolved[0]["function"]["arguments"]) == {"title": "Status"}

    unresolved_calls = [
        _call("u1", "call_tool", '{"name": "wrong.show_canvas", "arguments": {}}'),
        _call("u2", "call_tool", '{"name": "chat_ui.unknown", "arguments": {}}'),
        _call("u3", "call_tool", '{"name": "dynamic_tools.call_tool", "arguments": {}}'),
        _call("u4", "call_tool", '{"name": ".show_canvas", "arguments": {}}'),
    ]
    assert resolve_call_tool_calls(unresolved_calls, offered) == unresolved_calls
    plain_call = _call("u5", "call_tool", '{"name": "chat_ui.show_canvas", "arguments": {}}')
    assert resolve_call_tool_calls([plain_call], {"show_canvas"}) == [plain_call]
