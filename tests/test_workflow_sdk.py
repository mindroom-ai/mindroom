"""Tests for the workflow SDK that runs inside the script process.

Level: pure (the SDK against `FakeWorkflowGateway`).
"""

from __future__ import annotations

import ast
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import mindroom.workflow as workflow_sdk
from tests.workflow_helpers import TOKEN, FakeWorkflowGateway, completed, failed, sdk_env

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import ModuleType

_MODULE_LEVEL = "Call agent() inside main(args). MindRoom calls main; remove your own call."


@pytest.fixture
def gateway(tmp_path: Path) -> Iterator[FakeWorkflowGateway]:
    """Serve a fake workflow gateway for one test."""
    with FakeWorkflowGateway(tmp_path / "capability") as fake:
        yield fake


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch, gateway: FakeWorkflowGateway) -> ModuleType:
    """Return the SDK module, pointed at the fake gateway."""
    sdk_env(monkeypatch, gateway)
    return workflow_sdk


@pytest.fixture
def in_main(sdk: ModuleType) -> Iterator[None]:
    """Run the test as if inside `main(args)`: the root lane exists."""
    sdk._local.lane = sdk._Lane("")
    yield
    sdk._local.lane = None


@pytest.fixture
def run_source(sdk: ModuleType, tmp_path: Path) -> Callable[[str], None]:
    """Run a script source the way the validated shim does."""

    def run(source: str) -> None:
        path = tmp_path / "wf.py"
        path.write_text(source, encoding="utf-8")
        sdk._run_workflow(str(path))

    return run


