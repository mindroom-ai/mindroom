"""A tool's approval aliases let another function's approval rules judge its calls too."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.approval.response import plan_approval_calls
from mindroom.approval.tool_approval import ToolApprovalScriptError, evaluate_tool_approval, tool_may_require_approval
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.llm.runs import ToolExecution
from mindroom.tool_system.authoring import Toolkit, tool
from mindroom.tool_system.catalog import export_tools_metadata, get_tool_by_name
from mindroom.tool_system.declarations import ApprovalAlias, ToolCategory, ToolFileAccess, ToolMetadata
from mindroom.tool_system.plugins import candidate_tool_registry
from mindroom.tool_system.registry import ToolMetadataValidationError, builtin_tool_registry
from mindroom.tool_system.tool_function import POLICY_CONFIRMATION_APPROVAL_TYPE
from tests.conftest import bind_runtime_paths, publish_test_plugins, register_test_tool, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_SHELL = "run_shell_command"
# A review script that asks before a shell command that runs rm.
_RM_REVIEW = "def check(tool_name, arguments, agent_name):\n    return 'rm' in arguments['args'].split()\n"


def _judge_lines(arguments: Mapping[str, object]) -> Sequence[Mapping[str, object]] | None:
    """Judge ``run_line`` as the shell command it runs; a command that is not text cannot tell."""
    command = arguments.get("command")
    return [{"args": command}] if isinstance(command, str) else None


_LINES = ApprovalAlias(judged_as=_SHELL, judge=_judge_lines)


def _config(
    tmp_path: Path,
    *rules: dict[str, object],
    plugins: list[str] | None = None,
) -> tuple[Config, RuntimePaths]:
    """Return a config with approval ``rules`` and ``plugins``, where ``rm_review.py`` is the rm review script."""
    runtime_paths = test_runtime_paths(tmp_path)
    (tmp_path / "rm_review.py").write_text(_RM_REVIEW, encoding="utf-8")
    config = Config(
        agents={"code": AgentConfig(display_name="Code", role="Write code")},
        tool_approval={"rules": list(rules)},
        plugins=plugins or [],
    )
    return bind_runtime_paths(config, runtime_paths), runtime_paths


def _shell_review(**rule: object) -> dict[str, object]:
    return {"match": _SHELL, "script": "rm_review.py", **rule}


def _lines_tool(name: str = "lines", aliases: Mapping[str, ApprovalAlias] | None = None) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        display_name="Lines",
        description="Runs command lines",
        category=ToolCategory.DEVELOPMENT,
        file_access=ToolFileAccess.UNCONFINED,
        approval_aliases={"run_line": _LINES} if aliases is None else aliases,
    )


async def _asks(
    config: Config,
    runtime_paths: RuntimePaths,
    arguments: dict[str, object],
    alias: ApprovalAlias = _LINES,
) -> tuple[bool, float]:
    return await evaluate_tool_approval(config, runtime_paths, "run_line", arguments, "code", alias)


@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "asks"), [("rm -rf build", True), ("ls -la", False)])
async def test_the_judged_functions_rules_judge_the_calls_an_alias_names(
    tmp_path: Path,
    command: str,
    asks: bool,
) -> None:
    """A shell review asks before the alias's rm line, with the review rule's card timeout, and lets ls through."""
    config, runtime_paths = _config(tmp_path, _shell_review(timeout_days=1))

    requires_approval, timeout_seconds = await _asks(config, runtime_paths, {"command": command})

    assert requires_approval is asks
    if asks:
        assert timeout_seconds == 24 * 60 * 60


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shell_rule", "asks"),
    [
        pytest.param(_shell_review(timeout_days=1), True, id="script"),
        pytest.param({"match": _SHELL, "action": "require_approval", "timeout_days": 1}, True, id="require"),
        pytest.param({"match": _SHELL, "action": "auto_approve", "timeout_days": 3}, False, id="never-asks"),
    ],
)
async def test_arguments_that_cannot_tell_ask_whenever_the_judged_rules_may(
    tmp_path: Path,
    shell_rule: dict[str, object],
    asks: bool,
) -> None:
    """A call whose arguments name no command fails closed: it asks unless no shell command ever would.

    A card it asks for stays open as long as the shell's rule says; otherwise its own rules say.
    """
    config, runtime_paths = _config(tmp_path, shell_rule)

    requires_approval, timeout_seconds = await _asks(config, runtime_paths, {"command": ["rm", "-rf", "/"]})

    assert requires_approval is asks
    assert timeout_seconds == (1 if asks else config.tool_approval.timeout_days) * 24 * 60 * 60


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "asks"),
    [("ls; rm -rf build", True), ("rm -rf build; ls", True), ("ls; pwd", False)],
)
async def test_a_call_that_amounts_to_several_judged_calls_asks_when_any_of_them_would(
    tmp_path: Path,
    command: str,
    asks: bool,
) -> None:
    """A line of several shell commands asks when the shell's rules would ask about any one of them."""
    config, runtime_paths = _config(tmp_path, _shell_review())
    alias = ApprovalAlias(
        judged_as=_SHELL,
        judge=lambda arguments: [{"args": part.strip()} for part in str(arguments["command"]).split(";")],
    )

    assert (await _asks(config, runtime_paths, {"command": command}, alias))[0] is asks


