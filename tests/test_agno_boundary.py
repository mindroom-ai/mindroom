"""Only the tool and knowledge-reader boundary under ``src/mindroom`` imports Agno.

Agno stays MindRoom's tool library: ``Toolkit``, ``Function``, ``@tool``, and ``ToolResult`` remain the tool authoring
API, and Agno's knowledge readers stay. The built-in, custom, OAuth, and MCP tool packages, the toolkit adapter, the
skills toolkit, and the knowledge reader module may import Agno; ``logging_config`` may too, because it routes Agno's
loggers, which vendor toolkits log through. Every other module must take Agno's tool types from
``mindroom.tool_system.agno_toolkits``.
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


def agno_imports(source: str) -> set[str]:
    """Return the Agno modules ``source`` imports anywhere, including under ``if TYPE_CHECKING:`` and in functions."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            modules.add(node.module)
    return {module for module in modules if module == "agno" or module.startswith("agno.")}


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
