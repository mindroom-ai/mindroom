"""Helpers for adapting Agno toolkit function names."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agno.tools.function import Function
    from agno.tools.toolkit import Toolkit


def apply_toolkit_function_aliases(
    toolkit: Toolkit,
    aliases: Mapping[str, str],
    *,
    expose_attributes: bool = True,
) -> Toolkit:
    """Rename model-visible toolkit functions while preserving original methods."""
    toolkit.functions = _aliased_functions(toolkit.functions, aliases)
    toolkit.async_functions = _aliased_functions(toolkit.async_functions, aliases)
    if expose_attributes:
        for aliased_name in aliases.values():
            function = toolkit.functions.get(aliased_name)
            if function is not None and function.entrypoint is not None:
                setattr(toolkit, aliased_name, function.entrypoint)
    return toolkit


def _aliased_functions(functions: Mapping[str, Function], aliases: Mapping[str, str]) -> dict[str, Function]:
    aliased_functions: dict[str, Function] = {}
    for function_name, function in functions.items():
        aliased_name = aliases.get(function_name, function_name)
        function.name = aliased_name
        aliased_functions[aliased_name] = function
    return aliased_functions
