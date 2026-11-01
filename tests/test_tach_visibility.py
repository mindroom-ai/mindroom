"""Keep every Tach `visibility` entry naming a declared module that imports the granting module.

Tach checks no import made by a module that `tach.toml` does not declare, and it has no exact mode for `visibility`.
With `exact = true`, a declared module's `depends_on` lists exactly the declared modules it imports at runtime.
So a `visibility` entry restricts something only when it names a declared module whose `depends_on` lists the
granting module, and this test fails on every other entry.
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
