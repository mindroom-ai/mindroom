"""Provider Bash is only a projection of effective canonical shell bindings."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from agno.tools.function import Function, FunctionCall
from agno.tools.toolkit import Toolkit

from mindroom.agent_cli.bash import MinimalBashTools
from tests.test_agent_tool_calls import _catalog

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.tool_access import ToolKey


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

    source = Toolkit(name="shell", tools=[run_shell_command])
    catalog = await _catalog(tmp_path, [source])
    seen = []
    fc = FunctionCall(function=Function(name="bash"), call_id="real-provider-call")

    async def execute(key: ToolKey, canonical_arguments: dict[str, object], outer: FunctionCall) -> str:
        assert outer is fc
        binding = await catalog.bind(key)
        seen.append((binding, canonical_arguments))
        return "canonical-result"

    facade = MinimalBashTools(execute=execute)
    assert set(facade.get_async_functions()) == {"bash"}
    assert await facade.bash(**kwargs, fc=fc) == "canonical-result"
    assert seen[0][0].key.toolkit == "shell"
    assert seen[0][0].function.name == name
    assert seen[0][1] == arguments


@pytest.mark.asyncio
async def test_facade_closed_without_owner_and_filtered_source(tmp_path: Path) -> None:
    """Neither shell authorization nor an operation owner is invented by presentation."""
    catalog = await _catalog(tmp_path, [])
    facade = MinimalBashTools()
    with pytest.raises(RuntimeError, match="owner"):
        await facade.bash(command="echo no")

    async def execute(key: ToolKey, _args: dict[str, object], _fc: FunctionCall) -> None:
        await catalog.bind(key)
        pytest.fail("disabled source reached worker")

    facade = MinimalBashTools(execute=execute)
    with pytest.raises(ValueError, match=r"^No toolkit 'shell'$"):
        await facade.bash(command="echo no", fc=FunctionCall(function=Function(name="bash")))


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

    facade = MinimalBashTools(execute=execute)
    with pytest.raises(ValueError, match=r"^Bash requires a command$"):
        await facade.bash(**kwargs)


@pytest.mark.asyncio
async def test_agno_injects_exact_provider_call_without_exposing_fc_schema() -> None:
    """History checkpoints and stop flags operate on the actual outer FunctionCall."""
    calls = []

    async def execute(key: ToolKey, arguments: dict[str, object], fc: FunctionCall) -> str:
        calls.append((key, arguments, fc))
        return "executed"

    facade = MinimalBashTools(execute=execute)
    function = facade.get_async_functions()["bash"]
    function.process_entrypoint(strict=False)
    assert "fc" not in function.parameters["properties"]
    call = FunctionCall(function=function, call_id="actual-provider-id", arguments={"command": "echo hi"})
    await call.aexecute()
    assert call.result == "executed"
    assert calls[0][2] is call
