"""Enforce the zero-Agno boundary across ``src/``, ``tests/``, and ``pyproject.toml``."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPO_ROOT / "src" / "mindroom"
TESTS_ROOT = REPO_ROOT / "tests"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

_WORD_GAP = r"(?:\s|#)+"
# Phrases that justify behavior by the retired Agno runtime instead of the outcome it protects, each matched across
# line breaks and comment markers.
PARITY_WORDING = re.compile(
    "|".join(
        rf"\b{_WORD_GAP.join(words)}\b"
        for words in (
            ("kept", "as", "main"),
            ("as", "main", "(?:behaves|does|did|sends|sent|stores|stored|writes|wrote|reads|renders|selected|keeps)"),
            ("main", "behaves"),
            ("owned", "loop"),
            ("the", "owned", "runtime"),
            ("for", "parity"),
            ("as", "Agno", "(?:did|does)"),
        )
    ),
    re.IGNORECASE,
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


def parity_wording(source: str) -> list[str]:
    """Return each phrase in ``source`` that justifies behavior by the retired Agno runtime, with its line number."""
    phrases: list[str] = []
    for match in PARITY_WORDING.finditer(source):
        line = source.count("\n", 0, match.start()) + 1
        phrases.append(f"line {line}: {' '.join(match.group().replace('#', ' ').split())}")
    return phrases


def _agno_imports_under(root: Path) -> dict[str, list[str]]:
    """Return each Python module under ``root`` that imports Agno, with the Agno modules it imports."""
    violations: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        if imported := agno_imports(path.read_text(encoding="utf-8")):
            violations[path.relative_to(root).as_posix()] = sorted(imported)
    return violations


def test_agno_imports_counts_type_checking_and_function_level_imports() -> None:
    """Every import form counts, and neither a relative ``agno`` nor a package merely named like it does."""
    pkg = "agno"
    source = "\n".join(
        [
            f"import {pkg}",
            "import agnostic",
            "from typing import TYPE_CHECKING",
            f"from {pkg}.tools import Toolkit",
            "from agnostic.tools import Other",
            f"from . import {pkg} as local_agno",
            "if TYPE_CHECKING:",
            f"    from {pkg}.run import RunContext",
            "def build() -> None:",
            f"    import {pkg}.media as media",
        ],
    )

    assert agno_imports(source) == {"agno", "agno.tools", "agno.run", "agno.media"}


def test_no_source_module_imports_agno() -> None:
    """No module under ``src/mindroom`` imports the removed ``agno`` package."""
    violations = _agno_imports_under(SOURCE_ROOT)

    assert not violations, f"Agno imports found in src/mindroom: {violations}"


def test_no_test_module_imports_agno() -> None:
    """No module under ``tests`` imports the removed ``agno`` package."""
    violations = _agno_imports_under(TESTS_ROOT)

    assert not violations, f"Agno imports found in tests: {violations}"


def test_no_source_module_has_agno_compat_markers() -> None:
    """No module under ``src/mindroom`` carries retired ``AGNO_COMPAT:`` markers."""
    marker_hits = [
        path.relative_to(SOURCE_ROOT).as_posix()
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if "AGNO_COMPAT:" in path.read_text(encoding="utf-8")
    ]

    assert not marker_hits, f"Retired AGNO_COMPAT markers found in src/mindroom: {marker_hits}"


def test_pyproject_has_no_agno_dependency() -> None:
    """``pyproject.toml`` does not declare ``agno`` in any dependency list or group."""
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    project = data.get("project", {})
    requirement_specs: list[str] = list(project.get("dependencies", ()))
    for group in project.get("optional-dependencies", {}).values():
        requirement_specs.extend(group)
    for group in data.get("dependency-groups", {}).values():
        requirement_specs.extend(item for item in group if isinstance(item, str))

    agno_deps = [spec for spec in requirement_specs if Requirement(spec).name.lower() == "agno"]
    assert not agno_deps, f"pyproject.toml still declares agno dependency: {agno_deps}"


def test_parity_wording_finds_each_phrase_across_comment_lines_and_in_docstrings() -> None:
    """A phrase counts wherever a line break or a comment marker splits it; other uses of its words do not."""
    source = '''"""Render the reply. Kept as main
behaves."""

# Each result is stored as main
# does. Requests for parity with
# the owned loop run on the main thread, as Agno did.
mainline = "kept as mainline"
'''

    assert parity_wording(source) == [
        "line 1: Kept as main",
        "line 4: as main does",
        "line 5: for parity",
        "line 6: owned loop",
        "line 6: as Agno did",
    ]


def test_no_source_comment_justifies_behavior_by_the_retired_agno_runtime() -> None:
    """Source comments and docstrings state the outcome each behavior protects, not that the retired runtime had it."""
    found = {
        path.relative_to(SOURCE_ROOT).as_posix(): phrases
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if (phrases := parity_wording(path.read_text(encoding="utf-8")))
    }

    assert not found, f"State what each behavior protects, or mark it TODO(simplify), in: {found}"
