"""Capture what Agno 3.0.9 sends providers for every registered tool, and how it handles tool-call arguments.

Run `.venv/bin/python -m tests.agno_parity.capture_tools` from the repository root to rewrite
`tests/fixtures/agno_parity/tools/`; `tests/fixtures/agno_parity/README.md` describes the fixtures.
The tool-building setup lives in `tests/tool_system/agno_parity_support.py`, which the owned parity tests share.
Deleted together with Agno.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agno.agent import Agent
from agno.agent._tools import parse_tools
from agno.models.message import Message
from agno.models.openai import OpenAIChat
from agno.tools import Toolkit
from agno.tools.function import Function

from mindroom.tool_system.registry_state import BUILTIN_TOOL_METADATA
from tests.agno_parity import tool_argument_probes as probes
from tests.tool_system.agno_parity_support import (
    ALL_ENABLED_SCHEMAS_PATH,
    ARGUMENTS_PATH,
    SCHEMAS_PATH,
    all_enabled_overrides,
    build_toolkit,
    golden_runtime,
    isolated_process,
    missing_dependencies,
    normalize_error_text,
    normalize_version,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

# Registered tools that no offline capture can build, with the reason.
UNBUILDABLE = {
    "apify": "its functions come from Apify Actor builds, which only the service returns",
    "composio": "its functions come from the Composio action catalog, which only the service returns",
}
# Declared function names (`ToolMetadata.function_names`) that no configuration registers, by tool.
NEVER_REGISTERED_FUNCTIONS = {
    # Methods of async variants, which Agno registers under the name of their sync function.
    "brandfetch": ("asearch_by_brand", "asearch_by_identifier"),
    "browserbase": ("aclose_session", "aget_page_content", "anavigate_to", "ascreenshot"),
    # Helper methods and attributes that the toolkit never registers as functions.
    "discord": ("get_tool_config", "get_tool_description", "get_tool_name"),
    "duckdb": ("get_table_name_from_path",),
    "fal": ("on_queue_update",),
    "moviepy_video_tools": ("create_caption_clips", "parse_srt", "split_text_into_lines"),
    "newspaper": ("get_article_data",),
    "sql": ("run_sql",),
    "zoom": ("instructions",),
    # Registered only beside a knowledge base object, which the text `knowledge` config field cannot hold.
    "website": ("add_website_to_knowledge",),
    "wikipedia": ("search_wikipedia_and_update_knowledge_base",),
}


def _golden_model() -> OpenAIChat:
    return OpenAIChat(id="golden-model", api_key="golden-capture")


def prepare_functions(toolkit: Toolkit) -> tuple[list[Function], list[str]]:
    """Run Agno's per-run preparation for an async agent run; return the functions and the tool instructions."""
    model = _golden_model()
    agent = Agent(model=model)
    prepared = parse_tools(agent, tools=[toolkit], model=model, async_mode=True)
    return [function for function in prepared if isinstance(function, Function)], list(agent._tool_instructions or ())


def tool_schema(toolkit: Toolkit) -> dict[str, object]:
    """Return the golden entry of one built toolkit."""
    functions, tool_instructions = prepare_functions(toolkit)
    added = toolkit.add_instructions and toolkit.instructions is not None
    if tool_instructions != ([toolkit.instructions] if added else []):
        msg = f"{toolkit.name}: Agno adds instructions the golden does not record, such as function instructions"
        raise ValueError(msg)
    entry = {
        "instructions": toolkit.instructions,
        "add_instructions": toolkit.add_instructions,
        "functions": [
            {
                "name": function.name,
                "description": function.description,
                "parameters": function.parameters,
                "strict": function.strict,
                "requires_confirmation": function.requires_confirmation,
                "external_execution": function.external_execution,
                "approval_type": function.approval_type,
                "stop_after_tool_call": function.stop_after_tool_call,
                "show_result": function.show_result,
            }
            for function in functions
        ],
    }
    normalized = normalize_version(entry)
    assert isinstance(normalized, dict)
    return normalized


def all_enabled_tool_schema(tool_name: str, config: Config, runtime_paths: RuntimePaths) -> dict[str, object]:
    """Return the all-enabled golden entry of one registered tool: its overrides, then its schema with them."""
    overrides = all_enabled_overrides(tool_name)
    return {"overrides": overrides, **tool_schema(build_toolkit(tool_name, config, runtime_paths, overrides))}


@dataclass(frozen=True)
class ToolSchemaCapture:
    """The golden entries of every buildable registered tool, and why each other tool was skipped."""

    schemas: dict[str, dict[str, object]]
    all_enabled_schemas: dict[str, dict[str, object]]
    skipped: dict[str, str]