@pytest.mark.asyncio
async def test_a_call_that_amounts_to_no_judged_call_follows_its_own_rules(tmp_path: Path) -> None:
    """Unlike arguments that cannot tell, a call the judge maps to no shell command asks nothing of the shell's rules."""
    config, runtime_paths = _config(tmp_path, {"match": _SHELL, "action": "require_approval"})
    alias = ApprovalAlias(judged_as=_SHELL, judge=lambda _arguments: [])

    assert (await _asks(config, runtime_paths, {"command": "rm -rf build"}, alias))[0] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("own_rule", "command"),
    [
        pytest.param({"match": "run_line", "action": "require_approval"}, "ls -la", id="own-rule-asks"),
        pytest.param({"match": "run_line", "action": "auto_approve"}, "rm -rf build", id="own-rule-cannot-lift"),
    ],
)
async def test_a_tools_own_rules_add_to_the_judged_rules(
    tmp_path: Path,
    own_rule: dict[str, object],
    command: str,
) -> None:
    """The function's own rule still asks, and auto-approving it does not let it run what the shell rule asks about."""
    config, runtime_paths = _config(tmp_path, own_rule, _shell_review())

    assert (await _asks(config, runtime_paths, {"command": command}))[0] is True


@pytest.mark.parametrize(
    ("shell_rule", "gated"),
    [
        pytest.param(_shell_review(), True, id="script"),
        pytest.param({"match": _SHELL, "action": "auto_approve"}, False, id="never-asks"),
    ],
)
def test_an_aliased_function_may_need_approval_whenever_the_judged_function_may(
    tmp_path: Path,
    shell_rule: dict[str, object],
    gated: bool,
) -> None:
    """The gate a run puts on the function follows the shell's rules, which its own auto-approval does not lift."""
    config, _ = _config(tmp_path, {"match": "run_line", "action": "auto_approve"}, shell_rule)

    assert tool_may_require_approval(config, "run_line", _LINES) is gated
    assert tool_may_require_approval(config, "run_line") is False


def _paused_line(command: str, *, call_id: str = "call-1", tool_name: str = "run_line") -> ToolExecution:
    return ToolExecution(
        tool_call_id=call_id,
        tool_name=tool_name,
        tool_args={"command": command},
        requires_confirmation=True,
        approval_type=POLICY_CONFIRMATION_APPROVAL_TYPE,
    )


async def _planned_asks(
    config: Config,
    runtime_paths: RuntimePaths,
    toolkit: str,
    *paused: ToolExecution,
) -> list[bool]:
    """Return whether the policy asks a person about each paused call of the agent, offered from ``toolkit``."""
    plan = await plan_approval_calls(
        tuple((tool, "code") for tool in paused),
        config=config,
        runtime_paths=runtime_paths,
        requester_id="@user:localhost",
        toolkit_owners={("code", tool.tool_name): toolkit for tool in paused},
        room_id="!room:localhost",
        thread_id="$thread",
    )
    return [call.human_approval_required for call in plan.calls]


