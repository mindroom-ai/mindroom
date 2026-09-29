"""Execution policies stay isolated between managed runtime roots."""

from dataclasses import replace
from pathlib import Path

import pytest
from agno.tools.function import Function, FunctionCall

from mindroom.tool_jobs.execution_authority import authorized_tool_call, check_current_execution_authority
from mindroom.tool_jobs.instances import pin_background_tool_jobs, release_background_tool_jobs
from mindroom.tool_jobs.runtime import JobAccessError, register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.conftest import test_runtime_paths
from tests.delegation_helpers import _delegate_runtime_context
from tests.test_subagent_runtime import _config, _job
from tests.tool_job_helpers import tool_job_runtime


def test_unbound_tool_authority_does_not_access_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ordinary call needs no managed runtime lookup or storage filesystem access."""
    paths = test_runtime_paths(tmp_path)
    context = _delegate_runtime_context(_config(tmp_path), paths)

    def unavailable(_path: Path, *_args: object, **_kwargs: object) -> Path:
        msg = "Unbound calls must not resolve job storage"
        raise AssertionError(msg)

    with tool_runtime_context(context), monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", unavailable)
        check_current_execution_authority()


@pytest.mark.asyncio
async def test_execution_authorizers_are_scoped_to_runtime(tmp_path: Path) -> None:
    """Publishing or releasing a second runtime cannot bypass the first runtime's policy."""
    first = test_runtime_paths(tmp_path / "first")
    second = test_runtime_paths(tmp_path / "second")
    owner = _job().owner
    config = _config(tmp_path)
    context = _delegate_runtime_context(config, first, execution_identity=owner)
    function = Function(name="denied", entrypoint=lambda: None)

    def denied(*_args: object) -> None:
        message = "Revoked"
        raise JobAccessError(message)

    first_runtime = tool_job_runtime(first.storage_root, authorize_execution=denied)
    second_runtime = tool_job_runtime(second.storage_root)
    pin_background_tool_jobs(config, first)
    register_background_runtime(first, first_runtime)
    second_instance = pin_background_tool_jobs(config, second)
    register_background_runtime(second, second_runtime)
    try:
        with tool_runtime_context(context), authorized_tool_call(owner, FunctionCall(function=function)):
            with pytest.raises(JobAccessError, match="Revoked"):
                check_current_execution_authority()
            release_background_tool_jobs(second, second_instance)
            with pytest.raises(JobAccessError, match="Revoked"):
                check_current_execution_authority()
        with (
            tool_runtime_context(replace(context, runtime_paths=second)),
            authorized_tool_call(owner, FunctionCall(function=function)),
        ):
            check_current_execution_authority()
    finally:
        await first_runtime.shutdown()
        await second_runtime.shutdown()
