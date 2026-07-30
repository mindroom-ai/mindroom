"""Tests for sys.modules-gated model class checks (#1436).

The declared ``(module, class)`` tuples are load-bearing strings: a typo or an
agno relocation would make ``isinstance_of_loaded`` silently return False
forever instead of failing at import like a real import would. The resolution
test walks every declared tuple and asserts it names the class's actual
defining module, converting that silent failure mode back into a loud one.
"""

from __future__ import annotations

import decimal
from importlib import import_module

import pytest

from mindroom.claude_prompt_cache import _ANTHROPIC_CLAUDE_CLASS
from mindroom.model_instance_checks import isinstance_of_loaded
from mindroom.openai_tool_search import _OPENAI_RESPONSES_CLASS
from mindroom.thread_summary import _VERTEXAI_CLAUDE_CLASS

_ALL_DECLARED_CLASS_PATHS = sorted(
    {
        _ANTHROPIC_CLAUDE_CLASS,
        _OPENAI_RESPONSES_CLASS,
        _VERTEXAI_CLAUDE_CLASS,
    },
)


@pytest.mark.parametrize(("module_name", "class_name"), _ALL_DECLARED_CLASS_PATHS)
def test_declared_class_paths_name_the_defining_module(module_name: str, class_name: str) -> None:
    """Every declared tuple must resolve, and to the class's defining module.

    Resolving through a package init that merely re-exports the class would
    pass ``getattr`` but leave ``isinstance_of_loaded`` blind whenever only
    the concrete module is imported, so the defining module is asserted too.
    """
    loaded_class = getattr(import_module(module_name), class_name)
    assert loaded_class.__module__ == module_name


def test_isinstance_of_loaded_matches_loaded_class_and_subclass() -> None:
    """Loaded classes match exact instances and subclass instances."""

    class _SubDecimal(decimal.Decimal):
        pass

    assert isinstance_of_loaded(decimal.Decimal(1), ("decimal", "Decimal"))
    assert isinstance_of_loaded(_SubDecimal("1"), ("decimal", "Decimal"))
    assert not isinstance_of_loaded("not a decimal", ("decimal", "Decimal"))


def test_isinstance_of_loaded_treats_unloaded_module_as_no_match() -> None:
    """Unloaded or nonexistent modules never match and never import."""
    assert not isinstance_of_loaded(object(), ("module_that_is_never_imported", "Anything"))


def test_isinstance_of_loaded_checks_paths_in_order_until_first_match() -> None:
    """Any matching path in the tuple list is sufficient."""
    assert isinstance_of_loaded(
        decimal.Decimal(1),
        ("module_that_is_never_imported", "Anything"),
        ("decimal", "Decimal"),
    )