@pytest.mark.asyncio
async def test_a_paused_call_is_judged_by_the_alias_its_toolkit_declares(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the function the toolkit declares the alias for has its calls judged by the shell's rules."""
    register_test_tool(monkeypatch, _lines_tool())
    register_test_tool(monkeypatch, _lines_tool("other_lines", aliases={}))
    config, runtime_paths = _config(tmp_path, _shell_review())

    rm, ls = _paused_line("rm -rf build"), _paused_line("ls", call_id="call-2")
    show_rm = _paused_line("rm -rf build", call_id="call-3", tool_name="show_line")
    assert await _planned_asks(config, runtime_paths, "lines", rm, ls, show_rm) == [True, False, False]
    assert await _planned_asks(config, runtime_paths, "other_lines", rm) == [False]


@pytest.mark.asyncio
async def test_a_preset_naming_the_toolkit_brings_its_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A call names the preset its agent authored; the alias of the tool the preset brings in still judges it."""
    assert "coding" in Config.expand_tool_names(["openclaw_compat"])
    register_test_tool(monkeypatch, _lines_tool("coding"))
    config, runtime_paths = _config(tmp_path, _shell_review())

    assert await _planned_asks(config, runtime_paths, "openclaw_compat", _paused_line("rm -rf build")) == [True]


@pytest.mark.asyncio
async def test_the_judged_function_is_judged_by_its_own_rules_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alias never expands again: ``run_step``'s own alias to the shell does not judge the calls judged as it."""
    register_test_tool(
        monkeypatch,
        _lines_tool(aliases={"run_line": ApprovalAlias(judged_as="run_step", judge=_judge_lines)}),
    )
    register_test_tool(monkeypatch, _lines_tool("steps", aliases={"run_step": _LINES}))
    config, runtime_paths = _config(tmp_path, {"match": _SHELL, "action": "require_approval"})

    assert await _planned_asks(config, runtime_paths, "lines", _paused_line("rm -rf build")) == [False]
    assert await _planned_asks(config, runtime_paths, "steps", _paused_line("ls", tool_name="run_step")) == [True]


_PLUGIN_TOOLS = """\
from mindroom.plugin_api import ApprovalAlias, Toolkit, ToolCategory, ToolFileAccess, register_tool_with_metadata, tool


class LineTools(Toolkit):
    name = "lines"

    @tool
    def run_line(self, command: str) -> str:
        return command


def judge(arguments):
    return [{{"args": arguments["command"]}}]


@register_tool_with_metadata(
    name="lines",
    display_name="Lines",
    description="Runs command lines",
    category=ToolCategory.DEVELOPMENT,
    file_access=ToolFileAccess.UNCONFINED,
    approval_aliases={aliases},
)
def lines_tools():
    return LineTools
"""


@pytest.mark.asyncio
async def test_validating_a_plugin_edit_leaves_the_live_alias_judging(tmp_path: Path) -> None:
    """Validation runs the edited plugin's module again, yet the published alias keeps judging live calls."""
    plugin_root = tmp_path / "plugins" / "lines"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "lines", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    tools_module = plugin_root / "tools.py"
    tools_module.write_text(
        _PLUGIN_TOOLS.format(aliases='{"run_line": ApprovalAlias("run_shell_command", judge)}'),
        encoding="utf-8",
    )
    config, runtime_paths = _config(tmp_path, _shell_review(), plugins=["./plugins/lines"])
    publish_test_plugins(config, runtime_paths)
    tools_module.write_text(_PLUGIN_TOOLS.format(aliases="{}"), encoding="utf-8")

    candidate, skipped = candidate_tool_registry(config, runtime_paths)

    assert skipped == ()
    edited = candidate.get("lines")
    assert edited is not None
    assert dict(edited.approval_aliases) == {}
    assert await _planned_asks(config, runtime_paths, "lines", _paused_line("rm -rf build")) == [True]


class _LineTools(Toolkit):
    name = "lines"

    @tool
    def run_line(self, command: str) -> str:
        """Run one command line."""
        return command


@pytest.mark.parametrize(
    ("alias_key", "declared"),
    [
        pytest.param("run_line", (), id="offered"),
        # An option of the toolkit, such as one that turns a function off, may leave a declared function out.
        pytest.param("run_lines", ("run_line", "run_lines"), id="declared"),
    ],
)
def test_a_toolkit_builds_when_each_alias_names_one_of_its_functions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias_key: str,
    declared: tuple[str, ...],
) -> None:
    """An alias for a function the toolkit offers, or declares, builds the toolkit."""
    metadata = replace(_lines_tool(aliases={alias_key: _LINES}), function_names=declared, factory=lambda: _LineTools)
    register_test_tool(monkeypatch, metadata)

    toolkit = get_tool_by_name("lines", test_runtime_paths(tmp_path), tools=builtin_tool_registry(), worker_target=None)

    assert isinstance(toolkit, _LineTools)


