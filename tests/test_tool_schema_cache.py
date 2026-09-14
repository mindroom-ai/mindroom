"""Tests for the shared prompt tool-schema cache."""

from __future__ import annotations

import gc
import weakref
from functools import wraps
from typing import TYPE_CHECKING, ClassVar

import pytest
from agno.tools.function import Function
from pydantic import BaseModel

import mindroom.tool_schema_cache as schema_cache
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_schema_cache import cached_processed_schema, clear_tool_schema_cache
from mindroom.tool_system import sandbox_proxy
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
@pytest.mark.parametrize("bound_method", [False, True])
def test_output_file_schema_matches_execution_preparation(
    tmp_path: Path,
    *,
    strict: bool,
    custom_parameters: bool,
    requires_user_input: bool,
    bound_method: bool,
) -> None:
    """Caching descriptions must preserve the execution schema and optional output path."""
    owner = _EchoTool("owner")
    function = Function(
        name="echo",
        entrypoint=owner.echo if bound_method else _echo,
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
    if requires_user_input:
        assert "text" not in snapshot.parameters["properties"] or custom_parameters
    assert snapshot.parameters["properties"][OUTPUT_PATH_ARGUMENT]["default"] is None
    assert OUTPUT_PATH_ARGUMENT not in snapshot.parameters["required"]
    assert function.entrypoint is original_entrypoint


@pytest.mark.parametrize("bound_method", [False, True])
def test_output_file_schema_reuses_metadata_without_sharing_execution_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bound_method: bool,
) -> None:
    """Equivalent schemas reuse work while each callable keeps its own owner and policy."""
    clear_tool_schema_cache()
    original_process = Function.process_entrypoint
    preparations = []

    def count_preparation(self: Function, strict: bool = False) -> None:
        preparations.append(self.name)
        original_process(self, strict=strict)

    monkeypatch.setattr(Function, "process_entrypoint", count_preparation)
    first_owner, second_owner = _EchoTool("first"), _EchoTool("second")
    first = wrap_function_for_output_files(
        Function(name="echo", entrypoint=first_owner.echo if bound_method else _echo),
        ToolOutputFilePolicy(workspace_root=tmp_path / "first"),
    )
    second = wrap_function_for_output_files(
        Function(name="echo", entrypoint=second_owner.echo if bound_method else _echo),
        ToolOutputFilePolicy(workspace_root=tmp_path / "second"),
    )
    first_schema = cached_processed_schema(first, strict=False)
    assert first_schema is not None
    first_schema.parameters["properties"]["text"]["type"] = "integer"
    assert preparations == ["echo"]

    second_schema = cached_processed_schema(second, strict=False)

    assert second_schema is not None
    assert second_schema.parameters["properties"]["text"]["type"] == "string"
    assert preparations == ["echo"]
    assert first.entrypoint is not None
    assert first.entrypoint("hello") == ("first:hello!" if bound_method else "hello")
    assert second.entrypoint is not None
    assert second.entrypoint("hello") == ("second:hello!" if bound_method else "hello")


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


def test_output_file_schema_does_not_retain_dynamic_closure(tmp_path: Path) -> None:
    """A schema cache must not own the state captured by a dynamic function."""

    def build_function() -> tuple[Function, weakref.ReferenceType[_EchoTool]]:
        owner = _EchoTool("private")

        def dynamic(text: str) -> str:
            """Return text using invocation-specific state."""
            return owner.echo(text)

        function = wrap_function_for_output_files(
            Function(name="dynamic", entrypoint=dynamic),
            ToolOutputFilePolicy(tmp_path),
        )
        return function, weakref.ref(owner)

    function, owner_ref = build_function()

    assert cached_processed_schema(function, strict=False) is not None
    del function
    gc.collect()
    assert owner_ref() is None


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

    assert cached_processed_schema(function, strict=False) is not None
    del function, dynamic, owner
    gc.collect()

    assert owner_ref() is None


def test_schema_cache_does_not_retain_user_input_annotation_types(tmp_path: Path) -> None:
    """User-input annotations can own state even when their prompt schema is empty."""

    def build_function() -> tuple[Function, weakref.ReferenceType[_EchoTool]]:
        owner = _EchoTool("private")

        class Answer(BaseModel):
            text: str
            retained_owner: ClassVar[_EchoTool] = owner

        def ask(answer: Answer) -> str:
            return answer.text

        ask.__annotations__["answer"] = Answer
        function = Function(name="ask", entrypoint=ask, requires_user_input=True, user_input_fields=["answer"])
        return wrap_function_for_output_files(function, ToolOutputFilePolicy(tmp_path)), weakref.ref(owner)

    function, owner_ref = build_function()
    snapshot = cached_processed_schema(function, strict=False)
    assert snapshot is not None
    assert "answer" not in snapshot.parameters["properties"]
    del function
    gc.collect()
    assert owner_ref() is None