@pytest.mark.usefixtures("in_main")
def test_keys_count_calls_in_the_root_lane(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """Each call takes the next index of its lane, and a phase labels the calls after it."""
    sdk.tool("files", "read", path="a.txt")
    sdk.phase("Check")
    sdk.tool("files", "read", path="b.txt")
    assert [(node["key"], node["kind"], node["meta"].get("phase")) for node in gateway.nodes] == [
        ("@0", "tool", None),
        ("@1", "tool", "Check"),
    ]
    assert gateway.nodes[0]["args"] == {"toolkit": "files", "function": "read", "arguments": {"path": "a.txt"}}
    assert gateway.nodes[0]["meta"] == {"cached": True}


@pytest.mark.usefixtures("in_main")
def test_phase_as_a_context_manager_ends_with_its_block(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """`with phase(title):` groups only the calls inside the block."""
    with sdk.phase("Build"):
        sdk.tool("files", "read", path="a.txt", cached=False)
    sdk.tool("files", "read", path="b.txt")
    assert [node["meta"] for node in gateway.nodes] == [{"cached": False, "phase": "Build"}, {"cached": True}]


@pytest.mark.usefixtures("in_main")
def test_shell_reads_the_exit_status_from_the_receipt_and_sends_the_command_unchanged(
    gateway: FakeWorkflowGateway, sdk: ModuleType
) -> None:
    """The exit status comes from the receipt, so nothing in the command can fake it."""
    gateway.respond = lambda _request: completed({"output": "2 failed\n", "exit_code": 1})
    result = sdk.shell("pytest -x  # a trailing comment")
    assert (result.ok, result.exit_code, result.output) == (False, 1, "2 failed\n")
    assert gateway.nodes[0]["args"] == {
        "toolkit": "shell",
        "function": "run_shell_command",
        "arguments": {"args": "pytest -x  # a trailing comment", "timeout": 3600, "tail": 2000},
    }


@pytest.mark.usefixtures("in_main")
def test_failed_tool_raises_tool_error_with_kind(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """A call whose end is unknown raises with the stable kind the script can branch on."""
    gateway.respond = lambda _request: failed("indeterminate", "The call may have run.", state="indeterminate")
    with pytest.raises(sdk.ToolError, match=r"The call may have run\.") as raised:
        sdk.tool("files", "write", path="a.txt")
    assert raised.value.kind == "indeterminate"
    assert isinstance(raised.value, sdk.WorkflowError)


@pytest.mark.usefixtures("in_main")
def test_log_shows_a_progress_line_and_sends_the_capability(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """A log line reaches the run, authenticated by the attempt's token."""
    sdk.log("Checked 3 of 9")
    assert gateway.logs == ["Checked 3 of 9"]
    gateway.token_path.write_text("another-token", encoding="utf-8")
    sdk._gateway.cache_clear()
    assert TOKEN != "another-token"  # noqa: S105
    with pytest.raises(sdk.WorkflowError, match="Workflow run not found"):
        sdk.log("unauthorized")


def test_name_main_guard_is_harmless(gateway: FakeWorkflowGateway, run_source: Callable[[str], None]) -> None:
    """The script loads as `__workflow__`, so its `__main__` guard is false."""
    run_source('def main(args):\n    return "ok"\n\nif __name__ == "__main__":\n    main()\n')
    assert gateway.finished == {"result": "ok"}


def test_main_receives_the_runs_args(gateway: FakeWorkflowGateway, run_source: Callable[[str], None]) -> None:
    """`main` is called with the args the run was started with."""
    gateway.args = {"topic": "tides"}
    run_source('def main(args):\n    return args["topic"].upper()\n')
    assert gateway.finished == {"result": "TIDES"}


def test_module_level_call_fails_before_any_node(
    gateway: FakeWorkflowGateway,
    run_source: Callable[[str], None],
) -> None:
    """An SDK call outside `main` has no lane, so it fails at that call."""
    with pytest.raises(SystemExit) as raised:
        run_source('from mindroom.workflow import tool\ntool("t", "f")\ndef main(args): return 1\n')
    assert raised.value.code == 1
    assert _MODULE_LEVEL in gateway.finished["error"]
    assert gateway.nodes == []


def test_self_call_of_main_fails_before_any_node(
    gateway: FakeWorkflowGateway,
    run_source: Callable[[str], None],
) -> None:
    """The script's own `main({})` runs before the root lane exists."""
    with pytest.raises(SystemExit):
        run_source('from mindroom.workflow import tool\ndef main(args):\n    return tool("t", "f")\nmain({})\n')
    assert _MODULE_LEVEL in gateway.finished["error"]
    assert gateway.nodes == []


def test_missing_main_reports_what_to_do(gateway: FakeWorkflowGateway, run_source: Callable[[str], None]) -> None:
    """A script without `main` is told to define it."""
    with pytest.raises(SystemExit):
        run_source("report = 1\n")
    assert "Define def main(args): and return the report. MindRoom calls it." in gateway.finished["error"]


def test_traceback_keeps_the_users_line_numbers(
    gateway: FakeWorkflowGateway,
    run_source: Callable[[str], None],
) -> None:
    """The reported traceback names the script's own file and line."""
    with pytest.raises(SystemExit):
        run_source('def main(args):\n    values = {}\n    return values["missing"]\n')
    assert 'wf.py", line 3' in gateway.finished["error"]
    assert "KeyError: 'missing'" in gateway.finished["error"]
    assert len(gateway.finished["error"].encode()) <= 4096


@pytest.mark.usefixtures("in_main")
def test_call_from_a_raw_thread_says_to_use_foreach(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """A thread the SDK did not start has no lane, so its calls cannot be matched on resume."""
    errors: list[BaseException] = []

    def call() -> None:
        try:
            sdk.tool("files", "read", path="a.txt")
        except sdk.WorkflowError as exc:
            errors.append(exc)

    thread = threading.Thread(target=call)
    thread.start()
    thread.join()
    assert [str(error) for error in errors] == [
        "Call agent() from main or inside foreach()/parallel(). "
        "Raw threads are not tracked, so resume could not match their calls.",
    ]
    assert gateway.nodes == []


def test_unknown_option_suggests_the_right_name(sdk: ModuleType) -> None:
    """A misnamed option names the option the caller meant."""
    allowed = frozenset({"system", "agent", "prompt", "model", "timeout"})
    sdk._check_options("agent", {"model": "haiku"}, allowed)
    with pytest.raises(sdk.WorkflowError) as raised:
        sdk._check_options("agent", {"system_prompt": "x"}, allowed)
    assert str(raised.value) == "agent() has no option 'system_prompt'. Did you mean system?"
    with pytest.raises(sdk.WorkflowError, match=r"agent\(\) has no option 'timout'\. Did you mean timeout\?"):
        sdk._check_options("agent", {"timout": 5}, allowed)
    with pytest.raises(sdk.WorkflowError) as unknown:
        sdk._check_options("agent", {"zzz": 5}, allowed)
    assert str(unknown.value) == "agent() has no option 'zzz'."


@pytest.mark.usefixtures("in_main")
def test_oversized_request_says_to_use_a_file(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """A refused request reports its real size and what to do instead."""
    gateway.refuse = (413, {"bytes": 1403112, "limit": 1048576})
    with pytest.raises(sdk.WorkflowError) as raised:
        sdk.tool("files", "write", text="x")
    assert str(raised.value) == (
        "Prompt is 1,403,112 bytes, limit 1 MiB. Write the inputs to a workspace file and pass the path."
    )


@pytest.mark.usefixtures("in_main")
def test_poll_survives_a_gateway_restart(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """A gateway that drops connections looks like a transport error, and the SDK keeps polling."""

    def accept_then_restart(_request: dict[str, object]) -> dict[str, object]:
        gateway.drop_connections = 2
        return completed("after restart")

    gateway.respond = accept_then_restart
    gateway.polls_before_settled = 2
    assert sdk.tool("files", "read", path="a.txt") == "after restart"
    polls = [path for _method, path in gateway.requests if path.endswith("/nodes/n0?wait=25")]
    assert len(polls) == 4


@pytest.mark.usefixtures("in_main")
def test_superseded_attempt_exits_with_code_3(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """An attempt the run no longer names stops instead of acting."""
    gateway.refuse = (409, {"detail": "superseded"})
    with pytest.raises(SystemExit) as raised:
        sdk.tool("files", "read", path="a.txt")
    assert raised.value.code == 3


def test_sdk_imports_only_stdlib_and_launch_contract() -> None:
    """The SDK runs in the script's process, where only the standard library is promised."""
    tree = ast.parse(Path(workflow_sdk.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add(node.module or "")
    foreign = {name for name in imported if name.partition(".")[0] not in sys.stdlib_module_names}
    assert foreign == {"mindroom.script_runs.launch_contract"}


@pytest.mark.usefixtures("in_main")
def test_result_over_the_inline_limit_is_fetched_in_full_and_keeps_its_type(
    gateway: FakeWorkflowGateway, sdk: ModuleType
) -> None:
    """A receipt that only references its result makes the SDK read all of it, never the preview."""
    full = ["x" * 1023] * (9 * 1024)
    gateway.results["n0"] = full
    gateway.respond = lambda _request: completed("x" * 64, result_ref="n0")
    assert sdk.tool("files", "read_all") == full
