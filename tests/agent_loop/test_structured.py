"""Structured output is requested and read back as main does on each of its three provider paths."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import pytest
from graphiti_core.prompts.extract_nodes import ExtractedEntities
from pydantic import BaseModel, Field

from mindroom.agent_loop.structured import json_output_prompt, output_format_prompt, parse_structured
from mindroom.llm.provider import Capabilities
from mindroom.routing import _RoutingSuggestion
from mindroom.scheduling.tasks import ScheduledWorkflow
from mindroom.teams.formation import _TeamModeDecision
from mindroom.thread_summary import _ThreadEnrichment, _ThreadSummary
from mindroom.topic_generator import _RoomTopic
from tests.agno_parity.prompt_scenarios import load_golden

HELPER_SCHEMAS: dict[str, type[BaseModel]] = {
    "knowledge_graph": ExtractedEntities,
    "routing": _RoutingSuggestion,
    "schedule_parse": ScheduledWorkflow,
    "team_mode": _TeamModeDecision,
    "thread_enrichment": _ThreadEnrichment,
    "thread_summary": _ThreadSummary,
    "topic": _RoomTopic,
}
NATIVE = Capabilities(structured_output="native")
JSON_SCHEMA = Capabilities(structured_output="json_schema")
JSON_MODE = Capabilities(structured_output="json_mode")


def _first_request(name: str) -> dict[str, Any]:
    return load_golden(name)["requests"][0]


@pytest.mark.parametrize("helper", sorted(HELPER_SCHEMAS))
def test_only_json_mode_ends_the_system_message_with_the_json_prompt(helper: str) -> None:
    """The JSON prompt follows the helper's own prompt after a blank line, on the JSON-mode route only."""
    schema = HELPER_SCHEMAS[helper]
    system = {
        route: _first_request(f"helper_{helper}_{route}")["messages"][0]["content"]
        for route in ("anthropic", "cerebras", "vertexai_claude")
    }
    prompt = output_format_prompt(JSON_MODE, schema)

    assert prompt is not None
    assert system["vertexai_claude"] == f"{system['anthropic']}\n\n{prompt}"
    assert system["cerebras"] == system["anthropic"]
    assert output_format_prompt(NATIVE, schema) is None
    assert output_format_prompt(JSON_SCHEMA, schema) is None


class _Color(StrEnum):
    RED = "red"
    BLUE = "blue"


class _Swatch(BaseModel):
    """A named color."""

    name: str = Field(title="Swatch name", description="What the swatch is called")
    color: _Color


class _Palette(BaseModel):
    title: str
    primary: _Color
    swatches: list[_Swatch] = Field(default_factory=list)
    notes: str | None = None


# The JSON prompts main sent for these schemas; the helper schemas' prompts are pinned by their goldens above.
_SWATCH_PROMPT = """\
Provide your output as a JSON containing the following fields:
<json_fields>
["name", "color"]
</json_fields>

Here are the properties for each field:
<json_field_properties>
{
  "name": {
    "description": "What the swatch is called",
    "type": "string"
  },
  "color": {
    "$ref": "#/$defs/_Color"
  },
  "$defs": {
    "_Color": {
      "type": "string",
      "enum": [
        "red",
        "blue"
      ],
      "description": ""
    }
  }
}
</json_field_properties>
Start your response with `{` and end it with `}`.
Your output will be passed to json.loads() to convert it to a Python object.
Make sure it only contains valid JSON."""
_PALETTE_PROMPT = """\
Provide your output as a JSON containing the following fields:
<json_fields>
["title", "primary", "swatches", "notes"]
</json_fields>

Here are the properties for each field:
<json_field_properties>
{
  "title": {
    "type": "string"
  },
  "primary": {
    "$ref": "#/$defs/_Color"
  },
  "swatches": {
    "items": {
      "$ref": "#/$defs/_Swatch"
    },
    "type": "array"
  },
  "notes": {
    "anyOf": [
      {
        "type": "string"
      },
      {
        "type": "null"
      }
    ],
    "default": null
  },
  "$defs": {
    "_Color": {
      "type": "string",
      "enum": [
        "red",
        "blue"
      ],
      "description": ""
    },
    "_Swatch": {
      "name": {
        "description": "What the swatch is called",
        "type": "string"
      },
      "color": {
        "$ref": "#/$defs/_Color"
      }
    }
  }
}
</json_field_properties>
Start your response with `{` and end it with `}`.
Your output will be passed to json.loads() to convert it to a Python object.
Make sure it only contains valid JSON."""


