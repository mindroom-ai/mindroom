"""Import-graph regression tests for slim entry points (#1436).

Two guards keep import-time regressions from creeping back:

1. A provider-SDK ban: importing the tool registry, config layer, sandbox
   runner, or the primary runtime must not import any provider SDK; those load
   on first model or tool construction. Slim entry points additionally must
   not import the nio matrix client or the mcp SDK, which the primary runtime
   genuinely needs at boot.
2. A third-party allowlist: each slim entry point may only load the
   third-party packages it loads today. Any new package in the graph fails
   loudly — either defer the import (see the CLAUDE.md import rule) or extend
   the allowlist as a conscious, reviewed decision. The orchestrator is
   exempt: its dependency set is large and legitimately grows.

Each probe runs in a subprocess so the assertion sees exactly what the import
graph pulls in.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

_PROVIDER_SDK_ROOTS = (
    "anthropic",
    "boto3",
    "cerebras",
    "google.genai",
    "groq",
    "ollama",
    "openai",
)
_SLIM_ONLY_ROOTS = ("mcp", "nio")

# Small, stable third-party footprints shared by the config layer and the
# registry chain: pydantic and yaml for config, structlog/rich for logging,
# cryptography for credentials, dotenv for runtime env files.
_CONFIG_LAYER_ROOTS = frozenset(
    {
        "annotated_types",
        "attr",
        "colorama",
        "cryptography",
        "cython_runtime",
        "dotenv",
        "greenlet",
        "pydantic",
        "pydantic_core",
        "pygments",
        "rich",
        "structlog",
        "typing_extensions",
        "typing_inspection",
        "yaml",
    },
)
# The registry additionally carries httpx (sandbox proxy transport) and click
# (tool CLI plumbing).
_REGISTRY_ROOTS = _CONFIG_LAYER_ROOTS | frozenset({"brotli", "click", "httpx", "idna", "zstandard"})
# The full tool-module sweep adds the agno toolkit runtime (redis-backed run
# cancellation, opentelemetry via redis) and the httpx dependency tree.
_TOOLS_ROOTS = _REGISTRY_ROOTS | frozenset(
    {
        "agno",
        "anyio",
        "attrs",
        "certifi",
        "docstring_parser",
        "h11",
        "h2",
        "hpack",
        "httpcore",
        "hyperframe",
        "importlib_metadata",
        "opentelemetry",
        "outcome",
        "packaging",
        "psutil",
        "redis",
        "sniffio",
        "sortedcontainers",
        "trio",
        "xxhash",
        "zipp",
    },
)
_ALLOWED_THIRD_PARTY_ROOTS: dict[str, frozenset[str]] = {
    "mindroom.config.main": _CONFIG_LAYER_ROOTS,
    "mindroom.model_loading": _CONFIG_LAYER_ROOTS | frozenset({"agno"}),
    "mindroom.tool_system.declarations": frozenset({"dotenv"}),
    "mindroom.tool_system.metadata": _REGISTRY_ROOTS,
    "mindroom.tool_system.catalog": _REGISTRY_ROOTS,
    "mindroom.tool_system.registration": _CONFIG_LAYER_ROOTS,
    "mindroom.tools": _TOOLS_ROOTS,
    # The sandbox runner is the API server for worker tool execution: fastapi
    # and its dependency tree on top of the full registry footprint.
    "mindroom.api.sandbox_runner": _TOOLS_ROOTS
    | frozenset(
        {
            "annotated_doc",
            "authlib",
            "bcrypt",
            "chardet",
            "charset_normalizer",
            "email_validator",
            "fastapi",
            "joserfc",
            "orjson",
            "python_multipart",
            "requests",
            "socks",
            "starlette",
            "urllib3",
        },
    ),
}

_BAN_PROBE_TEMPLATE = """
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

# mypyc-compiled dependencies register hash-named helper modules (e.g.
# "4ef79d...__mypyc") that vary per build, so they are excluded from the
# footprint along with private roots and mindroom itself.
_ALLOWLIST_PROBE_TEMPLATE = """
import importlib, json, sys

stdlib = set(sys.stdlib_module_names)
baseline = {{name.split(".")[0] for name in sys.modules}}
importlib.import_module({module!r})
roots = sorted(
    root
    for root in {{name.split(".")[0] for name in sys.modules}} - stdlib - baseline
    if not root.startswith("_") and not root.endswith("__mypyc") and not root.startswith("mindroom")
)
print(json.dumps(roots))
"""


def _run_probe(probe: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return json.loads(result.stdout)


def _assert_probe_clean(module: str, roots: tuple[str, ...]) -> None:
    loaded = _run_probe(_BAN_PROBE_TEMPLATE.format(module=module, roots=roots))
    assert loaded == [], f"importing {module} pulled in banned modules: {loaded}"


@pytest.mark.parametrize(
    "module",
    [
        "mindroom.config.main",
        "mindroom.model_loading",
        "mindroom.tool_system.declarations",
        "mindroom.tool_system.metadata",
        "mindroom.tool_system.catalog",
        "mindroom.tool_system.registration",
        "mindroom.tools",
        "mindroom.api.sandbox_runner",
    ],
)
def test_slim_entry_points_do_not_import_provider_sdks(module: str) -> None:
    """Slim entry points must keep provider SDKs and the matrix client unimported."""
    _assert_probe_clean(module, _PROVIDER_SDK_ROOTS + _SLIM_ONLY_ROOTS)


def test_primary_runtime_does_not_import_provider_sdks() -> None:
    """The orchestrator import (mindroom run) loads no provider SDK; only configured ones load later."""
    _assert_probe_clean("mindroom.orchestrator", _PROVIDER_SDK_ROOTS)


def test_builtin_tool_manifest_does_not_import_runtime_catalog() -> None:
    """Built-in registration may write registry state without loading catalog behavior."""
    _assert_probe_clean(
        "mindroom.tools",
        ("mindroom.tool_system.catalog", "mindroom.tool_system.metadata"),
    )


@pytest.mark.parametrize("module", sorted(_ALLOWED_THIRD_PARTY_ROOTS))
def test_slim_entry_points_only_load_allowlisted_packages(module: str) -> None:
    """A new third-party package in a slim import graph must be a conscious decision.

    The check is subset-based (new packages fail, absent ones pass) so
    platform-conditional dependencies do not flake. If this fails, prefer
    deferring the import to first use over extending the allowlist.
    """
    loaded = frozenset(_run_probe(_ALLOWLIST_PROBE_TEMPLATE.format(module=module)))
    unexpected = sorted(loaded - _ALLOWED_THIRD_PARTY_ROOTS[module])
    assert not unexpected, (
        f"importing {module} now loads third-party packages not in its allowlist: {unexpected}. "
        "Defer the import to first use (see the CLAUDE.md function-level import rule), "
        "or extend the allowlist here if the dependency is genuinely needed at import time."
    )
