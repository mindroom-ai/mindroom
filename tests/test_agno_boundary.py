"""Only the tool and knowledge-reader boundary under ``src/mindroom`` imports Agno, and Agno's reach only shrinks.

Agno stays MindRoom's tool library: ``Toolkit``, ``Function``, ``@tool``, and ``ToolResult`` remain the tool authoring
API, and Agno's knowledge readers stay. The built-in, custom, OAuth, and MCP tool packages, the toolkit adapter, the
skills toolkit, and the knowledge reader module may import Agno; ``logging_config`` may too, because it routes Agno's
loggers, which vendor toolkits log through. Every other module must take Agno's tool types from
``mindroom.tool_system.agno_toolkits``.

The ratchets below list the modules that still take an Agno type through that re-export and the test modules that
import Agno. Each list must match the code exactly, so a module that stops leaves its list in the same change, and no
module may join one.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "mindroom"
# Paths under ``src/mindroom`` that may import Agno: whole packages, then single modules.
ALLOWED_PACKAGES = frozenset({"tools", "custom_tools", "oauth", "mcp"})
ALLOWED_MODULES = frozenset(
    {"tool_system/agno_toolkits.py", "tool_system/skills.py", "knowledge/readers.py", "logging_config.py"},
)
REEXPORT = "tool_system/agno_toolkits.py"
# Modules outside the boundary that take an Agno type through the re-export, until each moves to MindRoom's types.
REEXPORT_AGNO_TYPE_IMPORTERS = frozenset(
    {
        "agent_cli/bash.py",
        "agents.py",
        "ai.py",
        "api/computer_browser_binding.py",
        "api/sandbox_runner.py",
        "delegation/personas.py",
        "knowledge/search.py",
        "matrix_rtc/call_tools.py",
        "mcp_gateway/toolkits.py",
        "mcp_gateway/tools.py",
        "media_delivery.py",
        "script_runs/broker.py",
        "script_runs/policy.py",
        "tool_jobs/authorization.py",
        "tool_jobs/execution.py",
        "tool_jobs/execution_authority.py",
        "tool_system/construction.py",
        "tool_system/dynamic_toolkits.py",
        "tool_system/google_workspaces.py",
        "tool_system/media_attachments.py",
        "tool_system/media_transport.py",
        "tool_system/metadata.py",
        "tool_system/output_files.py",
        "tool_system/registry_state.py",
        "tool_system/runtime_context.py",
        "tool_system/sandbox_proxy.py",
        "tool_system/toolkit_aliases.py",
        "tool_system/toolkit_connections.py",
        "tool_system/worker_media.py",
        "worker_computer/mcp_provider.py",
    },
)
TESTS_ROOT = Path(__file__).resolve().parent
# How many test modules import Agno at all, and which import its runtime types, until the tests move to MindRoom's
# seams (#3120).
TEST_AGNO_IMPORTER_COUNT = 119
AGNO_RUNTIME_MODULES = ("agno.agent", "agno.run", "agno.models", "agno.team", "agno.session")
TEST_AGNO_RUNTIME_IMPORTERS = frozenset(
    {
        "agent_loop/test_run.py",
        "team_runtime/test_continue_team.py",
        "test_agent_cli_standard_mode.py",
        "test_agent_cli_turn.py",
        "test_agent_tool_calls.py",
        "test_agent_turn_runtime.py",
        "test_claude_agent_nightly_soak.py",
        "test_claude_agent_tool.py",
        "test_cli_approval_recovery.py",
        "test_compact_context.py",
        "test_mcp_gateway_tools.py",
        "test_minimal_agent_parity.py",
        "test_openai_compat.py",
        "test_script_tool_broker.py",
        "test_tool_job_execution.py",
        "test_worker_tool_transport.py",
        "tool_system/test_agno_toolkits.py",
        "tool_system/test_tool_invocation.py",
    },
)


def agno_imports(source: str) -> set[str]:
    """Return the Agno modules ``source`` imports anywhere, including under ``if TYPE_CHECKING:`` and in functions."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            modules.add(node.module)
    return {module for module in modules if module == "agno" or module.startswith("agno.")}


def reexported_agno_names(source: str) -> set[str]:
    """Return the names ``source`` binds at module level from Agno, which importers of it reach Agno through."""
    return {
        alias.asname or alias.name
        for node in ast.parse(source).body
        if isinstance(node, ast.ImportFrom) and node.module is not None and agno_imports(f"import {node.module}")
        for alias in node.names
    }


