"""Keep every Tach `visibility` entry naming a declared module that imports the granting module.

Tach checks each import as made by the nearest declared module that contains the importing file, and it has no exact
mode for `visibility`, so an entry naming an undeclared module matches nothing.
A grant to a declared module whose `depends_on` lacks the granting module pre-approves an import that no code makes,
because `exact = true` keeps every `depends_on` list equal to the module's runtime imports.
This test fails on both, so a module's `visibility` list names exactly the declared modules that import it.
Tach likewise ignores an `[[interfaces]]` block whose `from` names an undeclared module, so a second test requires a declared one.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

TACH_CONFIG = Path(__file__).resolve().parents[1] / "tach.toml"


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