def test_a_toolkit_whose_alias_names_none_of_its_functions_does_not_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misspelled alias refuses the toolkit, naming it and the alias, instead of leaving its function unjudged."""
    register_test_tool(
        monkeypatch,
        replace(_lines_tool(aliases={"run_lien": _LINES}), factory=lambda: _LineTools),
    )

    with pytest.raises(ToolMetadataValidationError, match=r"Tool 'lines' .*approval alias.*run_lien"):
        get_tool_by_name("lines", test_runtime_paths(tmp_path), tools=builtin_tool_registry(), worker_target=None)


class _SessionTools:
    """A judge and a factory bound to state no copy can take, such as a lock guarding a client session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def judge(self, arguments: Mapping[str, object]) -> Sequence[Mapping[str, object]] | None:
        with self._lock:
            return _judge_lines(arguments)

    def toolkit(self) -> type:
        return object


def test_the_tools_catalog_lists_a_tool_whose_alias_and_factory_hold_uncopyable_state() -> None:
    """The dashboard's tool catalog lists a toolkit with an alias as JSON, though its judge and factory hold a lock."""
    session = _SessionTools()
    metadata = replace(
        _lines_tool(aliases={"run_line": ApprovalAlias(judged_as=_SHELL, judge=session.judge)}),
        factory=session.toolkit,
    )

    (tool,) = export_tools_metadata({"lines": metadata})

    assert tool["name"] == "lines"
    json.dumps(tool)
    assert "approval_aliases" not in tool
    assert "factory" not in tool


def _timed_out_judge(_arguments: Mapping[str, object]) -> Sequence[Mapping[str, object]] | None:
    msg = "judging service timed out"
    raise TimeoutError(msg)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("judge", "error"),
    [
        pytest.param(_timed_out_judge, "failed with TimeoutError", id="raises"),
        pytest.param(lambda arguments: arguments["command"], "neither a list", id="text"),
        pytest.param(lambda arguments: {"args": arguments["command"]}, "neither a list", id="one-mapping"),
        pytest.param(lambda arguments: [arguments["command"]], "neither a list", id="list-of-text"),
        pytest.param(lambda _arguments: "", "neither a list", id="empty-text"),
    ],
)
async def test_a_judge_that_fails_is_an_approval_policy_error_like_a_failing_script(
    tmp_path: Path,
    judge: Callable[[Mapping[str, object]], Any],
    error: str,
) -> None:
    """A judge that raises or returns no list of calls fails the decision the way a failing approval script does.

    Its error names the function, never the judge's own message, and a malformed result never approves the call.
    """
    config, runtime_paths = _config(tmp_path, _shell_review())
    alias = ApprovalAlias(judged_as=_SHELL, judge=judge)

    with pytest.raises(ToolApprovalScriptError, match=error) as raised:
        await _asks(config, runtime_paths, {"command": "rm -rf build"}, alias)

    assert "run_line" in str(raised.value)
    assert "judging service" not in str(raised.value)
