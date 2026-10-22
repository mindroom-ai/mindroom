"""The loop resolves each ``call_tool`` wrapper call to the deferred function it names."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mindroom.agent_loop.tool_search import resolve_call_tool_calls


def _call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _resolved_arguments(arguments: str) -> object:
    [resolved] = resolve_call_tool_calls([_call("c1", "call_tool", arguments)], {"crm_lookup"})
    assert resolved["function"]["name"] == "crm_lookup"
    return json.loads(resolved["function"]["arguments"])


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
