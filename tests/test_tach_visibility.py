"""Ratchet Tach coverage and keep the exact module graph acyclic.

Tach checks each import as made by the nearest declared module that contains the importing file, and it has no exact
mode for `visibility`, so an entry naming an undeclared module matches nothing.
A grant to a declared module whose `depends_on` lacks the granting module pre-approves an import that no code makes,
because `exact = true` keeps every `depends_on` list equal to the module's runtime imports.
This test fails on both, so a module's `visibility` list names exactly the declared modules that import it.
Tach likewise ignores an `[[interfaces]]` block whose `from` names an undeclared module, so the tests also require a declared one.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

TACH_CONFIG = Path(__file__).resolve().parents[1] / "tach.toml"
MINDROOM_SOURCE = TACH_CONFIG.parent / "src" / "mindroom"

INTENTIONALLY_BUNDLED_PACKAGES = frozenset(
    {
        "mindroom.agent_loop",
        "mindroom.call_transcripts",
        "mindroom.egress",
        "mindroom.egress_broker",
        "mindroom.event_journal",
        "mindroom.llm",
        "mindroom.providers.claude_code",
        "mindroom.providers.gemini",
        "mindroom.providers.openai",
        "mindroom.providers.pipeline",
        "mindroom.report_publishing",
        "mindroom.teams",
    },
)
UNDECLARED_INIT_ALLOWLIST = frozenset(
    {
        "mindroom",  # Declaring the root would catch all otherwise undeclared modules.
        "mindroom.tools",  # Built-in tool registration manifest, imported lazily by the tool registry.
    },
)


def _is_trivial_init(source: Path) -> bool:
    """Ignore only the module docstring, future imports and an empty export list."""
    tree = ast.parse(source.read_bytes(), filename=str(source))
    body = tree.body[1:] if ast.get_docstring(tree) is not None else tree.body
    for statement in body:
        match statement:
            case ast.ImportFrom(module="__future__", level=0):
                continue
            case ast.Assign(targets=[ast.Name(id="__all__")], value=ast.List(elts=[])):
                continue
            case _:
                return False
    return True


def test_all_mindroom_modules_are_governed_by_tach() -> None:
    """Require explicit coverage outside intentional bundles and initializer exceptions."""
    config = tomllib.loads(TACH_CONFIG.read_text(encoding="utf-8"))
    declared = {module["path"] for module in config["modules"]}
    modules = set()
    nontrivial_inits = set()
    for source in MINDROOM_SOURCE.rglob("*.py"):
        relative = source.relative_to(MINDROOM_SOURCE.parent)
        if source.name != "__init__.py":
            modules.add(".".join(relative.with_suffix("").parts))
        elif not _is_trivial_init(source):
            nontrivial_inits.add(".".join(relative.parent.parts))

    undeclared = modules - declared
    bundled = {
        package: {module for module in undeclared if module.startswith(f"{package}.")}
        for package in INTENTIONALLY_BUNDLED_PACKAGES
    }
    assert not (INTENTIONALLY_BUNDLED_PACKAGES - declared), "Every intentional bundle must itself be declared"
    empty_bundles = sorted(package for package, descendants in bundled.items() if not descendants)
    assert not empty_bundles, "Remove fully unbundled packages from INTENTIONALLY_BUNDLED_PACKAGES"
    uncovered = sorted(undeclared.difference(*bundled.values()))
    assert not uncovered, "Declare uncovered modules in tach.toml"

    stale_exceptions = sorted(UNDECLARED_INIT_ALLOWLIST - (nontrivial_inits - declared))
    assert not stale_exceptions, "Each initializer exception must still exist, be non-trivial, and be undeclared"
    uncovered_inits = sorted(nontrivial_inits - declared - UNDECLARED_INIT_ALLOWLIST)
    assert not uncovered_inits, "Declare uncovered non-trivial package initializers in tach.toml"


def test_tach_enforces_exact_and_acyclic_graph() -> None:
    """Keep exact edges and cycle rejection enabled in Tach itself."""
    config = tomllib.loads(TACH_CONFIG.read_text(encoding="utf-8"))
    assert config["exact"] is True
    assert config["forbid_circular_dependencies"] is True


def test_visibility_names_only_declared_importers() -> None:
    """Every module and interface `visibility` entry names a declared module that depends on the granting module."""
    config = tomllib.loads(TACH_CONFIG.read_text(encoding="utf-8"))
    depends_on = {module["path"]: set(module.get("depends_on", [])) for module in config["modules"]}
    grants = [({module["path"]}, module.get("visibility", [])) for module in config["modules"]]
    grants += [(set(interface["from"]), interface.get("visibility", [])) for interface in config["interfaces"]]
    stale = [
        f"{grantee} in the visibility of {', '.join(sorted(granting))}"
        for granting, visibility in grants
        for grantee in visibility
        if not depends_on.get(grantee, set()) & granting
    ]
    assert stale == [], "Each entry must name a declared module whose depends_on lists the granting module"


def test_interfaces_name_only_declared_modules() -> None:
    """Every interface `from` entry names a declared module, so no interface block is silently unenforced."""
    config = tomllib.loads(TACH_CONFIG.read_text(encoding="utf-8"))
    declared = {module["path"] for module in config["modules"]}
    undeclared = sorted({path for interface in config["interfaces"] for path in interface["from"]} - declared)
    assert undeclared == [], "Each interface must name a declared module; delete or redeclare these blocks"


def _strongly_connected_components(graph: dict[str, list[str]]) -> list[list[str]]:
    """Find the strongly connected components using Tarjan's algorithm."""
    indices: dict[str, int] = {}
    low_links: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(module: str) -> None:
        # Only dependencies still on the stack can link back to this component.
        indices[module] = low_links[module] = len(indices)
        stack.append(module)
        on_stack.add(module)
        for dependency in graph[module]:
            if dependency not in indices:
                visit(dependency)
                low_links[module] = min(low_links[module], low_links[dependency])
            elif dependency in on_stack:
                low_links[module] = min(low_links[module], indices[dependency])

        if low_links[module] != indices[module]:
            return
        component = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == module:
                break
        components.append(component)

    for module in graph:
        if module not in indices:
            visit(module)
    return components


def test_tach_module_graph_is_acyclic() -> None:
    """No strongly connected component may contain a dependency cycle, including self-dependencies."""
    config = tomllib.loads(TACH_CONFIG.read_text(encoding="utf-8"))
    graph = {module["path"]: module.get("depends_on", []) for module in config["modules"]}
    cycles = [
        sorted(component)
        for component in _strongly_connected_components(graph)
        if len(component) > 1 or component[0] in graph[component[0]]
    ]
    assert cycles == [], "Tach modules must form a directed acyclic graph"
