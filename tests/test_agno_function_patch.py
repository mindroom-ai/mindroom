"""Tests for MindRoom's temporary Agno Function patch."""

from importlib import import_module, reload

import agno.tools.function as agno_function
import pytest
from agno.tools.function import Function


def test_pydantic_version_lookup_is_cached_across_function_wraps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated Agno wraps should read Pydantic package metadata only once."""
    lookups = 0

    def version(package: str) -> str:
        nonlocal lookups
        assert package == "pydantic"
        lookups += 1
        return "2.13.3"

    monkeypatch.setattr(agno_function, "version", version)
    output_files = import_module("mindroom.tool_system.output_files")
    reload(output_files)
    patched_version = agno_function.version
    reload(output_files)

    def tool(value: str) -> str:
        return value

    for _ in range(100):
        Function._wrap_callable(tool)

    assert agno_function.version is patched_version
    assert lookups == 1
