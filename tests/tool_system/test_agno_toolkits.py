"""The Agno toolkit adapter reproduces the C3 tool goldens' fields the owned loop reads, for every registered tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from agno.run import RunContext  # noqa: TC002 - pydantic resolves the annotations at wrap time.
from agno.tools import Toolkit
from agno.tools.function import Function, FunctionCall

from mindroom.llm.provider import ToolSpec
from mindroom.tool_system import agno_toolkits
from mindroom.tool_system.agno_toolkits import adapt_function, adapt_toolkit, injected_parameter_names
from mindroom.tool_system.registry_state import BUILTIN_TOOL_METADATA
from tests.agno_parity import regenerate_tool_goldens
from tests.agno_parity import tool_argument_probes as probes
from tests.tool_system import agno_parity_support as support

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.tool_function import AdaptedToolkit

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


def assert_instructions_as_golden(toolkit: Toolkit, adapted: AdaptedToolkit, golden: dict[str, Any]) -> None:
    """The adapter carries the toolkit's instructions only when Agno added them, and no function sets its own."""
    expected = golden["instructions"] if golden["add_instructions"] else None
    assert support.normalize_version(adapted.instructions) == expected
    assert all(function.instructions is None for function in toolkit.get_async_functions().values())


@pytest.fixture(scope="module")
def golden_runtime(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Config, RuntimePaths]:
    """Build the golden runtime once per worker."""
    root = tmp_path_factory.mktemp("tool-golden").resolve()
    with support.isolated_process(root):
        config, runtime_paths = support.golden_runtime(root)
    return root, config, runtime_paths


def test_the_tool_goldens_cover_every_registered_tool(golden_runtime: tuple[Path, Config, RuntimePaths]) -> None:
    """A registered tool without a golden fails here, as does a golden of a tool no longer registered.

    Only the tools no offline build can produce are left out, the ones the regeneration module skips; a tool has an
    all-enabled golden exactly when it has switches to turn on.
    """
    root, _config, _runtime_paths = golden_runtime
    with support.isolated_process(root):
        registered = set(BUILTIN_TOOL_METADATA)
        switched = {tool_name for tool_name in SCHEMAS if support.all_enabled_overrides(tool_name)}

    assert set(SCHEMAS) == registered - regenerate_tool_goldens.UNBUILDABLE
    assert set(ALL_ENABLED_SCHEMAS) == switched


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
    assert_same_json(support.golden_entry(toolkit, adapted), SCHEMAS[tool_name])
    assert_instructions_as_golden(toolkit, adapted, SCHEMAS[tool_name])


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
    assert_same_json({"overrides": overrides, **support.golden_entry(toolkit, adapted)}, golden)
    assert_instructions_as_golden(toolkit, adapted, golden)


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


def _noop_hook(*_args: object, **_kwargs: object) -> None:
    return None


def _lookup(city: str) -> str:
    return city


@pytest.mark.parametrize(
    "hooks",
    [{"pre_hook": _noop_hook}, {"post_hook": _noop_hook}, {"tool_hooks": [_noop_hook]}],
    ids=["pre_hook", "post_hook", "tool_hooks"],
)
def test_functions_with_agno_hooks_are_rejected_instead_of_running_without_them(hooks: dict[str, Any]) -> None:
    """MindRoom does not run Agno's per-function hooks, so a tool that sets one fails loudly when adapted."""
    function = Function(name="lookup", entrypoint=_lookup, **hooks)

    with pytest.raises(ValueError, match=r"'lookup' sets an Agno pre_hook, post_hook, or tool_hooks"):
        adapt_function(function)


def test_every_field_of_agnos_function_is_named_by_one_row_of_the_adapter_table() -> None:
    """Each ``Function`` field is mapped, rejected when set, or ignored by decision; a field Agno adds fails here."""
    named = [name for names in agno_toolkits._FUNCTION_FIELDS for name in names]

    assert sorted(named) == sorted(Function.model_fields)


def _lookup_country(country: str) -> str:
    return country


# A value for each field of Agno's ``Function`` that a tool may set without being rejected, other than its default.
_SET_FIELDS: dict[str, object] = {
    "name": "find",
    "description": "Find a city.",
    "parameters": {"type": "object", "properties": {"town": {"type": "string"}}, "required": ["town"]},
    "strict": True,
    "entrypoint": _lookup_country,
    "skip_entrypoint_processing": True,
    "requires_confirmation": True,
    "approval_type": "mindroom_policy",
    "external_execution": True,
    "external_execution_silent": True,
    "stop_after_tool_call": True,
    "user_input_fields": ["city"],
    "show_result": True,
    "cache_results": True,
    "cache_dir": "tool-cache",
    "cache_ttl": 60,
    "title": "Lookup",
    "annotations": {"readOnlyHint": True},
    "has_side_effects": True,
    "owning_toolkit": "atlas",
    "source_toolkit": object(),
}


def _what_the_loop_reads(function: Function) -> tuple[object, ...]:
    adapted = adapt_function(function)
    return (
        adapted.definition(),
        adapted.stop_after_tool_call,
        adapted.external_execution_silent,
        adapted.entrypoint_processed,
        adapted.injected_params,
    )