def test_schema_cache_evicts_least_recently_used_entries_and_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache hits preserve hot schemas; eviction and explicit clear require fresh preparation."""
    clear_tool_schema_cache()
    monkeypatch.setattr(schema_cache, "_CACHE_SIZE", 2)
    original_process = Function.process_entrypoint
    preparations = []

    def count_preparation(self: Function, strict: bool = False) -> None:
        preparations.append(self.name)
        original_process(self, strict=strict)

    monkeypatch.setattr(Function, "process_entrypoint", count_preparation)

    def prepare(name: str) -> None:
        assert cached_processed_schema(Function(name=name, entrypoint=_echo), strict=False) is not None

    for name in ("one", "two", "one", "three", "one"):
        prepare(name)
    assert preparations == ["one", "two", "three"]
    prepare("two")
    assert preparations == ["one", "two", "three", "two"]
    clear_tool_schema_cache()
    prepare("one")
    assert preparations == ["one", "two", "three", "two", "one"]


def test_schema_cache_leaves_custom_callback_processors_uncached() -> None:
    """Only supported schema processors may participate in the shared cache."""
    function = Function(name="echo", entrypoint=_echo)
    object.__setattr__(function, "process_entrypoint", lambda **_kwargs: None)
    assert cached_processed_schema(function, strict=False) is None


@pytest.mark.parametrize("async_proxy", [False, True])
@pytest.mark.parametrize("output_order", ["none", "before", "after"])
def test_worker_proxy_schema_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    async_proxy: bool,
    output_order: str,
) -> None:
    """Transparent worker routing must preserve reuse with either output-wrapper order."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    clear_tool_schema_cache()
    original_process = Function.process_entrypoint
    preparations = []

    def count_preparation(self: Function, strict: bool = False) -> None:
        preparations.append(self.name)
        original_process(self, strict=strict)

    monkeypatch.setattr(Function, "process_entrypoint", count_preparation)
    wrap_proxy = sandbox_proxy._wrap_async_function if async_proxy else sandbox_proxy._wrap_sync_function

    def build(owner: _EchoTool) -> Function:
        function = Function(name="echo", entrypoint=owner.echo)
        policy = ToolOutputFilePolicy(tmp_path / owner.prefix)
        if output_order == "before":
            wrap_function_for_output_files(function, policy)
        function = wrap_proxy(function, "example", "echo", runtime_paths=runtime_paths, credentials_manager=None)
        if output_order == "after":
            wrap_function_for_output_files(function, policy)
        return function

    first_function = build(_EchoTool("first"))
    expected = first_function.model_copy(deep=True)
    expected.process_entrypoint(strict=False)
    preparations.clear()
    first = cached_processed_schema(first_function, strict=False)
    second = cached_processed_schema(build(_EchoTool("second")), strict=False)

    assert first is not None
    assert first.parameters == expected.parameters
    assert first.description == expected.description
    assert second == first
    assert preparations == ["echo"]


def test_decorators_cannot_inherit_an_owned_wrapper_schema_identity(tmp_path: Path) -> None:
    """functools.wraps must not make different outer decorators share one schema."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    inner = sandbox_proxy._wrap_sync_function(
        Function(name="echo", entrypoint=_EchoTool("owner").echo),
        "example",
        "echo",
        runtime_paths=runtime_paths,
        credentials_manager=None,
    )
    assert inner.entrypoint is not None

    def decorate(description: str) -> Function:
        entrypoint = inner.entrypoint
        assert entrypoint is not None

        @wraps(entrypoint)
        def outer(*args: object, **kwargs: object) -> object:
            return entrypoint(*args, **kwargs)

        outer.__doc__ = description
        return Function(name="echo", entrypoint=outer)

    first = cached_processed_schema(decorate("First adapter."), strict=False)
    second = cached_processed_schema(decorate("Second adapter."), strict=False)
    assert first is not None
    assert second is not None
    assert first.description == "First adapter."
    assert second.description == "Second adapter."
