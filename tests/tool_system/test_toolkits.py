"""The native toolkit adapter reproduces the tool goldens' fields the loop reads, for every registered tool."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.llm.provider import ToolSpec
from mindroom.tool_system.authoring import Toolkit, passthrough_tool, tool
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.tool_function import ToolFunction, ToolGate
from mindroom.tool_system.toolkits import adapt_toolkit, with_offered_instructions
from tests.agno_parity import tool_argument_probes as probes
from tests.agno_parity.golden_files import render
from tests.tool_system import agno_parity_support as support

if TYPE_CHECKING:
    from collections.abc import Callable
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


def assert_same_json(actual: object, golden: object) -> None:
    """Compare values and key order, because providers receive schemas as serialized bytes."""
    assert json.dumps(actual, indent=2) == json.dumps(golden, indent=2)


@pytest.mark.parametrize(
    ("path", "golden"),
    [(support.SCHEMAS_PATH, SCHEMAS), (support.ALL_ENABLED_SCHEMAS_PATH, ALL_ENABLED_SCHEMAS)],
    ids=["schemas", "schemas_all_enabled"],
)
def test_the_schema_goldens_are_written_as_the_regeneration_writes_them(path: Path, golden: dict[str, Any]) -> None:
    """Each file lists its tools in sorted order, each entry as built, so regenerating unchanged tools rewrites nothing."""
    assert render({name: golden[name] for name in sorted(golden)}) == path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def golden_runtime(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Config, RuntimePaths]:
    """Build the golden runtime once per worker."""
    root = tmp_path_factory.mktemp("tool-golden").resolve()
    with support.isolated_process(root):
        config, runtime_paths = support.golden_runtime(root)
    return root, config, runtime_paths


def test_the_tool_goldens_cover_every_registered_tool(golden_runtime: tuple[Path, Config, RuntimePaths]) -> None:
    """A registered tool without a golden fails here, as does a golden of a tool no longer registered.

    Only the tools no offline build can produce are left out; a tool has an
    all-enabled golden exactly when it has switches to turn on.
    """
    root, _config, _runtime_paths = golden_runtime
    with support.isolated_process(root):
        registered = set(builtin_tool_registry())
        switched = {tool_name for tool_name in SCHEMAS if support.all_enabled_overrides(tool_name)}

    assert set(SCHEMAS) == registered - support.UNBUILDABLE
    assert set(ALL_ENABLED_SCHEMAS) == switched


@pytest.mark.parametrize("tool_name", sorted(SCHEMAS))
def test_adapted_tool_matches_golden(tool_name: str, golden_runtime: tuple[Path, Config, RuntimePaths]) -> None:
    """Each registered tool adapts to the golden functions, in the order it offers them, with golden instructions."""
    root, config, runtime_paths = golden_runtime
    with support.isolated_process(root):
        if (missing := support.missing_dependencies(tool_name)) is not None:
            pytest.skip(f"{tool_name} needs {missing}")
        toolkit = support.build_toolkit(tool_name, config, runtime_paths)
        adapted = adapt_toolkit(toolkit)
    assert adapted.name == toolkit.name
    assert_same_json(support.golden_entry(adapted), SCHEMAS[tool_name])


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
        overrides = support.all_enabled_overrides(tool_name)
        toolkit = support.build_toolkit(tool_name, config, runtime_paths, overrides)
        adapted = adapt_toolkit(toolkit)
    assert_same_json({"overrides": overrides, **support.golden_entry(adapted)}, golden)


def test_toolkit_instructions_reach_both_instructions_and_guidance() -> None:
    """A toolkit's instructions become both the prompt instructions and the CLI guidance."""
    authored = Toolkit(name="authored", tools=[probes.probe_no_arguments], instructions="Shown.")

    adapted = adapt_toolkit(authored)

    assert (adapted.instructions, adapted.guidance) == ("Shown.", "Shown.")


