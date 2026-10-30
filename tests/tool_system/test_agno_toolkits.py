"""The Agno toolkit adapter reproduces the tool goldens' fields the owned loop reads, for every registered tool."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from agno.run import RunContext  # noqa: TC002 - pydantic resolves the annotations at wrap time.
from agno.tools import Toolkit
from agno.tools.function import Function, FunctionCall
from agno.tools.reasoning import ReasoningTools
from agno.utils.schema import AGNO_INJECTED_PARAMS, FRAMEWORK_INJECTED_PARAMS

from mindroom.llm.provider import ToolSpec
from mindroom.tool_system import agno_toolkits
from mindroom.tool_system.agno_toolkits import (
    adapt_function,
    adapt_toolkit,
    injected_parameters,
    with_offered_instructions,
)
from mindroom.tool_system.authoring import Toolkit as OwnedToolkit
from mindroom.tool_system.authoring import tool
from mindroom.tool_system.registry import builtin_tool_registry
from tests.agno_parity import tool_argument_probes as probes
from tests.agno_parity.golden_files import render
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


def assert_same_json(actual: object, golden: object) -> None:
    """Compare values and key order, because providers receive schemas as serialized bytes."""
    assert json.dumps(actual, indent=2) == json.dumps(golden, indent=2)


def assert_no_function_instructions(toolkit: Toolkit) -> None:
    """No function sets instructions of its own, so the toolkit's are all the prompt and the guidance carry."""
    assert all(function.instructions is None for function in toolkit.get_async_functions().values())


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
    """Each registered tool adapts to the golden functions, in Agno's async-run order, with the golden instructions."""
    root, config, runtime_paths = golden_runtime
    with support.isolated_process(root):
        if (missing := support.missing_dependencies(tool_name)) is not None:
            pytest.skip(f"{tool_name} needs {missing}")
        toolkit = support.build_toolkit(tool_name, config, runtime_paths)
        adapted = adapt_toolkit(toolkit)
    assert adapted.name == toolkit.name
    assert_same_json(support.golden_entry(adapted), SCHEMAS[tool_name])
    assert_no_function_instructions(toolkit)


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
    assert_no_function_instructions(toolkit)


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


def test_the_reserved_names_are_every_name_agno_fills_itself() -> None:
    """A name Agno starts reserving fails here, instead of silently becoming a parameter the model may fill."""
    assert set(agno_toolkits._RESERVED_NAMES) == {*FRAMEWORK_INJECTED_PARAMS, *AGNO_INJECTED_PARAMS, "fc"}


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
        (adapted.requires_confirmation, adapted.external_execution, adapted.approval_type),
        adapted.stop_after_call,
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


def test_every_toolkit_s_instructions_reach_the_prompt_whatever_its_add_instructions() -> None:
    """A toolkit authored on ``Toolkit`` and one of Agno's own both show their instructions; ``add_instructions`` is unread."""
    authored = Toolkit(name="authored", tools=[probes.probe_no_arguments], instructions="Shown.")
    vendor = ReasoningTools()

    assert (authored.add_instructions, vendor.add_instructions) == (False, False)
    assert (adapt_toolkit(authored).instructions, adapt_toolkit(authored).guidance) == ("Shown.", "Shown.")
    assert adapt_toolkit(vendor).instructions == adapt_toolkit(vendor).guidance == vendor.instructions


@pytest.mark.parametrize(
    ("tool_name", "offering", "leaving_one_out"),
    [
        ("daytona", {}, {"exclude_tools": ["delete_file"]}),
        ("gmail", {}, {"send_email": False}),
        ("google_calendar", {}, {"allow_update": False}),
        ("reasoning", {}, {"enable_think": False}),
        ("zep", {}, {"enable_add_zep_message": False}),
    ],
)
def test_agno_default_instructions_show_only_while_every_function_they_name_is_offered(
    tool_name: str,
    offering: dict[str, object],
    leaving_one_out: dict[str, object],
    golden_runtime: tuple[Path, Config, RuntimePaths],
) -> None:
    """Agno's default instructions show while the agent offers every function they name; authored ones always show."""
    root, config, runtime_paths = golden_runtime
    authorable = any(
        field.name == "instructions" for field in builtin_tool_registry().entries[tool_name].config_fields or ()
    )
    with support.isolated_process(root):
        if (missing := support.missing_dependencies(tool_name)) is not None:
            pytest.skip(f"{tool_name} needs {missing}")
        full = support.build_toolkit(tool_name, config, runtime_paths, offering or None)
        narrowed = support.build_toolkit(tool_name, config, runtime_paths, leaving_one_out or None)
        # Authored text naming every function, the one left out too, still shows.
        authored_text = "Use " + ", ".join(function.name for function in adapt_toolkit(full).functions) + "."
        authored = (
            support.build_toolkit(
                tool_name,
                config,
                runtime_paths,
                {**leaving_one_out, "instructions": authored_text},
            )
            if authorable
            else None
        )

    shown = with_offered_instructions(full, adapt_toolkit(full), authored_instructions=False)
    assert shown.instructions == shown.guidance == full.instructions
    assert adapt_toolkit(narrowed).instructions == full.instructions
    hidden = with_offered_instructions(narrowed, adapt_toolkit(narrowed), authored_instructions=False)
    assert (hidden.instructions, hidden.guidance) == (None, None)
    assert len(hidden.functions) < len(shown.functions)
    if authored is not None:
        kept = with_offered_instructions(authored, adapt_toolkit(authored), authored_instructions=True)
        assert kept.instructions == authored_text


def test_definition_is_the_provider_facing_tool_spec() -> None:
    """``definition()`` carries the fields a provider sends, with each tri-state kept; approval stays on the function."""
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
    )
    assert (gated.requires_confirmation, gated.external_execution) == (True, True)
    assert gated.definition() == replace(function.definition(), name=gated.name)
    # The definition keeps None for a field the tool never set, so a provider adapter can omit it.
    assert bare.definition() == ToolSpec(
        name="bare",
        description=None,
        parameters=bare.parameters,
        strict=None,
    )


def test_injected_parameters_are_the_names_the_framework_supplies() -> None:
    """Reserved names and identity-typed parameters are hidden from the model and recorded for injection."""

    def tool(query: str, agent: object = None, ctx: RunContext | None = None, fc: FunctionCall | None = None) -> str:
        del agent, ctx, fc
        return query

    [function] = adapt_toolkit(Toolkit(name="probes", tools=[tool])).functions

    assert function.injected_params == {"agent": "actor", "ctx": "run_context", "fc": "default"}
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
    assert injected_parameters(tool) == {"run_context": "run_context", "fc": "default"}


class _RunContextByTypeTools(OwnedToolkit):
    @tool
    def whoami(self, query: str, ctx: RunContext | None = None) -> str:
        return f"{query}{ctx}"


class _RunContextBareTypeTools(OwnedToolkit):
    @tool
    def whoami(self, query: str, ctx: RunContext) -> str:
        return f"{query}{ctx}"


@pytest.mark.parametrize("toolkit_class", [_RunContextByTypeTools, _RunContextBareTypeTools])
def test_an_owned_function_that_takes_the_run_context_by_type_is_refused(toolkit_class: type[OwnedToolkit]) -> None:
    """A parameter Agno would fill with its run context by type is refused, so the model cannot send one."""
    with pytest.raises(ValueError, match=r"'whoami' takes ctx.*current_call\(\)"):
        adapt_toolkit(toolkit_class())
