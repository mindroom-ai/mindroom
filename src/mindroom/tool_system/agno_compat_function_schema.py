"""Preserve owner schema postprocessors through Agno Function preparation/copies."""

from __future__ import annotations

import inspect
from functools import partial
from types import MethodType
from typing import TYPE_CHECKING, cast

from agno.tools.function import Function

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Any

# Reason: Agno rebuilds Function schemas after per-run copies without a public
# postprocessor contract; bound processors also need rebinding to the copied Function.
# Upstream issue: No matching copy-preserved Function schema processor issue identified.
# Upstream PR: None identified for this extension point.
# Remove when: Public Function schema postprocessors survive shallow/deep/per-run
# copies and run after processing; preserve the owner's output-path schema policy.
# Coverage: tests/test_tool_output_files.py::test_model_copy_update_preserves_output_path_schema_postprocessor;
# tests/test_tool_output_files.py::test_schema_keeps_output_path_optional_after_strict_processing;
# tests/test_tool_schema_cache.py.


def _process_entrypoint(postprocess: Callable[[Function], None], self: Function, strict: bool = False) -> None:
    effective_strict = False if self.strict is False else strict
    Function.process_entrypoint(self, strict=effective_strict)
    postprocess(self)


def uses_schema_postprocessor(function: Function, postprocess: Callable[[Function], None]) -> bool:
    """Recognize the exact owner processor without treating arbitrary wrappers as safe."""
    processor = function.process_entrypoint
    return (
        function.entrypoint is not None
        and isinstance(processor, MethodType)
        and isinstance(processor.__func__, partial)
        and processor.__func__.func is _process_entrypoint
        and processor.__func__.args == (postprocess,)
    )


def _copy_function_model(self: Function, *, update: Mapping[str, object] | None, deep: bool) -> Function:
    model_copy_parameters = inspect.signature(Function.model_copy).parameters
    if "update" in model_copy_parameters:
        return cast("Any", Function.model_copy)(self, update=update, deep=deep)
    copied = Function.model_copy(self, deep=deep)
    if update:
        for field_name, value in update.items():
            object.__setattr__(copied, field_name, value)
    return copied


def _model_copy(
    postprocess: Callable[[Function], None],
    self: Function,
    *,
    update: Mapping[str, object] | None = None,
    deep: bool = False,
) -> Function:
    copied = _copy_function_model(self, update=update, deep=deep)
    install_schema_postprocessor(copied, postprocess)
    return copied


def install_schema_postprocessor(function: Function, postprocess: Callable[[Function], None]) -> None:
    """Bind the owner processor and rebind it after every Agno Function copy."""
    object.__setattr__(function, "process_entrypoint", MethodType(partial(_process_entrypoint, postprocess), function))
    object.__setattr__(function, "model_copy", MethodType(partial(_model_copy, postprocess), function))