@pytest.mark.parametrize("field", sorted(_SET_FIELDS))
def test_a_function_field_changes_the_adapted_function_exactly_when_the_table_maps_it(field: str) -> None:
    """Setting a mapped field changes what the loop reads of the adapted function; setting an ignored one does not."""
    [handling] = [handling for names, handling in agno_toolkits._FUNCTION_FIELDS.items() if field in names]
    base: dict[str, Any] = {"name": "lookup", "entrypoint": _lookup}

    changed = _what_the_loop_reads(Function(**{**base, field: _SET_FIELDS[field]}))

    assert (changed != _what_the_loop_reads(Function(**base))) is (handling == "mapped")


def test_every_mapped_or_ignored_field_has_a_value_to_set() -> None:
    """The parametrized check above covers every field the table maps or ignores."""
    covered = {
        name
        for names, handling in agno_toolkits._FUNCTION_FIELDS.items()
        if isinstance(handling, str)
        for name in names
    }
    # Agno reads these only for a function that requires user input, which the table rejects, so no other value
    # of theirs can reach a run.
    assert covered - set(_SET_FIELDS) == {"user_input_schema"}


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"requires_user_input": True}, r"'lookup' sets Agno's requires_user_input"),
        ({"instructions": "Call it twice."}, r"'lookup' sets instructions for Agno to add to the system prompt"),
    ],
    ids=["requires_user_input", "instructions"],
)
def test_functions_asking_for_what_the_owned_loop_never_does_are_rejected(options: dict[str, Any], error: str) -> None:
    """A function that needs user input or adds its own prompt instructions fails loudly instead of running without."""
    with pytest.raises(ValueError, match=error):
        adapt_function(Function(name="lookup", entrypoint=_lookup, **options))


def test_fields_agno_would_not_act_on_adapt_unchanged() -> None:
    """Instructions Agno keeps out of the prompt, and user-input fields without the pause, are left unread."""
    plain = adapt_function(Function(name="lookup", entrypoint=_lookup))
    quiet = adapt_function(
        Function(
            name="lookup",
            entrypoint=_lookup,
            instructions="Unseen.",
            add_instructions=False,
            user_input_fields=["city"],
        ),
    )

    assert quiet.definition() == plain.definition()


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


def test_instructions_are_adapted_only_when_agno_adds_them() -> None:
    """Toolkit instructions reach the prompt only with ``add_instructions``; the adapter reports the same."""
    silent = Toolkit(name="silent", tools=[probes.probe_no_arguments], instructions="Not added.")
    spoken = Toolkit(name="spoken", tools=[probes.probe_no_arguments], instructions="Added.", add_instructions=True)

    assert adapt_toolkit(silent).instructions is None
    assert adapt_toolkit(spoken).instructions == "Added."


def test_definition_is_the_provider_facing_tool_spec() -> None:
    """``definition()`` carries every field Agno serialized into a tool definition, with each tri-state kept."""
    [function] = adapt_toolkit(Toolkit(name="probes", tools=[probes.probe_choice])).functions
    [gated] = adapt_toolkit(
        Toolkit(
            name="gated",
            tools=[probes.probe_choice],
            requires_confirmation_tools=["probe_choice"],
            external_execution_required_tools=["probe_choice"],
        ),
    ).functions
    bare = adapt_function(Function(name="bare", entrypoint=lambda: "x", skip_entrypoint_processing=True))

    # A plain Toolkit leaves ``strict`` unset and the approval flags False; MindRoom's wrappers set ``strict`` False.
    assert function.definition() == ToolSpec(
        name="probe_choice",
        description="Record a constrained choice.",
        parameters=function.parameters,
        strict=None,
        requires_confirmation=False,
        external_execution=False,
        approval_type=None,
    )
    assert (gated.requires_confirmation, gated.external_execution) == (True, True)
    assert (gated.definition().requires_confirmation, gated.definition().external_execution) == (True, True)
    # Agno omitted these from the wire; the definition keeps None so a provider adapter can omit them too.
    assert bare.definition() == ToolSpec(
        name="bare",
        description=None,
        parameters=bare.parameters,
        strict=None,
        requires_confirmation=None,
        external_execution=None,
        approval_type=None,
    )


def test_injected_parameters_are_the_names_the_framework_supplies() -> None:
    """Reserved names and identity-typed parameters are hidden from the model and recorded for injection."""

    def tool(query: str, agent: object = None, ctx: RunContext | None = None, fc: FunctionCall | None = None) -> str:
        del agent, ctx, fc
        return query

    [function] = adapt_toolkit(Toolkit(name="probes", tools=[tool])).functions

    assert function.injected_params == {"agent", "ctx", "fc"}
    assert list(function.parameters["properties"]) == ["query"]


def test_unresolvable_annotations_leave_only_the_reserved_names_injected() -> None:
    """When a tool's annotations do not resolve, as with a ``TYPE_CHECKING``-only import, only reserved names inject."""

    def tool(
        config: Config,
        run_context: RunContext,
        ctx: RunContext | None = None,
        fc: FunctionCall | None = None,
    ) -> str:
        del config, run_context, ctx, fc
        return "ok"

    # ``Config`` is imported only for type checking here, so no annotation resolves and ``ctx`` is not recognized.
    assert injected_parameter_names(tool) == {"run_context", "fc"}
