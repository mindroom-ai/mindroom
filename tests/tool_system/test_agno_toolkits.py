"""The Agno toolkit adapter reproduces every field the C3 tool goldens recorded, for every registered tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from agno.run import RunContext  # noqa: TC002 - pydantic resolves the annotations at wrap time.
from agno.tools import Toolkit
from agno.tools.function import Function, FunctionCall

from mindroom.llm.provider import ToolSpec
from mindroom.tool_system.agno_toolkits import AdaptedToolkit, adapt_function, adapt_toolkit
from tests.agno_parity import tool_argument_probes as probes
from tests.tool_system import agno_parity_support as support

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

# Importing these SDKs to build their tools warns about their own deprecated usage.
pytestmark = [
    pytest.mark.filterwarnings("ignore::DeprecationWarning:daytona"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning:openbb_"),
]

SCHEMAS = json.loads(support.SCHEMAS_PATH.read_text(encoding="utf-8"))
ALL_ENABLED_SCHEMAS = json.loads(support.ALL_ENABLED_SCHEMAS_PATH.read_text(encoding="utf-8"))


def _flag(value: bool | None) -> bool:
    """Agno leaves an unset flag at None and reads it as False everywhere; ``ToolFunction`` stores that bool."""
    return bool(value)


def expected_entry(golden: dict[str, object]) -> dict[str, object]:
    """Return a golden entry as the adapter reports it: instructions only when Agno adds them, flags as bools."""
    functions = golden["functions"]
    assert isinstance(functions, list)
    return {
        "instructions": golden["instructions"] if golden["add_instructions"] else None,
        "functions": [
            {
                **function,
                "strict": _flag(function["strict"]),
                "requires_confirmation": _flag(function["requires_confirmation"]),
                "external_execution": _flag(function["external_execution"]),
            }
            for function in functions
        ],
    }


def adapted_entry(adapted: AdaptedToolkit) -> object:
    """Return every recorded field of an adapted toolkit, in the golden's key order."""
    return support.normalize_version(
        {
            "instructions": adapted.instructions,
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
                for function in adapted.functions
            ],
        },
    )


def assert_same_json(actual: object, golden: object) -> None:
    """Compare values and key order, because providers receive schemas as serialized bytes."""
    assert json.dumps(actual, indent=2) == json.dumps(golden, indent=2)


@pytest.fixture(scope="module")
def golden_runtime(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Config, RuntimePaths]:
    """Build the capture runtime once per worker."""
    root = tmp_path_factory.mktemp("tool-golden").resolve()
    with support.isolated_process(root):
        config, runtime_paths = support.golden_runtime(root)
    return root, config, runtime_paths


@pytest.mark.parametrize("tool_name", sorted(SCHEMAS))
def test_adapted_tool_matches_golden(tool_name: str, golden_runtime: tuple[Path, Config, RuntimePaths]) -> None:
    """Each registered tool adapts to the golden functions, in Agno's async-run order, with the golden instructions."""
    root, config, runtime_paths = golden_runtime
    with support.isolated_process(root):
        if (missing := support.missing_dependencies(tool_name)) is not None:
            pytest.skip(f"{tool_name} needs {missing}")
        toolkit = support.build_toolkit(tool_name, config, runtime_paths)
        adapted = adapt_toolkit(toolkit)
    assert adapted.name == toolkit.name
    assert_same_json(adapted_entry(adapted), expected_entry(SCHEMAS[tool_name]))


@pytest.mark.parametrize("tool_name", sorted(ALL_ENABLED_SCHEMAS))
def test_adapted_all_enabled_tool_matches_golden(
    tool_name: str,
    golden_runtime: tuple[Path, Config, RuntimePaths],
) -> None:
    """Each registered tool with every function switched on adapts to its all-enabled golden."""
    root, config, runtime_paths = golden_runtime
    golden = ALL_ENABLED_SCHEMAS[tool_name]
    with support.isolated_process(root):
        if (missing := support.missing_dependencies(tool_name)) is not None:
            pytest.skip(f"{tool_name} needs {missing}")
        assert support.all_enabled_overrides(tool_name) == golden["overrides"]
        toolkit = support.build_toolkit(tool_name, config, runtime_paths, golden["overrides"])
        adapted = adapt_toolkit(toolkit)
    assert_same_json(adapted_entry(adapted), expected_entry(golden))


def test_adapting_leaves_the_shared_function_untouched() -> None:
    """Processing happens on a copy: the toolkit's own Function keeps its raw entrypoint and default schema."""
    toolkit = Toolkit(name="probes", tools=[probes.probe_scalars])
    function = toolkit.functions["probe_scalars"]
    parameters_before = json.dumps(function.parameters)

    adapted = adapt_function(function)

    assert function.entrypoint is probes.probe_scalars
    assert json.dumps(function.parameters) == parameters_before
    assert adapted.entrypoint is not probes.probe_scalars
    assert adapted.parameters["properties"]["count"] == {"type": "integer", "description": "A whole number."}
    assert adapted.parameters["required"] == ["count", "ratio", "enabled", "label"]


def test_async_variants_replace_sync_functions_in_place() -> None:
    """An async run uses the async variant of a name in the sync function's slot, then the async-only functions."""

    def first() -> str:
        return "first"

    def second() -> str:
        return "second"

    async def async_second() -> str:
        return "async second"

    async def third() -> str:
        return "third"

    toolkit = Toolkit(name="order", tools=[first, second, third], async_tools=[(async_second, "second")])

    adapted = adapt_toolkit(toolkit)

    assert [function.name for function in adapted.functions] == ["first", "second", "third"]
    assert adapted.functions[1].entrypoint.__wrapped__ is async_second
    assert [function.name for function in adapt_toolkit(toolkit, async_mode=False).functions] == [
        "first",
        "second",
    ]
    assert adapt_toolkit(toolkit, async_mode=False).functions[1].entrypoint.__wrapped__ is second


def test_instructions_are_adapted_only_when_agno_adds_them() -> None:
    """Toolkit instructions reach the prompt only with ``add_instructions``; the adapter reports the same."""
    silent = Toolkit(name="silent", tools=[probes.probe_no_arguments], instructions="Not added.")
    spoken = Toolkit(name="spoken", tools=[probes.probe_no_arguments], instructions="Added.", add_instructions=True)

    assert adapt_toolkit(silent).instructions is None
    assert adapt_toolkit(spoken).instructions == "Added."


def test_definition_is_the_provider_facing_tool_spec() -> None:
    """``definition()`` carries the processed name, description, schema, and strict flag; no description is empty."""
    [function] = adapt_toolkit(Toolkit(name="probes", tools=[probes.probe_choice])).functions

    assert function.definition() == ToolSpec(
        name="probe_choice",
        description="Record a constrained choice.",
        parameters=function.parameters,
        strict=False,
    )
    bare = adapt_function(Function(name="bare", entrypoint=lambda: "x", skip_entrypoint_processing=True))
    assert bare.description is None
    assert bare.definition().description == ""


def test_injected_parameters_are_the_names_the_framework_supplies() -> None:
    """Reserved names and identity-typed parameters are hidden from the model and recorded for injection."""

    def tool(query: str, agent: object = None, ctx: RunContext | None = None, fc: FunctionCall | None = None) -> str:
        del agent, ctx, fc
        return query

    [function] = adapt_toolkit(Toolkit(name="probes", tools=[tool])).functions

    assert function.injected_params == {"agent", "ctx", "fc"}
    assert list(function.parameters["properties"]) == ["query"]
