"""Import-graph regression tests for slim entry points (#1436).

Importing the tool registry, config layer, or sandbox runner must not import
any provider SDK (or the nio matrix client); those load on first model or tool
construction. Each probe runs in a subprocess so the assertion sees exactly
what the import graph pulls in.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

_BANNED_IMPORT_ROOTS = (
    "anthropic",
    "boto3",
    "cerebras",
    "google.genai",
    "groq",
    "mcp",
    "nio",
    "ollama",
    "openai",
)

_PROBE_TEMPLATE = """
import importlib, json, sys

importlib.import_module({module!r})
roots = {roots!r}
loaded = sorted(
    name
    for name in sys.modules
    if any(name == root or name.startswith(root + ".") for root in roots)
)
print(json.dumps(loaded))
"""


@pytest.mark.parametrize(
    "module",
    [
        "mindroom.config.main",
        "mindroom.model_loading",
        "mindroom.tool_system.metadata",
        "mindroom.tool_system.catalog",
        "mindroom.tools",
        "mindroom.api.sandbox_runner",
    ],
)
def test_slim_entry_points_do_not_import_provider_sdks(module: str) -> None:
    """Slim entry points must keep provider SDKs and the matrix client unimported."""
    probe = _PROBE_TEMPLATE.format(module=module, roots=_BANNED_IMPORT_ROOTS)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    loaded = json.loads(result.stdout)
    assert loaded == [], f"importing {module} pulled in banned modules: {loaded}"