def capture_tool_schemas(root: Path) -> ToolSchemaCapture:
    """Build every registered tool with its default configuration and, when it has switches, with all of them on."""
    capture = ToolSchemaCapture(schemas={}, all_enabled_schemas={}, skipped={})
    with isolated_process(root):
        config, runtime_paths = golden_runtime(root)
        for tool_name in sorted(BUILTIN_TOOL_METADATA):
            if tool_name in UNBUILDABLE:
                capture.skipped[tool_name] = UNBUILDABLE[tool_name]
            elif (dependencies := missing_dependencies(tool_name)) is not None:
                capture.skipped[tool_name] = f"missing optional dependencies: {dependencies}"
            else:
                capture.schemas[tool_name] = tool_schema(build_toolkit(tool_name, config, runtime_paths))
                if all_enabled_overrides(tool_name):
                    capture.all_enabled_schemas[tool_name] = all_enabled_tool_schema(tool_name, config, runtime_paths)
    return capture


@dataclass(frozen=True)
class ArgumentCase:
    """One tool call a model sends: the probe function it names and its raw arguments string."""

    function: str
    raw_arguments: str


_SCALARS = "probe_scalars"
_OPTIONAL = "probe_optional"
_COLLECTIONS = "probe_collections"
_MODEL = "probe_model"
_AGENT = "probe_agent"
ARGUMENT_CASES = {
    "scalars_native_types": ArgumentCase(_SCALARS, '{"count": 3, "ratio": 2.5, "enabled": true, "label": "x"}'),
    "scalars_string_numbers": ArgumentCase(_SCALARS, '{"count": "3", "ratio": "2.5", "enabled": true, "label": "7"}'),
    "scalars_padded_true_string": ArgumentCase(
        _SCALARS,
        '{"count": 1, "ratio": 1, "enabled": " TRUE ", "label": "x"}',
    ),
    "scalars_false_string_for_text": ArgumentCase(
        _SCALARS,
        '{"count": 1, "ratio": 1.0, "enabled": false, "label": "false"}',
    ),
    "scalars_yes_string_for_flag": ArgumentCase(_SCALARS, '{"count": 1, "ratio": 1.0, "enabled": "yes", "label": "x"}'),
    "scalars_null_string_for_number": ArgumentCase(
        _SCALARS,
        '{"count": "null", "ratio": 1.0, "enabled": true, "label": "x"}',
    ),
    "scalars_integral_float_for_int": ArgumentCase(
        _SCALARS,
        '{"count": 3.0, "ratio": 2, "enabled": true, "label": "x"}',
    ),
    "scalars_fractional_float_for_int": ArgumentCase(
        _SCALARS,
        '{"count": 3.5, "ratio": 2, "enabled": true, "label": "x"}',
    ),
    "scalars_decimal_string_for_int": ArgumentCase(
        _SCALARS,
        '{"count": "3.5", "ratio": 2, "enabled": true, "label": "x"}',
    ),
    "scalars_missing_required": ArgumentCase(_SCALARS, '{"count": 1}'),
    "scalars_unknown_argument": ArgumentCase(
        _SCALARS,
        '{"count": 1, "ratio": 1.0, "enabled": true, "label": "x", "extra": 1}',
    ),
    "scalars_python_literal": ArgumentCase(_SCALARS, "{'count': 3, 'ratio': 2.5, 'enabled': True, 'label': 'x'}"),
    "scalars_invalid_json": ArgumentCase(_SCALARS, '{"count": 3'),
    "scalars_json_array": ArgumentCase(_SCALARS, "[1, 2]"),
    "scalars_json_string": ArgumentCase(_SCALARS, '"count"'),
    "scalars_json_null": ArgumentCase(_SCALARS, "null"),
    "scalars_empty_array": ArgumentCase(_SCALARS, "[]"),
    "optional_omitted": ArgumentCase(_OPTIONAL, "{}"),
    "optional_none_strings": ArgumentCase(_OPTIONAL, '{"note": "None", "limit": "none", "flag": "False"}'),
    "optional_values": ArgumentCase(_OPTIONAL, '{"note": "hi", "limit": "4", "flag": "true"}'),
    "optional_empty_arguments": ArgumentCase(_OPTIONAL, ""),
    "collections_coerced_values": ArgumentCase(_COLLECTIONS, '{"tags": ["a", "b"], "weights": {"x": "1.5", "y": 2}}'),
    "collections_nested_strings_kept": ArgumentCase(_COLLECTIONS, '{"tags": ["true", "null"], "weights": {}}'),
    "collections_wrong_item_type": ArgumentCase(_COLLECTIONS, '{"tags": [1], "weights": {}}'),
    "collections_json_encoded_list": ArgumentCase(_COLLECTIONS, '{"tags": "[\\"a\\"]", "weights": {}}'),
    "choice_valid": ArgumentCase("probe_choice", '{"mode": "fast"}'),
    "choice_invalid": ArgumentCase("probe_choice", '{"mode": "medium"}'),
    "model_from_object": ArgumentCase(_MODEL, '{"query_filter": {"field": "name", "limit": "5"}}'),
    "model_default_field": ArgumentCase(_MODEL, '{"query_filter": {"field": "name"}}'),
    "model_invalid_fields": ArgumentCase(_MODEL, '{"query_filter": {"limit": "many"}}'),
    "model_from_json_string": ArgumentCase(_MODEL, '{"query_filter": "{\\"field\\": \\"name\\"}"}'),
    "no_arguments_empty_object": ArgumentCase("probe_no_arguments", "{}"),
    "no_arguments_empty_array": ArgumentCase("probe_no_arguments", "[]"),
    "no_arguments_unknown_argument": ArgumentCase("probe_no_arguments", '{"extra": 1}'),
    "async_string_number": ArgumentCase("probe_async", '{"count": "4"}'),
    "async_invalid_number": ArgumentCase("probe_async", '{"count": "four"}'),
    "agent_parameter_skips_coercion": ArgumentCase(_AGENT, '{"query": 3}'),
    "agent_parameter_drops_model_value": ArgumentCase(_AGENT, '{"query": "x", "agent": "spoof"}'),
    "agent_parameter_unknown_argument": ArgumentCase(_AGENT, '{"query": "x", "extra": 1}'),
    "agent_parameter_missing_required": ArgumentCase(_AGENT, "{}"),
    "tool_raises": ArgumentCase("probe_raises", '{"message": "boom"}'),
    "unknown_function": ArgumentCase("probe_missing", "{}"),
}


