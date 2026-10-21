"""Structured output is requested and read back as Agno 3.0.9 did on each of its three provider paths."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import pytest
from agno.utils.prompts import get_json_output_prompt
from agno.utils.string import parse_response_model_str
from graphiti_core.prompts.extract_nodes import ExtractedEntities
from pydantic import BaseModel, Field

from mindroom.agent_loop.structured import json_output_prompt, parse_structured, select_response_format
from mindroom.llm.provider import Capabilities
from mindroom.routing import _RoutingSuggestion
from mindroom.scheduling.tasks import ScheduledWorkflow
from mindroom.teams import _TeamModeDecision
from mindroom.thread_summary import _ThreadEnrichment, _ThreadSummary
from mindroom.topic_generator import _RoomTopic
from tests.agno_parity.prompt_scenarios import load_golden, response_format_dict

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
def test_response_formats_equal_the_helper_goldens(helper: str) -> None:
    """Claude gets the schema, Cerebras Agno's json_schema format, and Vertex Claude JSON mode."""
    schema = HELPER_SCHEMAS[helper]
    for route, capabilities in (("anthropic", NATIVE), ("cerebras", JSON_SCHEMA), ("vertexai_claude", JSON_MODE)):
        selected = select_response_format(capabilities, schema)
        assert (
            response_format_dict(selected.request_format)
            == _first_request(f"helper_{helper}_{route}")["response_format"]
        )
        assert selected.schema is schema


@pytest.mark.parametrize("helper", sorted(HELPER_SCHEMAS))
def test_only_json_mode_ends_the_system_message_with_the_json_prompt(helper: str) -> None:
    """The JSON prompt follows the helper's own prompt after a blank line, on the JSON-mode route only."""
    schema = HELPER_SCHEMAS[helper]
    system = {
        route: _first_request(f"helper_{helper}_{route}")["messages"][0]["content"]
        for route in ("anthropic", "cerebras", "vertexai_claude")
    }
    prompt = select_response_format(JSON_MODE, schema).prompt

    assert prompt is not None
    assert system["vertexai_claude"] == f"{system['anthropic']}\n\n{prompt}"
    assert system["cerebras"] == system["anthropic"]
    assert select_response_format(NATIVE, schema).prompt is None
    assert select_response_format(JSON_SCHEMA, schema).prompt is None


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


@pytest.mark.parametrize("schema", [_Palette, _Swatch, _RoutingSuggestion, ScheduledWorkflow])
def test_the_json_prompt_is_agnos(schema: type[BaseModel]) -> None:
    """Field lists, enum and object definitions, and the closing instructions match Agno's prompt."""
    assert json_output_prompt(schema) == get_json_output_prompt(schema)


_ROUTING_JSON = '{"entity_name": "assistant", "reasoning": "Handbook questions."}'
_PALETTE = _Palette(title="Sea", primary=_Color.BLUE, swatches=[_Swatch(name="deep", color=_Color.BLUE)])
PARSE_CASES = {
    "plain": (_RoutingSuggestion, _ROUTING_JSON),
    "fenced": (_RoutingSuggestion, f"Here you go:\n```json\n{_ROUTING_JSON}\n```\nAnything else?"),
    "bare_fence": (_RoutingSuggestion, f"```\n{_ROUTING_JSON}\n```"),
    "thinking": (_RoutingSuggestion, f"<think>The handbook agent fits.</think>\n{_ROUTING_JSON}"),
    "empty_thinking": (_RoutingSuggestion, f"<think></think>{_ROUTING_JSON}"),
    "markdown_keys": (_RoutingSuggestion, '{*"entity_name"*: "assistant", `"reasoning"`: "Handbook questions."}'),
    "newlines_in_values": (_RoutingSuggestion, '{"entity_name": "assistant",\n"reasoning": "Two\nlines"}'),
    "unescaped_quotes": (_RoutingSuggestion, '{"entity_name": "assistant", "reasoning": "It said "hi" first"}'),
    "surrounding_prose": (_RoutingSuggestion, f"I pick: {_ROUTING_JSON} as requested."),
    "split_objects": (_Palette, '{"title": "Sea", "swatches": [{"name": "a", "color": "red"}]} {"primary": "blue"}'),
    "split_lists": (
        _Palette,
        '{"title": "Sea", "primary": "red", "swatches": [{"name": "a", "color": "red"}]} '
        '{"swatches": [{"name": "b", "color": "blue"}]}',
    ),
    "nested": (_Palette, _PALETTE.model_dump_json()),
    "invalid_value": (_Palette, '{"title": "Sea", "primary": "green"}'),
    "no_json": (_RoutingSuggestion, "The assistant should answer."),
    "wrong_shape": (_RoutingSuggestion, '["assistant"]'),
}


@pytest.mark.parametrize("case", sorted(PARSE_CASES))
def test_parsing_reads_what_agno_read(case: str) -> None:
    """Answers parse to the same model, or fail the same way, as Agno's parser."""
    schema, text = PARSE_CASES[case]
    assert parse_structured(text, schema) == parse_response_model_str(text, schema)


def test_parsing_recovers_common_answer_shapes() -> None:
    """Fenced, thinking-prefixed, and split answers validate; text without a valid object does not."""
    expected = _RoutingSuggestion(entity_name="assistant", reasoning="Handbook questions.")
    for case in ("plain", "fenced", "bare_fence", "thinking", "markdown_keys", "surrounding_prose"):
        schema, text = PARSE_CASES[case]
        assert parse_structured(text, schema) == expected, case
    schema, text = PARSE_CASES["split_objects"]
    assert parse_structured(text, schema) == _Palette(
        title="Sea",
        primary=_Color.BLUE,
        swatches=[_Swatch(name="a", color=_Color.RED)],
    )
    for case in ("invalid_value", "no_json", "wrong_shape"):
        schema, text = PARSE_CASES[case]
        assert parse_structured(text, schema) is None, case
