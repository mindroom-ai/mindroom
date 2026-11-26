"""Tests for the workflow SDK that runs inside the script process.

Level: pure (the SDK against `FakeWorkflowGateway`).
"""

from __future__ import annotations

import ast
import hashlib
import json
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
        sdk.run_workflow(str(path))

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
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
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
def test_agent_sends_its_turn_and_returns_the_answer(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """The call's arguments are the turn itself: the prompt and the options that were given, and no timeout."""
    gateway.respond = lambda _request: completed("hi")

    assert sdk.agent("Say hi", name="greeter", model="haiku", tools=["shell"], minimal=True, timeout=30) == "hi"
    assert sdk.agent("Say hi") == "hi"

    first, plain = gateway.nodes
    assert (first["key"], first["kind"], plain["args"]) == ("@0", "agent", {"prompt": "Say hi"})
    assert first["args"] == {
        "prompt": "Say hi",
        "name": "greeter",
        "model": "haiku",
        "tools": ["shell"],
        "minimal": True,
    }
    assert (first["meta"], plain["meta"]) == ({"cached": True, "name": "greeter", "timeout": 30}, {"cached": True})


@pytest.mark.usefixtures("in_main")
def test_agent_sends_an_empty_list_as_none_of_them(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """`tools=[]` asks for a copy without tools: only an option that was not given is left out of the call."""
    sdk.agent("Summarize", system="Only summarize.", tools=[], skills=[], knowledge=[], minimal=False)

    assert gateway.nodes[0]["args"] == {
        "prompt": "Summarize",
        "system": "Only summarize.",
        "tools": [],
        "skills": [],
        "knowledge": [],
    }


@pytest.mark.usefixtures("in_main")
def test_sdk_agent_raises_agent_error_with_kind(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """A turn that did not end with an answer raises with the stable kind the script can branch on."""
    gateway.respond = lambda _request: failed("refused", "Cannot delegate: Unknown model 'opus5'.")

    with pytest.raises(sdk.AgentError, match="Unknown model 'opus5'") as raised:
        sdk.agent("Say hi", model="opus5")

    assert (raised.value.kind, isinstance(raised.value, sdk.WorkflowError)) == ("refused", True)


@pytest.mark.usefixtures("in_main")
def test_agent_refuses_an_option_it_does_not_take_before_any_node(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
) -> None:
    """A misnamed option fails in the script, where the model can fix it."""
    with pytest.raises(sdk.WorkflowError) as misnamed:
        sdk.agent("Say hi", system_prompt="Be brief.")

    assert str(misnamed.value) == "agent() has no option 'system_prompt'. Did you mean system?"
    assert gateway.nodes == []


@pytest.mark.usefixtures("in_main")
def test_agent_with_a_schema_sends_it_and_returns_the_parsed_value(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
) -> None:
    """With `schema` the call names the schema and returns the value as it is, also null; without it, text."""
    answers = iter([completed({"ok": True}), completed(None), completed(None)])
    gateway.respond = lambda _request: next(answers)
    schema = {"type": ["object", "null"]}

    assert sdk.agent("Does it hold?", schema=schema) == {"ok": True}
    assert sdk.agent("Does it still?", schema=schema) is None
    assert sdk.agent("Say hi") == "None"
    assert gateway.nodes[0]["args"] == {"prompt": "Does it hold?", "schema": schema}
    assert "schema" not in gateway.nodes[2]["args"]


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


@pytest.mark.usefixtures("in_main")
def test_ask_user_sends_the_question_and_returns_the_answer(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """`ask_user` is one call of kind `ask`; a question that was closed raises a WorkflowError the script can catch."""
    answers = iter([completed("Yes"), failed("cancelled", "The run ended before the question was answered.")])
    gateway.respond = lambda _request: next(answers)

    assert sdk.ask_user("Deploy?", ["Yes", "No"]) == "Yes"
    with pytest.raises(sdk.WorkflowError) as closed:
        sdk.ask_user("When?")

    assert (type(closed.value), closed.value.kind) == (sdk.WorkflowError, "cancelled")
    first, second = gateway.nodes
    assert (first["kind"], first["args"]) == ("ask", {"question": "Deploy?", "options": ["Yes", "No"]})
    assert second["args"] == {"question": "When?", "options": None}


@pytest.mark.parametrize("options", ["Yes", [], ["Yes", ""], ["Yes", 2], ("Yes", "No")])
@pytest.mark.usefixtures("in_main")
def test_sdk_refuses_options_that_are_not_strings(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
    options: object,
) -> None:
    """Options are a list of non-empty strings: anything else fails in the script, before any call is made."""
    with pytest.raises(sdk.WorkflowError) as refused:
        sdk.ask_user("Deploy?", options)

    assert str(refused.value) == "options must be a list of strings, such as ['Yes', 'No']."
    assert gateway.nodes == []


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
        "Request is 1,403,112 bytes, limit 1 MiB. Write the inputs to a workspace file and pass the path."
    )


@pytest.mark.usefixtures("in_main")
def test_oversized_finish_report_names_report_and_actual_limit(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """An over-limit finish report reports the actual report limit rather than a 1 MiB prompt."""
    gateway.refuse = (413, {"bytes": 9_000_000, "limit": 8 * 1024 * 1024})
    with pytest.raises(sdk.WorkflowError) as raised:
        sdk._gateway().finish(result="x")
    assert "Report is 9,000,000 bytes, limit 8 MiB." in str(raised.value)
    assert "Prompt" not in str(raised.value)


@pytest.mark.usefixtures("in_main")
def test_server_fault_that_repeats_ends_the_call_and_a_try_again_never_does(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 that repeats is a fault the script gets as an error; a 503 says try again, however often it comes."""
    waits: list[int] = []

    def wait(_seconds: float) -> None:
        waits.append(gateway.refuse[0] if gateway.refuse else 0)
        if len(waits) == 40:
            gateway.refuse = (500, {"detail": "boom"})

    monkeypatch.setattr(sdk.time, "sleep", wait)
    gateway.refuse = (503, {"detail": "not ready"})

    with pytest.raises(sdk.WorkflowError, match=r"\(500\): boom"):
        sdk.tool("files", "read", path="a.txt")
    assert waits == [503] * 40 + [500] * 5


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


def test_sdk_imports_only_stdlib_and_transport() -> None:
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
    assert foreign == {"mindroom.script_runs.transport"}


@pytest.mark.usefixtures("in_main")
def test_result_over_the_inline_limit_is_fetched_in_full_and_keeps_its_type(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
) -> None:
    """A receipt that only references its result makes the SDK read all of it, never the preview."""
    full = ["x" * 1023] * (9 * 1024)
    gateway.results["n0"] = full
    gateway.respond = lambda _request: completed("x" * 64, result_ref="n0")
    assert sdk.tool("files", "read_all") == full


@pytest.mark.usefixtures("in_main")
def test_now_and_random_are_value_nodes_whose_saved_results_they_return(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
) -> None:
    """The gateway makes the value, so a resume replays it; the SDK only gives it its type."""
    values = {"now": "2026-11-20T08:15:00+00:00", "random": 0.25}
    gateway.respond = lambda request: completed(values[request["args"]["op"]])

    moment, number = sdk.now(), sdk.random()

    assert (moment.isoformat(), moment.utcoffset().total_seconds(), number) == ("2026-11-20T08:15:00+00:00", 0, 0.25)
    assert [(node["key"], node["kind"], node["args"]) for node in gateway.nodes] == [
        ("@0", "value", {"op": "now"}),
        ("@1", "value", {"op": "random"}),
    ]


def _by_prompt(answers: dict[str, dict[str, object]]) -> Callable[[dict[str, object]], dict[str, object]]:
    """Answer each agent call with the receipt its prompt names, and every other node as completed."""

    def respond(request: dict[str, object]) -> dict[str, object]:
        args = request["args"]
        assert isinstance(args, dict)
        return answers[str(args["prompt"])] if request["kind"] == "agent" else completed(None)

    return respond


def _item(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:12]


@pytest.mark.usefixtures("in_main")
def test_foreach_lanes_are_keyed_by_item_value(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """Each item runs in a lane named by its value, an equal item by its occurrence; the group names them in order."""
    gateway.respond = lambda request: completed(str(request["args"].get("prompt", "")).upper())  # type: ignore[union-attr]
    sdk.phase("Research")

    assert sdk.foreach(["b", "a", "b"], lambda item: sdk.agent(item)) == ["B", "A", "B"]

    a, b = _item("a"), _item("b")
    [group] = gateway.nodes_of_kind("group")
    assert (group["key"], group["args"]) == ("@0", {"op": "foreach"})
    assert group["meta"] == {"cached": True, "phase": "Research", "members": [f"/0:{b}", f"/0:{a}", f"/0:{b}#2"]}
    agents = {node["key"]: node["meta"] for node in gateway.nodes_of_kind("agent")}
    assert sorted(agents) == sorted([f"/0:{b}@0", f"/0:{a}@0", f"/0:{b}#2@0"])
    # A lane starts in the phase of the call that opened it, and the next call of the root lane follows the group.
    assert {meta["phase"] for meta in agents.values()} == {"Research"}
    sdk.agent("after")
    assert gateway.nodes[-1]["key"] == "@1"


@pytest.mark.usefixtures("in_main")
@pytest.mark.parametrize("fan_out", ["foreach", "parallel"])
def test_a_fan_out_whose_group_was_refused_opens_no_lane(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
    fan_out: str,
) -> None:
    """The key of the group holds a call that may have run: the refusal is raised, and no item's call is submitted."""
    refused = failed("indeterminate", "This call may already have run (attempt 1).")
    gateway.respond = lambda request: refused if request["kind"] == "group" else completed("ran")

    calls = {
        "foreach": lambda: sdk.foreach(["a", "b"], lambda item: sdk.tool("files", "write", path=item)),
        "parallel": lambda: sdk.parallel([lambda: sdk.tool("files", "write", path="a")]),
    }

    with pytest.raises(sdk.ToolError, match=r"This call may already have run \(attempt 1\)\.") as raised:
        calls[fan_out]()

    assert (raised.value.kind, [node["kind"] for node in gateway.nodes]) == ("indeterminate", ["group"])


@pytest.mark.usefixtures("in_main")
def test_fan_out_inside_a_lane_opens_lanes_below_it(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """A lane counts its own calls, and a fan-out inside it names its lanes under the lane's path."""
    gateway.respond = lambda _request: completed("ok")

    def both(item: str) -> list[object]:
        sdk.agent(f"first {item}")
        return sdk.parallel([lambda: sdk.agent(f"left {item}"), lambda: sdk.agent(f"right {item}")])

    assert sdk.foreach(["a"], both) == [["ok", "ok"]]

    keys = {str(node["args"].get("prompt", node["args"].get("op"))): node["key"] for node in gateway.nodes}  # type: ignore[union-attr]
    lane = f"/0:{_item('a')}"
    assert (keys["first a"], keys["parallel"]) == (f"{lane}@0", f"{lane}@1")
    assert (keys["left a"], keys["right a"]) == (f"{lane}/1:p0@0", f"{lane}/1:p1@0")


@pytest.mark.usefixtures("in_main")
def test_one_failure_lets_siblings_finish_then_raises_with_partial_results(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
) -> None:
    """Every call settles before the fan-out raises, with what succeeded and what failed by position."""
    gateway.respond = _by_prompt({"a": completed("A"), "b": failed("failed", "boom"), "c": completed(None)})

    with pytest.raises(sdk.PartialFailure) as caught:
        sdk.foreach(["a", "b", "c"], lambda item: sdk.agent(item))

    error = caught.value
    assert (error.ok, error.results, list(error.errors), error.items) == (
        ["A", "None"],
        ["A", None, "None"],
        [1],
        ["a", "b", "c"],
    )
    assert (error.kind, str(error)) == ("partial", "1 of 3 calls failed: [1] AgentError(failed): boom")
    assert isinstance(error.errors[1], sdk.AgentError)
    assert len(gateway.nodes_of_kind("agent")) == 3


@pytest.mark.usefixtures("in_main", "gateway")
def test_a_legitimate_none_result_is_not_a_failure(sdk: ModuleType) -> None:
    """Without an error the plain list returns, and a None in it is what the function returned."""
    assert sdk.foreach(["a", "b"], lambda item: sdk.tool("files", "touch", path=item)) == [None, None]


@pytest.mark.usefixtures("in_main")
def test_all_failed_raises_and_ok_is_empty(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """A fan-out in which nothing succeeded raises like any other, with nothing in `ok`."""
    gateway.respond = lambda request: failed("failed", "no") if request["kind"] == "tool" else completed(None)

    with pytest.raises(sdk.PartialFailure) as caught:
        sdk.parallel([lambda: sdk.tool("files", "read", path="a"), lambda: sdk.tool("files", "read", path="b")])

    error = caught.value
    assert (error.ok, error.results, sorted(error.errors), error.items) == ([], [None, None], [0, 1], None)
    assert str(error) == "2 of 2 calls failed: [0] ToolError(failed): no; [1] ToolError(failed): no"


@pytest.mark.usefixtures("in_main")
def test_a_script_bug_in_the_callback_is_an_error_at_its_position_with_a_node(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
) -> None:
    """An exception of the script's own code fails its item only, and the lane gets a node that shows it."""
    table = {"a": 1}

    with pytest.raises(sdk.PartialFailure) as caught:
        sdk.foreach(["a", "x"], lambda item: sdk.tool("files", "read", path=str(table[item])))

    assert isinstance(caught.value.errors[1], KeyError)
    assert str(caught.value) == "1 of 2 calls failed: [1] KeyError: 'x'"
    [node] = gateway.nodes_of_kind("error")
    assert (node["key"], node["args"]) == (f"/0:{_item('x')}@0", {"type": "KeyError", "message": "'x'"})


@pytest.mark.usefixtures("in_main")
def test_run_cancel_is_not_collected(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """An attempt that the run left behind exits from the fan-out itself: no item of it is a failure to catch."""

    def respond(request: dict[str, object]) -> dict[str, object]:
        if request["kind"] == "agent":
            gateway.refuse = (409, {"detail": "superseded"})
        return completed("ok")

    gateway.respond = respond

    with pytest.raises(SystemExit) as stopped:
        sdk.foreach(["a", "b", "c"], lambda item: sdk.agent(item), max_concurrent=1)

    assert stopped.value.code == 3
    assert len(gateway.nodes_of_kind("agent")) == 1


@pytest.mark.usefixtures("in_main")
def test_parallel_of_lambdas_over_a_loop_variable_is_refused(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """Functions that share one loop variable would all see its last value: the call says what to write instead."""
    prompts = ["a", "b"]

    with pytest.raises(sdk.WorkflowError, match="capture the same loop variable") as refused:
        sdk.parallel([lambda: sdk.agent(p) for p in prompts])  # noqa: B023 - the mistake under test.

    assert str(refused.value).endswith("Use foreach(prompts, lambda p: agent(p)).")
    assert gateway.nodes == []


@pytest.mark.usefixtures("in_main")
def test_parallel_of_distinct_functions_and_of_default_bound_lambdas_is_fine(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
) -> None:
    """A lambda that binds its value, different functions, and one function given twice each see what they should."""
    gateway.respond = lambda request: completed(str(request["args"].get("prompt", "")).upper())  # type: ignore[union-attr]
    prompts, tone = ["a", "b"], "x"

    def ask() -> str:
        return sdk.agent(tone)

    assert sdk.parallel([lambda p=p: sdk.agent(p) for p in prompts]) == ["A", "B"]
    assert sdk.parallel([lambda: sdk.agent(tone), lambda: sdk.agent(tone + "y")]) == ["X", "XY"]
    assert sdk.parallel([ask, ask]) == ["X", "X"]
    assert gateway.nodes_of_kind("group")[0]["meta"]["members"] == ["/0:p0", "/0:p1"]  # type: ignore[index]


@pytest.mark.usefixtures("in_main")
def test_parallel_given_results_says_to_pass_functions(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """`parallel([agent(a), agent(b)])` ran its calls one after the other already: the message shows the right form."""
    with pytest.raises(sdk.WorkflowError) as refused:
        sdk.parallel(["answer a", "answer b"])

    assert str(refused.value) == (
        "parallel() takes functions. Write parallel([lambda: agent(a), lambda: agent(b)]) or use foreach(items, fn)."
    )
    assert gateway.nodes == []


@pytest.mark.usefixtures("in_main")
@pytest.mark.parametrize("asked", [None, 5000])
def test_foreach_of_1000_items_opens_at_most_256_requests(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
    asked: int | None,
) -> None:
    """Items are fed to at most 256 lanes at a time, however many there are, and every result comes back in order.

    A `max_concurrent` above that asks for no more threads than the 256.
    """
    barrier = threading.Barrier(256, timeout=20)

    def respond(request: dict[str, object]) -> dict[str, object]:
        if request["kind"] != "tool":
            return completed(None)
        arguments = request["args"]["arguments"]  # type: ignore[index]
        if arguments["n"] < 256:
            # The first 256 calls are open together: the fan-out is parallel up to its limit.
            barrier.wait()
        return completed(arguments["n"])

    gateway.respond = respond

    options = {} if asked is None else {"max_concurrent": asked}
    assert sdk.foreach(range(1000), lambda n: sdk.tool("files", "stat", n=n), **options) == list(range(1000))

    assert gateway.peak_open == 256
    assert len(gateway.nodes_of_kind("group")[0]["meta"]["members"]) == 1000  # type: ignore[arg-type]


@pytest.mark.usefixtures("in_main")
def test_max_concurrent_of_a_fan_out_holds_its_lanes_to_that_many(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
) -> None:
    """`max_concurrent` of the call itself starts the next lane only when one has ended."""
    pair = threading.Barrier(2, timeout=20)

    def respond(request: dict[str, object]) -> dict[str, object]:
        if request["kind"] == "tool":
            # Two calls are open together, and never a third: twenty lanes without the limit would be.
            pair.wait()
        return completed(None)

    gateway.respond = respond

    assert sdk.foreach(range(20), lambda n: sdk.tool("files", "stat", n=n), max_concurrent=2) == [None] * 20
    assert gateway.peak_open == 2
    with pytest.raises(sdk.WorkflowError, match="max_concurrent must be a whole number of at least 1"):
        sdk.foreach([1], str, max_concurrent=0)


@pytest.mark.usefixtures("in_main")
def test_a_waiting_lane_lets_the_next_lane_start_at_max_concurrent_one(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
) -> None:
    """A lane whose call waits for a person gives its place to the next lane, and takes one again to go on."""
    submitted = threading.Event()

    def respond(request: dict[str, object]) -> dict[str, object]:
        if request["kind"] == "agent" and request["args"]["prompt"] == "b":  # type: ignore[index]
            submitted.set()
        return completed("ok")

    def hold(request: dict[str, object]) -> str | None:
        # The first item waits for a person until the second one has started.
        waits = request["kind"] == "agent" and request["args"]["prompt"] == "a"  # type: ignore[index]
        return "waiting" if waits and not submitted.is_set() else None

    gateway.respond, gateway.hold = respond, hold

    assert sdk.foreach(["a", "b"], lambda item: sdk.agent(item), max_concurrent=1) == ["ok", "ok"]


@pytest.mark.usefixtures("in_main")
def test_non_json_items_are_positional_and_marked(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """An item that is no JSON value is remembered by its position, and the group says that it is."""
    items = [object(), {"a": 1}, {1, 2}]

    assert sdk.foreach(items, lambda _item: None) == [None, None, None]

    [group] = gateway.nodes_of_kind("group")
    assert group["args"] == {"op": "foreach", "positional": True}
    assert group["meta"]["members"] == ["/0:p0", f"/0:{_item({'a': 1})}", "/0:p2"]  # type: ignore[index]


@pytest.mark.usefixtures("in_main")
def test_fan_out_of_nothing_returns_nothing_and_still_has_its_group(
    gateway: FakeWorkflowGateway,
    sdk: ModuleType,
) -> None:
    """An empty list is a fan-out too: its group is saved, so a resume sees items that were added since."""
    assert (sdk.foreach([], str), sdk.parallel([])) == ([], [])
    assert [node["meta"]["members"] for node in gateway.nodes_of_kind("group")] == [[], []]  # type: ignore[index]