def prepare_probe_functions() -> dict[str, Function]:
    """Return the probe tools by name, prepared as Agno prepares an async agent run's tools."""
    functions, _instructions = prepare_functions(Toolkit(name="probes", tools=list(probes.PROBES)))
    return {function.name: function for function in functions}


async def capture_argument_case(case: ArgumentCase, functions: dict[str, Function]) -> dict[str, object]:
    """Resolve and execute one tool call as Agno's model loop does; return what the probe body or the model saw."""
    model = _golden_model()
    outcome: dict[str, object] = {"function": case.function, "raw_arguments": case.raw_arguments}
    tool_call = {
        "id": "call_golden",
        "type": "function",
        "function": {"name": case.function, "arguments": case.raw_arguments},
    }
    rejected: list[Message] = []
    function_calls = model.get_function_calls_to_run(
        Message(role="assistant", tool_calls=[tool_call]),
        rejected,
        functions,
    )
    if rejected:
        [message] = rejected
        return {**outcome, "error_text": normalize_error_text(str(message.content))}
    results: list[Message] = []
    async for _event in model.arun_function_calls(function_calls, results):
        pass
    [result] = results
    # The tool message's arguments, which history keeps, after Agno drops values the model sent for injected names.
    outcome["arguments"] = result.tool_args
    if result.tool_call_error:
        return {**outcome, "error_text": normalize_error_text(str(result.content))}
    return {**outcome, "kwargs": json.loads(str(result.content))}


def capture_argument_cases() -> dict[str, dict[str, object]]:
    """Return the golden outcome of every argument case."""
    functions = prepare_probe_functions()

    async def capture_all() -> dict[str, dict[str, object]]:
        return {case_id: await capture_argument_case(case, functions) for case_id, case in ARGUMENT_CASES.items()}

    return asyncio.run(capture_all())


def write_golden(path: Path, value: object, *, sort_keys: bool) -> None:
    """Write one fixture as pretty JSON, with its keys sorted or in the order they were built."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=sort_keys, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    """Rewrite the tool fixtures and report the tools left out."""
    with tempfile.TemporaryDirectory(prefix="tool-golden-") as root:
        capture = capture_tool_schemas(Path(root).resolve())
    # Schemas keep the key order providers receive, such as parameters in signature order.
    write_golden(SCHEMAS_PATH, capture.schemas, sort_keys=False)
    write_golden(ALL_ENABLED_SCHEMAS_PATH, capture.all_enabled_schemas, sort_keys=False)
    write_golden(ARGUMENTS_PATH, capture_argument_cases(), sort_keys=True)
    for tool_name, reason in capture.skipped.items():
        sys.stdout.write(f"skipped {tool_name}: {reason}\n")


if __name__ == "__main__":
    main()