def reexport_agno_type_importers() -> set[str]:
    """Return each module outside the allowed paths that imports an Agno name from the re-export."""
    agno_names = reexported_agno_names((SOURCE_ROOT / REEXPORT).read_text(encoding="utf-8"))
    importers: set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT)
        if relative.parts[0] in ALLOWED_PACKAGES or relative.as_posix() in ALLOWED_MODULES:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "mindroom.tool_system.agno_toolkits"
                and any(alias.name in agno_names for alias in node.names)
            ):
                importers.add(relative.as_posix())
    return importers


def agno_test_imports() -> dict[str, set[str]]:
    """Return each test module that imports Agno, with the Agno modules it imports."""
    imports = {
        path.relative_to(TESTS_ROOT).as_posix(): agno_imports(path.read_text(encoding="utf-8"))
        for path in sorted(TESTS_ROOT.rglob("*.py"))
    }
    return {module: imported for module, imported in imports.items() if imported}


def boundary_violations() -> dict[str, list[str]]:
    """Return each module outside the allowed paths that imports Agno, with the Agno modules it imports."""
    violations: dict[str, list[str]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT)
        if relative.parts[0] in ALLOWED_PACKAGES or relative.as_posix() in ALLOWED_MODULES:
            continue
        if imported := agno_imports(path.read_text(encoding="utf-8")):
            violations[relative.as_posix()] = sorted(imported)
    return violations


def test_agno_imports_counts_type_checking_and_function_level_imports() -> None:
    """Every import form counts, and neither a relative ``agno`` nor a package merely named like it does."""
    source = """
import agno
import agnostic
from typing import TYPE_CHECKING

from agno.tools import Toolkit
from agnostic.tools import Other
from . import agno as local_agno

if TYPE_CHECKING:
    from agno.run import RunContext


def build() -> None:
    import agno.media as media
"""

    assert agno_imports(source) == {"agno", "agno.tools", "agno.run", "agno.media"}


def test_no_module_outside_the_boundary_imports_agno() -> None:
    """Every ``agno`` import under ``src/mindroom`` sits in an allowed package or module."""
    violations = boundary_violations()

    assert not violations, f"Agno imports outside the boundary: {violations}"


def test_reexported_agno_names_are_the_names_the_reexport_binds_from_agno() -> None:
    """A name counts when the re-export imports it from Agno, under its alias; MindRoom's own names do not."""
    source = """
from agno.media import Image as AgnoImage
from agno.tools import Toolkit
from mindroom.tool_system.tool_function import ToolFunction


def warm() -> None:
    from agno.agent import Agent
"""

    assert reexported_agno_names(source) == {"AgnoImage", "Toolkit"}


def test_the_modules_that_take_an_agno_type_through_the_reexport_are_exactly_the_listed_ones() -> None:
    """No module may start taking Agno's tool types through the re-export, and one that stops leaves the list."""
    importers = reexport_agno_type_importers()

    joined = sorted(importers - REEXPORT_AGNO_TYPE_IMPORTERS)
    assert not joined, f"Modules that newly take an Agno type through {REEXPORT}: {joined}"
    left = sorted(REEXPORT_AGNO_TYPE_IMPORTERS - importers)
    assert not left, f"Delete these modules from REEXPORT_AGNO_TYPE_IMPORTERS, since they no longer import Agno: {left}"


def test_the_test_modules_that_import_agno_are_exactly_the_counted_and_listed_ones() -> None:
    """No new test module may import Agno or its runtime types, and one that stops lowers the cap or leaves the list."""
    imports = agno_test_imports()
    runtime_importers = {
        module
        for module, imported in imports.items()
        if any(
            name == runtime or name.startswith(f"{runtime}.") for name in imported for runtime in AGNO_RUNTIME_MODULES
        )
    }

    assert len(imports) <= TEST_AGNO_IMPORTER_COUNT, (
        f"{len(imports)} test modules import Agno, more than the {TEST_AGNO_IMPORTER_COUNT} allowed"
    )
    assert len(imports) == TEST_AGNO_IMPORTER_COUNT, (
        f"Lower TEST_AGNO_IMPORTER_COUNT to {len(imports)}, since fewer test modules import Agno"
    )
    joined = sorted(runtime_importers - TEST_AGNO_RUNTIME_IMPORTERS)
    assert not joined, f"Test modules that newly import Agno runtime types: {joined}"
    left = sorted(TEST_AGNO_RUNTIME_IMPORTERS - runtime_importers)
    assert not left, (
        f"Delete these modules from TEST_AGNO_RUNTIME_IMPORTERS, since they no longer import Agno's runtime types: {left}"
    )
