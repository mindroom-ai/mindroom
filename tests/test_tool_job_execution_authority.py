"""Execution policies stay isolated between managed runtime roots."""

from dataclasses import replace
from pathlib import Path

import pytest
from agno.tools.function import Function

from mindroom.tool_jobs.execution_authority import (
    authorized_tool_call,
    check_current_execution_authority,
    set_execution_authorizer,
)
from mindroom.tool_jobs.runtime import JobAccessError
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.conftest import test_runtime_paths
from tests.test_delegate_tools import _delegate_runtime_context
from tests.test_subagent_runtime import _config, _job


def test_execution_authorizers_are_scoped_to_runtime(tmp_path: Path) -> None:
    """Installing or removing a second runtime cannot bypass the first policy."""
    first = test_runtime_paths(tmp_path / "first")
    second = test_runtime_paths(tmp_path / "second")
    owner = _job().owner
    context = _delegate_runtime_context(_config(tmp_path), first, execution_identity=owner)
    function = Function(name="denied", entrypoint=lambda: None)

    def denied(*_args: object) -> None:
        message = "Revoked"
        raise JobAccessError(message)

    set_execution_authorizer(first, denied)
    set_execution_authorizer(second, lambda *_args: None)
    try:
        with tool_runtime_context(context), authorized_tool_call(owner, function):
            with pytest.raises(JobAccessError, match="Revoked"):
                check_current_execution_authority()
            set_execution_authorizer(second, None)
            with pytest.raises(JobAccessError, match="Revoked"):
                check_current_execution_authority()
        with tool_runtime_context(replace(context, runtime_paths=second)), authorized_tool_call(owner, function):
            check_current_execution_authority()
    finally:
        set_execution_authorizer(first, None)
        set_execution_authorizer(second, None)