@pytest.mark.parametrize(("schema", "expected"), [(_Swatch, _SWATCH_PROMPT), (_Palette, _PALETTE_PROMPT)])
def test_the_json_prompt_lists_fields_and_definitions(schema: type[BaseModel], expected: str) -> None:
    """Field lists, enum and object definitions, and the closing instructions keep the model-visible prompt."""
    assert json_output_prompt(schema) == expected


_ROUTING_JSON = '{"entity_name": "assistant", "reasoning": "Handbook questions."}'
_ROUTED = _RoutingSuggestion(entity_name="assistant", reasoning="Handbook questions.")
_DEEP = [_Swatch(name="deep", color=_Color.BLUE)]
_PALETTE = _Palette(title="Sea", primary=_Color.BLUE, swatches=_DEEP)
# Each answer with its schema and the model main's helpers read from it; None when no attempt validates.
PARSE_CASES: dict[str, tuple[type[BaseModel], str, BaseModel | None]] = {
    "plain": (_RoutingSuggestion, _ROUTING_JSON, _ROUTED),
    "fenced": (_RoutingSuggestion, f"Here you go:\n```json\n{_ROUTING_JSON}\n```\nAnything else?", _ROUTED),
    "unclosed_fence": (_RoutingSuggestion, f"Here you go:\n```json\n{_ROUTING_JSON}", _ROUTED),
    "bare_fence": (_RoutingSuggestion, f"```\n{_ROUTING_JSON}\n```", _ROUTED),
    "thinking": (_RoutingSuggestion, f"<think>The handbook agent fits.</think>\n{_ROUTING_JSON}", _ROUTED),
    "empty_thinking": (_RoutingSuggestion, f"<think></think>{_ROUTING_JSON}", _ROUTED),
    "markdown_keys": (
        _RoutingSuggestion,
        '{*"entity_name"*: "assistant", `"reasoning"`: "Handbook questions."}',
        _ROUTED,
    ),
    "newlines_in_values": (
        _RoutingSuggestion,
        '{"entity_name": "assistant",\n"reasoning": "Two\nlines"}',
        _RoutingSuggestion(entity_name="assistant", reasoning="Two lines"),
    ),
    "unescaped_quotes": (
        _RoutingSuggestion,
        '{"entity_name": "assistant", "reasoning": "It said "hi" first"}',
        _RoutingSuggestion(entity_name="assistant", reasoning='It said "hi" first'),
    ),
    "surrounding_prose": (_RoutingSuggestion, f"I pick: {_ROUTING_JSON} as requested.", _ROUTED),
    "split_objects": (
        _Palette,
        '{"title": "Sea", "swatches": [{"name": "a", "color": "red"}]} {"primary": "blue"}',
        _Palette(title="Sea", primary=_Color.BLUE, swatches=[_Swatch(name="a", color=_Color.RED)]),
    ),
    "split_objects_with_escapes": (
        _Palette,
        '{"title": "Sea 5\\" {draft} at C:\\\\maps\\\\", "primary": "red"} '
        '{"swatches": [{"name": "deep", "color": "blue"}]}',
        _Palette(title='Sea 5" {draft} at C:\\maps\\', primary=_Color.RED, swatches=_DEEP),
    ),
    "split_objects_after_a_placeholder": (
        _Palette,
        'Filling in {title}: {"title": "Sea", "primary": "blue"} {"swatches": [{"name": "deep", "color": "blue"}]}',
        _PALETTE,
    ),
    "split_lists": (
        _Palette,
        '{"title": "Sea", "primary": "red", "swatches": [{"name": "a", "color": "red"}]} '
        '{"swatches": [{"name": "b", "color": "blue"}]}',
        _Palette(
            title="Sea",
            primary=_Color.RED,
            swatches=[_Swatch(name="a", color=_Color.RED), _Swatch(name="b", color=_Color.BLUE)],
        ),
    ),
    "nested": (_Palette, _PALETTE.model_dump_json(), _PALETTE),
    "invalid_value": (_Palette, '{"title": "Sea", "primary": "green"}', None),
    "no_json": (_RoutingSuggestion, "The assistant should answer.", None),
    "wrong_shape": (_RoutingSuggestion, '["assistant"]', None),
}


@pytest.mark.parametrize("case", sorted(PARSE_CASES))
def test_parsing_recovers_common_answer_shapes(case: str) -> None:
    """Fenced, unclosed-fence, thinking-prefixed, and split answers validate; text without a valid object does not."""
    schema, text, expected = PARSE_CASES[case]
    assert parse_structured(text, schema) == expected
