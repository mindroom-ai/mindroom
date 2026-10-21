"""The current Agno-backed tool system reproduces the tool schema and argument goldens.

Deleted together with Agno; owned implementations test against the same fixtures.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.tool_system.registry_state import BUILTIN_TOOL_METADATA
from tests.agno_parity import capture_tools

if TYPE_CHECKING:
    from pathlib import Path

    from agno.tools.function import Function

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

# Importing these SDKs to build their tools warns about their own deprecated usage.
pytestmark = [
    pytest.mark.filterwarnings("ignore::DeprecationWarning:daytona"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning:openbb_"),
]

SCHEMAS = json.loads(capture_tools.SCHEMAS_PATH.read_text(encoding="utf-8"))
ALL_ENABLED_SCHEMAS = json.loads(capture_tools.ALL_ENABLED_SCHEMAS_PATH.read_text(encoding="utf-8"))
ARGUMENTS = json.loads(capture_tools.ARGUMENTS_PATH.read_text(encoding="utf-8"))


def assert_same_json(actual: object, golden: object) -> None:
    """Compare values and key order, because providers receive schemas as serialized bytes."""
    assert json.dumps(actual, indent=2) == json.dumps(golden, indent=2)


@pytest.fixture(scope="module")
def golden_runtime(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Config, RuntimePaths]:
    """Build the capture runtime once per worker."""
    root = tmp_path_factory.mktemp("tool-golden").resolve()
    with capture_tools.isolated_process(root):
        config, runtime_paths = capture_tools.golden_runtime(root)
    return root, config, runtime_paths


@pytest.fixture(scope="module")
def probe_functions() -> dict[str, Function]:
    """Prepare the argument probes once per worker."""
    return capture_tools.prepare_probe_functions()


@pytest.mark.parametrize("tool_name", sorted(SCHEMAS))
def test_tool_schema_matches_golden(tool_name: str, golden_runtime: tuple[Path, Config, RuntimePaths]) -> None:
    """Each registered tool still reaches the model with its golden definition and instructions."""
    root, config, runtime_paths = golden_runtime
    with capture_tools.isolated_process(root):
        if (missing := capture_tools.missing_dependencies(tool_name)) is not None:
            pytest.skip(f"{tool_name} needs {missing}")
        toolkit = capture_tools.build_toolkit(tool_name, config, runtime_paths)
        assert_same_json(capture_tools.tool_schema(toolkit), SCHEMAS[tool_name])


@pytest.mark.parametrize("tool_name", sorted(ALL_ENABLED_SCHEMAS))
def test_all_enabled_tool_schema_matches_golden(
    tool_name: str,
    golden_runtime: tuple[Path, Config, RuntimePaths],
) -> None:
    """Each registered tool with every function switched on still reaches the model with its golden definition."""
    root, config, runtime_paths = golden_runtime
    with capture_tools.isolated_process(root):
        if (missing := capture_tools.missing_dependencies(tool_name)) is not None:
            pytest.skip(f"{tool_name} needs {missing}")
        assert_same_json(
            capture_tools.all_enabled_tool_schema(tool_name, config, runtime_paths),
            ALL_ENABLED_SCHEMAS[tool_name],
        )


def test_schema_golden_covers_every_buildable_registered_tool(
    golden_runtime: tuple[Path, Config, RuntimePaths],
) -> None:
    """A registered tool is missing from the goldens only when it cannot be built here."""
    root, _config, _runtime_paths = golden_runtime
    with capture_tools.isolated_process(root):
        uncovered = {
            tool_name
            for tool_name in BUILTIN_TOOL_METADATA
            if tool_name not in SCHEMAS
            and tool_name not in capture_tools.UNBUILDABLE
            and capture_tools.missing_dependencies(tool_name) is None
        }
    assert uncovered == set()
    assert set(SCHEMAS) <= set(BUILTIN_TOOL_METADATA)
    assert set(ALL_ENABLED_SCHEMAS) == {
        tool_name for tool_name in SCHEMAS if capture_tools.all_enabled_overrides(tool_name)
    }


@pytest.mark.usefixtures("golden_runtime")
def test_schema_goldens_cover_every_declared_function() -> None:
    """Every function a tool declares is in its default or all-enabled golden, unless no configuration registers it."""
    unregistered = {}
    for tool_name, entry in SCHEMAS.items():
        captured = {
            function["name"]
            for golden in (entry, ALL_ENABLED_SCHEMAS.get(tool_name, {"functions": []}))
            for function in golden["functions"]
        }
        if missing := set(BUILTIN_TOOL_METADATA[tool_name].function_names) - captured:
            unregistered[tool_name] = missing
    assert unregistered == {
        tool_name: set(function_names) for tool_name, function_names in capture_tools.NEVER_REGISTERED_FUNCTIONS.items()
    }


def test_argument_golden_holds_every_case() -> None:
    """Every defined argument case has a golden outcome, and the golden holds no stale case."""
    assert set(ARGUMENTS) == set(capture_tools.ARGUMENT_CASES)


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", sorted(capture_tools.ARGUMENT_CASES))
async def test_argument_case_matches_golden(case_id: str, probe_functions: dict[str, Function]) -> None:
    """Agno decodes, coerces, and rejects each raw tool call exactly as the golden records."""
    outcome = await capture_tools.capture_argument_case(capture_tools.ARGUMENT_CASES[case_id], probe_functions)
    assert json.loads(json.dumps(outcome)) == ARGUMENTS[case_id]
