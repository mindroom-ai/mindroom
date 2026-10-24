"""Provider Bash is only a projection of effective canonical shell bindings."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mindroom.agent_cli.bash import minimal_bash_toolkit
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from tests.test_agent_tool_calls import _catalog, _toolkit

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.tool_access import ToolKey


def _provider_call(call_id: str | None) -> ToolCallContext:
    return ToolCallContext(run_id="run", session_id="session", call_id=call_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "name", "arguments"),
    [
        ({"command": "echo hi"}, "run_shell_command", {"args": "echo hi", "tail": 100}),
        ({"command": "echo hi", "timeout": 5}, "run_shell_command", {"args": "echo hi", "tail": 100, "timeout": 5}),
    ],
)
async def test_facade_passes_effective_canonical_binding(
    tmp_path: Path,
    kwargs: dict[str, Any],
    name: str,
    arguments: dict[str, object],
) -> None:
    """Renaming presentation must not replace approval/hook names or arguments."""

    async def run_shell_command(args: str, timeout: int | None = None, tail: int = 100) -> str:  # noqa: ASYNC109
        del timeout, tail
        return args

    catalog = await _catalog(tmp_path, [_toolkit("shell", run_shell_command)])
    seen = []

    async def execute(key: ToolKey, canonical_arguments: dict[str, object], call_id: str) -> str:
        assert call_id == "real-provider-call"
        binding = await catalog.bind(key)
        seen.append((binding, canonical_arguments))
        return "canonical-result"

    [bash] = minimal_bash_toolkit(execute=execute).functions
    assert bash.name == "bash"
    outcome = await execute_tool_call(bash, kwargs, _provider_call("real-provider-call"), middleware=())
    assert outcome.content == "canonical-result"
    assert seen[0][0].key.toolkit == "shell"
    assert seen[0][0].function.name == name
    assert seen[0][1] == arguments


@pytest.mark.asyncio
async def test_facade_closed_without_owner_and_filtered_source(tmp_path: Path) -> None:
    """Neither shell authorization nor an operation owner is invented by presentation."""
    catalog = await _catalog(tmp_path, [])
    [closed] = minimal_bash_toolkit().functions
    outcome = await execute_tool_call(closed, {"command": "echo no"}, _provider_call("provider"), middleware=())
    assert outcome.is_error
    assert "owner" in outcome.content

    async def execute(key: ToolKey, _args: dict[str, object], _call_id: str) -> None:
        await catalog.bind(key)
        pytest.fail("disabled source reached worker")

    [bash] = minimal_bash_toolkit(execute=execute).functions
    outcome = await execute_tool_call(bash, {"command": "echo no"}, _provider_call("provider"), middleware=())
    assert outcome.is_error
    assert outcome.content == "No toolkit 'shell'"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"command": ""},
        {"command": "   "},
    ],
)
async def test_invalid_facade_shapes_fail_before_owner(kwargs: dict[str, Any]) -> None:
    async def execute(*_args: object) -> None:
        pytest.fail("invalid operation reached owner")

    [facade] = minimal_bash_toolkit(execute=execute).functions
    with pytest.raises(ValueError, match=r"^Bash requires a command$"):
        await facade.entrypoint(**kwargs)


@pytest.mark.asyncio
async def test_bash_reads_its_exact_provider_call_without_exposing_it_in_the_schema() -> None:
    """History checkpoints and stop flags operate on the provider call the loop is running, which the model never sees."""
    calls = []

    async def execute(key: ToolKey, arguments: dict[str, object], call_id: str) -> str:
        calls.append((key, arguments, call_id))
        return "executed"

    [bash] = minimal_bash_toolkit(execute=execute).functions
    assert set(bash.parameters["properties"]) == {"command", "timeout", "tail"}
    assert bash.injected_params == frozenset()
    outcome = await execute_tool_call(bash, {"command": "echo hi"}, _provider_call("actual-provider-id"), middleware=())
    assert outcome.content == "executed"
    assert calls[0][2] == "actual-provider-id"
    refused = await execute_tool_call(bash, {"command": "echo hi"}, _provider_call(None), middleware=())
    assert refused.is_error
    assert len(calls) == 1
