"""The model-call functions plugins import keep their names and keyword arguments (docs/plugins.md)."""

from __future__ import annotations

import inspect

from mindroom.agent_loop.helper import complete_prompt
from mindroom.providers.registry import build_model_provider
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context


def _parameters(function: object) -> list[tuple[str, inspect._ParameterKind]]:
    return [(name, parameter.kind) for name, parameter in inspect.signature(function).parameters.items()]


def test_plugins_build_a_provider_by_model_name_and_execution_identity() -> None:
    """A plugin passes the run's config, runtime paths, a configured model name, and its execution identity."""
    positional = inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert _parameters(build_model_provider) == [
        ("config", positional),
        ("runtime_paths", positional),
        ("model_name", positional),
        ("execution_identity", inspect.Parameter.KEYWORD_ONLY),
    ]
    assert _parameters(build_execution_identity_from_runtime_context) == [("context", positional)]


def test_plugins_send_one_prompt_with_optional_instructions_and_schema() -> None:
    """A plugin sends a prompt with keyword instructions and an optional structured-output schema."""
    names = [name for name, _ in _parameters(complete_prompt)]
    assert names[:2] == ["provider", "prompt"]
    assert {"instructions", "schema"} <= set(names[2:])
    assert all(
        inspect.signature(complete_prompt).parameters[name].kind is inspect.Parameter.KEYWORD_ONLY for name in names[2:]
    )
