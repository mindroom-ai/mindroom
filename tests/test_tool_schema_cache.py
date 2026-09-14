"""Tests for the shared prompt tool-schema cache."""

from __future__ import annotations

import gc
import weakref
from functools import wraps
from typing import TYPE_CHECKING

import pytest
from agno.tools.function import Function

from mindroom.tool_schema_cache import (
    _cached_processed_function_schema,
    cached_processed_schema,
    clear_tool_schema_cache,
)
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT, ToolOutputFilePolicy, wrap_function_for_output_files

if TYPE_CHECKING:
    from pathlib import Path


class _EchoTool:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def echo(self, text: str, suffix: str = "!") -> str:
        """Echo text with the current owner's prefix.

        Args:
            text: Text to echo.
            suffix: Optional suffix.

        """
        return f"{self.prefix}:{text}{suffix}"


def _echo(text: str, agent: object = None) -> str:
    """Provide static metadata for a decorated dynamic entrypoint."""
    del agent
    return text


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("custom_parameters", [False, True])
@pytest.mark.parametrize("requires_user_input", [False, True])
def test_output_file_schema_matches_execution_preparation(
    tmp_path: Path,
    *,
    strict: bool,
    custom_parameters: bool,
    requires_user_input: bool,
) -> None:
    """Caching descriptions must preserve the execution schema and optional output path."""
    owner = _EchoTool("owner")
    function = Function(
        name="echo",
        entrypoint=owner.echo,
        requires_user_input=requires_user_input,
        user_input_fields=["text"] if requires_user_input else None,
    )
    if custom_parameters:
        function.parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    wrap_function_for_output_files(function, ToolOutputFilePolicy(workspace_root=tmp_path))
    original_entrypoint = function.entrypoint
    expected = function.model_copy(deep=True)
    expected.process_entrypoint(strict=strict)

    snapshot = cached_processed_schema(function, strict=strict)

    assert snapshot is not None
    assert snapshot.parameters == expected.parameters
    assert snapshot.description == expected.description
    assert snapshot.user_input_schema == (tuple(expected.user_input_schema) if expected.user_input_schema else None)
    assert snapshot.parameters["properties"][OUTPUT_PATH_ARGUMENT]["default"] is None
    assert OUTPUT_PATH_ARGUMENT not in snapshot.parameters["required"]
    assert function.entrypoint is original_entrypoint


def test_output_file_schema_reuses_metadata_without_sharing_execution_owners(tmp_path: Path) -> None:
    """Equivalent schemas reuse work while each callable keeps its own owner and policy."""
    clear_tool_schema_cache()
    first_owner, second_owner = _EchoTool("first"), _EchoTool("second")
    first = wrap_function_for_output_files(
        Function(name="echo", entrypoint=first_owner.echo),
        ToolOutputFilePolicy(workspace_root=tmp_path / "first"),
    )
    second = wrap_function_for_output_files(
        Function(name="echo", entrypoint=second_owner.echo),
        ToolOutputFilePolicy(workspace_root=tmp_path / "second"),
    )
    first_schema = cached_processed_schema(first, strict=False)
    assert first_schema is not None
    first_schema.parameters["properties"]["text"]["type"] = "integer"
    after_first = _cached_processed_function_schema.cache_info()

    second_schema = cached_processed_schema(second, strict=False)

    assert second_schema is not None
    assert second_schema.parameters["properties"]["text"]["type"] == "string"
    assert _cached_processed_function_schema.cache_info().misses == after_first.misses
    assert _cached_processed_function_schema.cache_info().hits == after_first.hits + 1
    assert first.entrypoint is not None
    assert first.entrypoint("hello") == "first:hello!"
    assert second.entrypoint is not None
    assert second.entrypoint("hello") == "second:hello!"


def test_output_file_schema_cache_does_not_retain_tool_owner_or_policy(tmp_path: Path) -> None:
    """Cached prompt metadata must not extend a requester's execution-state lifetime."""
    owner = _EchoTool("private")
    policy = ToolOutputFilePolicy(workspace_root=tmp_path)
    owner_ref, policy_ref = weakref.ref(owner), weakref.ref(policy)
    function = wrap_function_for_output_files(Function(name="echo", entrypoint=owner.echo), policy)

    assert cached_processed_schema(function, strict=False) is not None
    del function, policy, owner
    gc.collect()

    assert owner_ref() is None
    assert policy_ref() is None


def test_output_file_schema_keeps_dynamic_closure_on_uncached_path(tmp_path: Path) -> None:
    """Dynamic closure state must never enter the shared schema cache."""
    marker = object()

    def dynamic(text: str) -> str:
        """Return text using invocation-specific state."""
        assert marker is not None
        return text

    function = wrap_function_for_output_files(
        Function(name="dynamic", entrypoint=dynamic),
        ToolOutputFilePolicy(tmp_path),
    )

    assert cached_processed_schema(function, strict=False) is None


def test_cached_processed_schema_returns_private_copies() -> None:
    """Mutating a returned snapshot must not corrupt the shared LRU entry."""

    def sync_event(title: str, include_attendees: bool = False) -> str:
        """Sync one event."""
        return f"{title}:{include_attendees}"

    function = Function(name="sync_event", entrypoint=sync_event)

    first = cached_processed_schema(function, strict=False)
    assert first is not None
    first.parameters["properties"]["injected"] = {"type": "string"}
    first.parameters["required"].append("injected")

    second = cached_processed_schema(function, strict=False)
    assert second is not None
    assert "injected" not in second.parameters["properties"]
    assert second.parameters["required"] == ["title"]
    assert second.parameters is not first.parameters


@pytest.mark.parametrize("keyword_only", [False, True])
@pytest.mark.parametrize("decorated", [False, True])
def test_output_file_schema_keeps_default_captured_owners_out_of_cache(
    tmp_path: Path,
    *,
    keyword_only: bool,
    decorated: bool,
) -> None:
    """Factory functions can retain request state through defaults without a closure."""
    owner = _EchoTool("private")
    owner_ref = weakref.ref(owner)
    if keyword_only:

        def dynamic(text: str, *, agent: _EchoTool = owner) -> str:
            return agent.echo(text)

    else:

        def dynamic(text: str, agent: _EchoTool = owner) -> str:
            return agent.echo(text)

    if decorated:
        wraps(_echo)(dynamic)
        assert "<locals>" not in dynamic.__qualname__
    assert dynamic.__closure__ is None
    function = wrap_function_for_output_files(
        Function(name="dynamic", entrypoint=dynamic),
        ToolOutputFilePolicy(tmp_path),
    )

    assert cached_processed_schema(function, strict=False) is None
    del function, dynamic, owner
    gc.collect()

    assert owner_ref() is None
