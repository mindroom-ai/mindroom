"""Rewrite the tool goldens from the owned toolkit adapter.

Run `.venv/bin/python -m tests.agno_parity.regenerate_tool_goldens` from the repository root after a deliberate
change to a registered tool, and commit the rewritten `tests/fixtures/agno_parity/tools/` files with that change.
Every registered tool is built as `tests/tool_system/test_agno_toolkits.py` builds it, and every case already in
`arguments.json` runs as `tests/tool_system/test_tool_invocation.py` runs it, through the same support functions, so
the files hold exactly what those tests compare with; a new argument case needs only its `function` and
`raw_arguments` before a run fills in the rest.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.registry_state import BUILTIN_TOOL_METADATA
from tests.tool_system import agno_parity_support as support

# Registered tools no offline build can produce: their functions come from the Apify Actor builds and the Composio
# action catalog, which only those services return.
UNBUILDABLE = frozenset({"apify", "composio"})


def tool_schemas(root: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Build every registered tool with its default configuration and, when it has switches, with all of them on."""
    schemas: dict[str, object] = {}
    all_enabled: dict[str, object] = {}
    with support.isolated_process(root):
        config, runtime_paths = support.golden_runtime(root)
        for tool_name in sorted(BUILTIN_TOOL_METADATA):
            if tool_name in UNBUILDABLE:
                continue
            if (missing := support.missing_dependencies(tool_name)) is not None:
                msg = f"{tool_name} needs {missing}; the goldens cover every tool, so sync every extra first"
                raise SystemExit(msg)
            toolkit = support.build_toolkit(tool_name, config, runtime_paths)
            schemas[tool_name] = support.golden_entry(toolkit, adapt_toolkit(toolkit))
            if overrides := support.all_enabled_overrides(tool_name):
                toolkit = support.build_toolkit(tool_name, config, runtime_paths, overrides)
                all_enabled[tool_name] = {
                    "overrides": overrides,
                    **support.golden_entry(toolkit, adapt_toolkit(toolkit)),
                }
    return schemas, all_enabled


async def argument_outcomes() -> dict[str, dict[str, object]]:
    """Run every case of the committed argument golden again."""
    cases = json.loads(support.ARGUMENTS_PATH.read_text(encoding="utf-8"))
    functions = support.probe_functions()
    return {case_id: await support.argument_outcome(case, functions) for case_id, case in cases.items()}


def write_golden(path: Path, value: object, *, sort_keys: bool) -> None:
    """Write one fixture as pretty JSON, with its keys sorted or in the order they were built."""
    path.write_text(json.dumps(value, indent=2, sort_keys=sort_keys, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    """Rewrite the three tool fixtures."""
    with tempfile.TemporaryDirectory(prefix="tool-golden-") as root:
        schemas, all_enabled = tool_schemas(Path(root).resolve())
    # Schemas keep the key order providers receive, such as parameters in signature order.
    write_golden(support.SCHEMAS_PATH, schemas, sort_keys=False)
    write_golden(support.ALL_ENABLED_SCHEMAS_PATH, all_enabled, sort_keys=False)
    write_golden(support.ARGUMENTS_PATH, asyncio.run(argument_outcomes()), sort_keys=True)


if __name__ == "__main__":
    main()