def test_with_offered_instructions_keeps_authored_settings_and_narrows_scoped_toolkits(
    golden_runtime: tuple[Path, Config, RuntimePaths],
) -> None:
    """Instructions set in a tool's settings always show, while FunctionScopedInstructions narrow to offered tools."""
    root, config, runtime_paths = golden_runtime
    authored_text = "Always verify memory context before answering."
    with support.isolated_process(root):
        if (missing := support.missing_dependencies("zep")) is not None:
            pytest.skip(f"zep needs {missing}")
        zep = support.build_toolkit(
            "zep",
            config,
            runtime_paths,
            {"enable_add_zep_message": False, "instructions": authored_text},
        )

    kept = with_offered_instructions(zep, adapt_toolkit(zep))
    assert (kept.instructions, kept.guidance) == (authored_text, authored_text)

    class _ScopedToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="scoped", tools=[self.alpha, self.beta], instructions="default")

        @tool
        def alpha(self) -> str:
            """Alpha."""
            return "a"

        @tool
        def beta(self) -> str:
            """Beta."""
            return "b"

        def instructions_for(self, function_names: object) -> str:
            names = tuple(function_names) if isinstance(function_names, (list, tuple)) else ()
            return f"Use {', '.join(names)}." if names else ""

    scoped = _ScopedToolkit()
    adapted = adapt_toolkit(scoped)
    full = with_offered_instructions(scoped, adapted)
    assert (full.instructions, full.guidance) == ("Use alpha, beta.", "Use alpha, beta.")
    narrowed = with_offered_instructions(scoped, replace(adapted, functions=adapted.functions[:1]))
    assert (narrowed.instructions, narrowed.guidance) == ("Use alpha.", "Use alpha.")
    empty = with_offered_instructions(scoped, replace(adapted, functions=()))
    assert (empty.instructions, empty.guidance) == (None, None)


def test_definition_is_the_provider_facing_tool_spec() -> None:
    """``definition()`` carries the fields a provider sends, with each tri-state kept; approval stays on the function."""

    class _GatedToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="gated", tools=[probes.probe_choice])

        def offered_function(self, function: ToolFunction) -> ToolFunction:
            return replace(function, gate=ToolGate.CONFIRM)

    class _BareToolkit(Toolkit):
        @passthrough_tool(parameters={"type": "object", "properties": {}, "required": []})
        def bare(self) -> str:
            return "x"

    [function] = adapt_toolkit(Toolkit(name="probes", tools=[probes.probe_choice])).functions
    [gated] = adapt_toolkit(_GatedToolkit()).functions
    [bare] = adapt_toolkit(_BareToolkit()).functions

    assert function.definition() == ToolSpec(
        name="probe_choice",
        description="Record a constrained choice.",
        parameters=function.parameters,
        strict=None,
    )
    assert (function.gate, gated.gate) == (None, ToolGate.CONFIRM)
    assert gated.definition() == replace(function.definition(), name=gated.name)
    # The definition keeps None for a field the tool never set, so a provider adapter can omit it.
    assert bare.definition() == ToolSpec(
        name="bare",
        description=None,
        parameters=bare.parameters,
        strict=None,
    )


@pytest.mark.asyncio
async def test_keyword_arguments_a_function_collects_reach_it_without_a_kwargs_property() -> None:
    """A function collecting ``**fields``, as ClickUp's ``update_task`` does, offers only its named parameters.

    The model sends the fields beside them, and the function receives each one as its own keyword argument.
    """

    def update_task(task_id: str, **fields: object) -> str:
        return json.dumps({"task_id": task_id, "fields": fields})

    [function] = adapt_toolkit(Toolkit(name="tasks", tools=[update_task])).functions
    outcome = await execute_tool_call(
        function,
        {"task_id": "7", "status": "done"},
        ToolCallContext(run_id="run", session_id="session", user_id=None),
        middleware=(),
    )

    assert function.parameters == {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    }
    assert json.loads(outcome.content) == {"task_id": "7", "fields": {"status": "done"}}


def _with_agent(query: str, agent: object = None) -> str:
    del agent
    return query


def _with_team(query: str, team: object = None) -> str:
    del team
    return query


def _with_run_context(query: str, run_context: object = None) -> str:
    del run_context
    return query


def _with_agno_agent(query: str, _agno_agent: object = None) -> str:
    del _agno_agent
    return query


def _with_agno_team(query: str, _agno_team: object = None) -> str:
    del _agno_team
    return query


def _with_agno_run_context(query: str, _agno_run_context: object = None) -> str:
    del _agno_run_context
    return query


@pytest.mark.parametrize(
    ("param_name", "entrypoint"),
    [
        ("agent", _with_agent),
        ("team", _with_team),
        ("run_context", _with_run_context),
        ("_agno_agent", _with_agno_agent),
        ("_agno_team", _with_agno_team),
        ("_agno_run_context", _with_agno_run_context),
    ],
)
def test_reserved_framework_parameter_names_are_refused(param_name: str, entrypoint: Callable[..., str]) -> None:
    """Framework-reserved parameter names are refused so models cannot spoof call identity."""
    with pytest.raises(ValueError, match=rf"takes {param_name}.*current_call\(\)"):
        adapt_toolkit(Toolkit(name="reserved", tools=[entrypoint]))
