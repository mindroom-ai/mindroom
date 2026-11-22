"""The agent CLI parses strict input without loading the runtime."""

# ruff: noqa: D103, ANN001
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from email.message import Message
from io import BytesIO, TextIOWrapper
from urllib.error import HTTPError

import pytest

from mindroom.agent_cli.client import AgentCliUnavailableError
from mindroom.agent_cli.main import main, parse_arguments, read_call_arguments
from mindroom.script_runs.launch_contract import digest_arguments
from mindroom.shell.output_capture import format_shell_completion, format_shell_timeout


def test_call_accepts_json_object() -> None:

    args = parse_arguments(["tools", "call", "calculator", "add", "--json", '{"a":1,"b":2}'])
    assert read_call_arguments(args) == {"a": 1, "b": 2}


def test_dotted_name_selects_toolkit_and_function() -> None:

    describe = parse_arguments(["tools", "describe", "matrix_message.matrix_message"])
    assert (describe.toolkit, describe.function) == ("matrix_message", "matrix_message")
    call = parse_arguments(["tools", "call", "todo.add_todo", "--json", "{}"])
    assert (call.toolkit, call.function, call.timeout) == ("todo", "add_todo", None)


@pytest.mark.parametrize(
    "argv",
    [
        ["tools", "call", "matrix_room", "matrix_room", '{"action":"threads"}'],
        ["tools", "call", "matrix_room.matrix_room", '{"action":"threads"}'],
        *(
            ["tools", "call", "matrix_room.matrix_room", f'{space}{{"action":"threads"}}']
            for space in (" ", "\t", "\n")
        ),
    ],
)
def test_trailing_json_object_supplies_call_arguments(argv: list[str]) -> None:

    args = parse_arguments(argv)
    assert (args.toolkit, args.function) == ("matrix_room", "matrix_room")
    assert read_call_arguments(args) == {"action": "threads"}


def test_call_arguments_are_given_once(capsys: pytest.CaptureFixture[str]) -> None:

    with pytest.raises(SystemExit):
        parse_arguments(["tools", "call", "a.b", '{"x":1}', "--json", '{"x":2}'])
    assert "give tool arguments once" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["tools", "describe", "scheduler"], ["tools", "call", "scheduler"]])
def test_missing_function_names_the_search(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:

    with pytest.raises(SystemExit):
        parse_arguments(argv)
    assert "find function names with: mindroom-agent tools search scheduler" in capsys.readouterr().err


@pytest.mark.parametrize(
    "payload",
    ["[]", '{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', "{", '"x"', '{"a":"' + "x" * 65536 + '"}'],
)
def test_call_rejects_non_strict_or_oversized_input(payload: str) -> None:

    args = parse_arguments(["tools", "call", "calculator", "add", "--json", payload])
    with pytest.raises(ValueError, match="JSON"):
        read_call_arguments(args)


def test_help_and_imports_need_no_runtime() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from mindroom.agent_cli.main import main; assert not any(x.startswith(('agno', 'pydantic', 'mindroom.api', 'mindroom.config', 'mindroom.credentials.manager')) for x in sys.modules); main(['--help'])",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "tools" in result.stdout


def test_help_says_what_workers_run_needs(capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI knows no config, so its help says the command works only for an agent that may name workers."""
    with pytest.raises(SystemExit):
        parse_arguments(["--help"])
    assert "needs the agent's named_workers setting" in " ".join(capsys.readouterr().out.split())


def test_main_reports_missing_authority(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.delenv("MINDROOM_AGENT_CLI_URL", raising=False)
    monkeypatch.delenv("MINDROOM_AGENT_CLI_TOKEN", raising=False)
    assert main(["tools", "list"]) == 4
    assert json.loads(capsys.readouterr().out)["error"] == "Agent CLI authority is unavailable"


def test_file_stdin_and_exclusive_inputs(tmp_path, monkeypatch) -> None:

    path = tmp_path / "arguments.json"
    path.write_text('{"value":3}')
    assert read_call_arguments(parse_arguments(["tools", "call", "a", "b", "--json-file", str(path)])) == {"value": 3}
    monkeypatch.setattr(sys, "stdin", TextIOWrapper(BytesIO(b'{"value":4}')))
    assert read_call_arguments(parse_arguments(["tools", "call", "a", "b", "--json-stdin"])) == {"value": 4}
    with pytest.raises(SystemExit):
        parse_arguments(["tools", "call", "a", "b", "--json", "{}", "--json-stdin"])
    with pytest.raises(SystemExit):
        parse_arguments(["tools", "unknown"])


def test_failed_poll_keeps_the_admitted_receipt(capsys, monkeypatch) -> None:

    class Client:
        def operation(self, payload) -> dict[str, object]:
            return {"call_id": payload["call_id"], "toolkit": "a", "function": "b", "status": "queued"}

        def receipt(self, _call_id) -> dict[str, object]:
            msg = "Agent CLI transport outcome is unknown"
            raise AgentCliUnavailableError(msg)

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    assert main(["tools", "call", "a.b", "--timeout", "1"]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "queued"


def test_unknown_transport_preserves_exact_call_id(capsys, monkeypatch) -> None:

    monkeypatch.delenv("MINDROOM_AGENT_CLI_URL", raising=False)
    call_id = "12345678-1234-4234-8234-123456789abc"
    assert main(["tools", "call", "a", "b", "--call-id", call_id]) == 4
    assert json.loads(capsys.readouterr().out)["call_id"] == call_id


def _in_background_script(monkeypatch, tmp_path) -> None:
    token_path = tmp_path / "capability"
    token_path.write_text("script-token", encoding="utf-8")
    monkeypatch.delenv("MINDROOM_AGENT_CLI_URL", raising=False)
    monkeypatch.delenv("MINDROOM_AGENT_CLI_TOKEN", raising=False)
    monkeypatch.setenv("MINDROOM_SCRIPT_GATEWAY_URL", "http://primary:8765/api/script-gateway")
    monkeypatch.setenv("MINDROOM_SCRIPT_RUN_ID", "run-1")
    monkeypatch.setenv("MINDROOM_SCRIPT_TOKEN_PATH", str(token_path))
    monkeypatch.setattr("mindroom.script_sdk.uuid.uuid4", lambda: type("ID", (), {"hex": "call-1"})())


def _script_gateway(monkeypatch, state: str, *, result=None, error=None) -> list[dict[str, object]]:
    """Answer every submission with one receipt in *state*, and return the submissions."""
    submitted: list[dict[str, object]] = []

    def urlopen(request, *, timeout) -> BytesIO:
        del timeout
        assert request.headers["Authorization"] == "Bearer script-token"
        payload = json.loads(request.data)
        submitted.append(payload)
        receipt = {
            "run_id": "run-1",
            "call_id": "call-1",
            "toolkit_name": payload["toolkit_name"],
            "function_name": payload["function_name"],
            "arguments_digest": digest_arguments(payload["arguments"]),
            "state": state,
            "created_at": "2026-11-04T00:00:00Z",
            "updated_at": "2026-11-04T00:00:01Z",
            "result": result,
            "error": error,
        }
        return BytesIO(json.dumps(receipt).encode())

    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)
    return submitted


def test_a_background_script_calls_tools_with_its_own_capability(capsys, monkeypatch, tmp_path) -> None:

    _in_background_script(monkeypatch, tmp_path)
    submitted = _script_gateway(monkeypatch, "completed", result={"messages": 3})
    # Arguments may share a name with the SDK's own parameters.
    arguments = {"query": "launch", "function_name": "kept"}

    assert main(["tools", "call", "slack.slack_search", json.dumps(arguments)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "toolkit": "slack",
        "function": "slack_search",
        "status": "completed",
        "outcome": {"messages": 3},
    }
    assert [(call["toolkit_name"], call["function_name"], call["arguments"]) for call in submitted] == [
        ("slack", "slack_search", arguments),
    ]


@pytest.mark.parametrize(
    ("state", "kind", "exit_code"),
    [("failed", "tool_failure", 1), ("indeterminate", "indeterminate", 4)],
)
def test_an_unfinished_background_script_call_prints_its_error(
    state,
    kind,
    exit_code,
    capsys,
    monkeypatch,
    tmp_path,
) -> None:

    _in_background_script(monkeypatch, tmp_path)
    _script_gateway(monkeypatch, state, error={"kind": kind, "message": "boom", "retryable": False})

    assert main(["tools", "call", "slack", "slack_search", "{}"]) == exit_code
    assert json.loads(capsys.readouterr().out) == {
        "call_id": "call-1",
        "toolkit": "slack",
        "function": "slack_search",
        "status": state,
        "outcome": {"error": "boom", "kind": kind},
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["tools", "list"],
        ["calls", "get", "12345678-1234-4234-8234-123456789abc"],
        ["context", "list"],
        ["tools", "call", "a.b", "--call-id", "12345678-1234-4234-8234-123456789abc"],
        ["tools", "call", "a.b", "--timeout", "0"],
    ],
)
def test_a_background_script_is_offered_only_tool_calls(argv, capsys, monkeypatch, tmp_path) -> None:

    _in_background_script(monkeypatch, tmp_path)
    assert main(argv) == 2
    assert "background script" in json.loads(capsys.readouterr().out)["error"]


def test_a_background_script_prints_a_result_at_the_gateway_limit(capsys, monkeypatch, tmp_path) -> None:

    _in_background_script(monkeypatch, tmp_path)
    _script_gateway(monkeypatch, "completed", result="x" * 65_500)

    assert main(["tools", "call", "file.read_file", "{}"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "x" * 65_500


def test_a_refused_receipt_lookup_leaves_the_accepted_call_indeterminate(capsys, monkeypatch, tmp_path) -> None:

    _in_background_script(monkeypatch, tmp_path)
    pending = _script_gateway(monkeypatch, "pending")
    accept = urllib.request.urlopen

    def urlopen(request, *, timeout) -> BytesIO:
        if request.method == "POST":
            return accept(request, timeout=timeout)
        # The requester lost access after the call was accepted, so the gateway refuses to show its receipt.
        raise HTTPError(request.full_url, 404, "Not Found", Message(), BytesIO(b'{"detail":"unavailable"}'))

    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)
    monkeypatch.setattr("mindroom.script_sdk.time.sleep", lambda _seconds: None)

    assert main(["tools", "call", "matrix_message.matrix_message", "{}"]) == 4
    output = json.loads(capsys.readouterr().out)
    assert (output["call_id"], output["status"]) == ("call-1", "indeterminate")
    assert len(pending) == 1


def test_a_background_script_without_its_capability_exits_4(capsys, monkeypatch, tmp_path) -> None:

    _in_background_script(monkeypatch, tmp_path)
    (tmp_path / "capability").unlink()

    assert main(["tools", "call", "a.b", "{}"]) == 4
    assert json.loads(capsys.readouterr().out)["error"] == "The MindRoom script capability token is unavailable."


def test_a_background_script_call_the_gateway_refused_failed(capsys, monkeypatch, tmp_path) -> None:

    _in_background_script(monkeypatch, tmp_path)

    def urlopen(request, *, timeout) -> BytesIO:
        del timeout
        # A tool outside the run's grants is refused before the gateway records the call.
        raise HTTPError(request.full_url, 404, "Not Found", Message(), BytesIO(b'{"detail":"unavailable"}'))

    monkeypatch.setattr("mindroom.script_sdk.urllib.request.urlopen", urlopen)

    assert main(["tools", "call", "memory.add_memory", "{}"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def _worker_client(monkeypatch, *receipts: dict[str, object]) -> list[dict[str, object]]:
    """Answer one submitted call with *receipts* in turn, the last one for every later read, and return what was sent."""
    sent: list[dict[str, object]] = []
    remaining = list(receipts)

    class Client:
        def operation(self, payload) -> dict[str, object]:
            sent.append(payload)
            return self.receipt(payload["call_id"])

        def receipt(
            self,
            call_id,
            *,
            timeout: float = 30,
            wait: float | None = None,
            after_progress: int | None = None,
        ) -> dict[str, object]:
            del timeout, wait, after_progress
            assert call_id == sent[0]["call_id"]
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr("mindroom.agent_cli.main.time.sleep", lambda _seconds: None)
    return sent


def _completed(outcome: object) -> dict[str, object]:
    return {"status": "completed", "outcome": outcome}


def test_workers_run_sends_one_shell_call_that_names_the_worker(capsys, monkeypatch) -> None:
    """Everything after ``--`` is the command, and the call is waited for however long it takes."""
    sent = _worker_client(
        monkeypatch,
        {"status": "queued"},
        {"status": "running"},
        _completed("[worker: e2e-3, started]\n" + format_shell_completion("shard 3 ok", None, return_code=0)),
    )

    argv = ["workers", "run", "e2e-3", "--resource-profile", "large", "--timeout", "900", "--tail", "20"]
    assert main([*argv, "--", "bash", "e2e/shard.sh", "--timeout", "3"]) == 0

    [call] = sent
    assert (call["operation"], call["toolkit"], call["function"]) == ("tools.call", "shell", "run_shell_command")
    assert call["arguments"] == {
        "args": ["bash", "e2e/shard.sh", "--timeout", "3"],
        "worker": "e2e-3",
        "resource_profile": "large",
        "timeout": 900,
        "tail": 20,
    }
    output = capsys.readouterr()
    assert (output.out, output.err) == ("shard 3 ok\n", "mindroom-agent: started worker e2e-3\n")


def test_workers_run_defaults_to_an_hour_and_the_normal_worker_size(monkeypatch) -> None:
    result = format_shell_completion("", None, return_code=0)
    sent = _worker_client(monkeypatch, _completed(f"[worker: build]\n{result}"))

    assert main(["workers", "run", "build", "--", "make test && make lint"]) == 0
    assert sent[0]["arguments"] == {"args": ["make test && make lint"], "worker": "build", "timeout": 3600, "tail": 100}


@pytest.mark.parametrize(
    "stdout",
    [
        "Exit code 0",
        "Exit code 3 was expected",
        "Exit code 3\nand more",
        "Stopped after 5s.",
        "Stopped after 60s. Nothing failed.",
        "Stopped after 60s.\nThen finished.",
        "[cwd: genuine]\nkept",
    ],
)
def test_workers_run_keeps_successful_command_output_verbatim(stdout, capsys, monkeypatch) -> None:
    """Output that resembles CLI or shell metadata is still printed."""
    result = format_shell_completion(stdout, None, return_code=0)
    _worker_client(monkeypatch, _completed(f"[worker: w]\n{result}"))

    assert main(["workers", "run", "w", "--timeout", "60", "--", "pytest"]) == 0
    assert capsys.readouterr().out == f"{stdout}\n"


@pytest.mark.parametrize(
    ("return_code", "exit_code"),
    [(1, 1), (3, 3), (137, 137), (255, 255), (-9, 137), (-15, 143)],
)
@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [("", ""), ("ran 4 tests", ""), ("", "Killed"), ("ran 4 tests", "1 failed")],
)
def test_workers_run_reads_a_failure_as_the_shell_words_it(
    return_code,
    exit_code,
    stdout,
    stderr,
    capsys,
    monkeypatch,
) -> None:
    """The shell's own wording of a failed command gives the exit code; a command a signal killed exits 128 + signal."""
    result = format_shell_completion(stdout, stderr, return_code=return_code)
    _worker_client(monkeypatch, _completed(f"[worker: w]\n{result}"))

    assert main(["workers", "run", "w", "--", "pytest"]) == exit_code
    streams = result.partition("\n")[2]
    assert capsys.readouterr().out == (f"{streams}\n" if streams else "")


@pytest.mark.parametrize("timeout", [1, 90, 3600])
@pytest.mark.parametrize(("stdout", "stderr"), [("", None), ("still going", None), ("still going", "warning")])
def test_workers_run_reads_a_stop_at_the_time_limit_as_the_shell_words_it(
    timeout,
    stdout,
    stderr,
    capsys,
    monkeypatch,
) -> None:
    """The shell's own wording of a command stopped at its time limit makes the CLI exit 124."""
    result = format_shell_timeout(stdout, stderr, timeout=timeout)
    _worker_client(monkeypatch, _completed(f"[worker: w]\n{result}"))

    assert main(["workers", "run", "w", "--timeout", str(timeout), "--", "sleep", "9999"]) == 124
    output = capsys.readouterr()
    streams = result.partition("\n")[2]
    assert output.out == (f"{streams}\n" if streams else "")
    assert output.err == f"mindroom-agent: stopped after {timeout}s\n"


class _Clock:
    """The CLI's clock, which only its own waiting moves."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def _unreachable_worker_client(
    monkeypatch,
    *,
    failed_reads: int | None,
    fail_submission: bool = False,
    failed_read_seconds: float = 0,
) -> tuple:
    """Simulate consecutive HTTP failures at the transport seam, using the actual client's timeouts."""
    clock = _Clock()
    reads: list[float] = []
    submissions: list[dict[str, object]] = []
    url = "http://primary:8765"

    def open_request(_opener, request: urllib.request.Request, *, timeout: float) -> BytesIO:
        if request.get_method() == "POST":
            assert request.data is not None
            submissions.append(json.loads(request.data))
            if fail_submission:
                raise TimeoutError
            return BytesIO(b'{"status":"running"}')
        assert request.full_url.split("?", 1)[0] == f"{url}/api/agent-cli/calls/{submissions[0]['call_id']}"
        reads.append(clock.now)
        if failed_reads is None or len(reads) <= failed_reads:
            clock.sleep(min(failed_read_seconds, timeout))
            raise TimeoutError
        receipt = _completed("[worker: w]\n" + format_shell_completion("shard ok", None, return_code=0))
        return BytesIO(json.dumps(receipt).encode())

    monkeypatch.setenv("MINDROOM_AGENT_CLI_URL", url)
    monkeypatch.setenv("MINDROOM_AGENT_CLI_TOKEN", "private-capability")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_request)
    monkeypatch.setattr("mindroom.agent_cli.main.time", clock)
    return clock, reads, submissions


def test_workers_run_keeps_waiting_when_reading_the_result_fails_for_a_while(capsys, monkeypatch) -> None:
    """The command runs on in its worker, so a read that fails is tried again."""
    _clock, reads, _submissions = _unreachable_worker_client(monkeypatch, failed_reads=5)

    assert main(["workers", "run", "w", "--", "bash", "shard.sh"]) == 0
    assert len(reads) == 6
    output = capsys.readouterr()
    assert (output.out, output.err) == ("shard ok\n", "")


@pytest.mark.parametrize("failed_read_seconds", [0, 30])
def test_workers_run_exits_255_when_the_result_stays_unreachable(failed_read_seconds, capsys, monkeypatch) -> None:
    """After a minute of failed reads (including time spent in HTTP), report that the command may still run."""
    clock, reads, submissions = _unreachable_worker_client(
        monkeypatch,
        failed_reads=None,
        failed_read_seconds=failed_read_seconds,
    )

    assert main(["workers", "run", "w", "--", "bash", "shard.sh"]) == 255
    assert len(submissions) == 1
    assert len(reads) > 1
    assert clock.now - reads[0] == pytest.approx(60)
    output = capsys.readouterr()
    assert output.out == ""
    assert "Agent CLI transport outcome is unknown" in output.err
    assert "may still be running in worker w" in output.err


def test_workers_run_counts_a_minute_of_failed_reads_from_the_last_one_that_worked(monkeypatch) -> None:
    clock = _Clock()
    done = _completed("[worker: w]\n" + format_shell_completion("ok", None, return_code=0))
    answers = iter([*[None] * 20, {"status": "running"}, *[None] * 20, done])

    class Client:
        def operation(self, payload) -> dict[str, object]:
            del payload
            return {"status": "running"}

        def receipt(
            self,
            call_id,
            *,
            timeout: float,
            wait: float | None = None,
            after_progress: int | None = None,
        ) -> dict[str, object]:
            del call_id, timeout, wait, after_progress
            answer = next(answers)
            if answer is None:
                msg = "Agent CLI transport outcome is unknown"
                raise AgentCliUnavailableError(msg)
            return answer

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr("mindroom.agent_cli.main.time", clock)

    # Each run of failed reads is shorter than a minute, and both together are longer.
    assert main(["workers", "run", "w", "--", "true"]) == 0
    assert clock.now > 60


@pytest.mark.parametrize(("failed_reads", "expected_code"), [(None, 255), (2, 0)])
def test_workers_run_recovers_after_a_lost_submission_response(
    capsys,
    monkeypatch,
    failed_reads,
    expected_code,
) -> None:
    """Only read the receipt: another submission could run the command twice."""
    clock, reads, submissions = _unreachable_worker_client(monkeypatch, failed_reads=failed_reads, fail_submission=True)

    assert main(["workers", "run", "w", "--", "true"]) == expected_code
    [submission] = submissions
    output = capsys.readouterr()
    if failed_reads is None:
        assert len(reads) > 1
        assert clock.now == pytest.approx(60)
        assert f"may still be running in worker w (call {submission['call_id']})" in output.err
    else:
        assert len(reads) == failed_reads + 1
        assert (output.out, output.err) == ("shard ok\n", "")


@pytest.mark.parametrize(
    ("receipt", "expected_code"),
    [
        (
            {
                "status": "failed",
                "error_kind": "named_worker_refusal",
                "outcome": "Cannot start worker 'w': 2 of the 2 named workers ...",
            },
            125,
        ),
        ({"status": "cancelled", "outcome": "Call cancelled before dispatch"}, 255),
        # Non-refusal receipts after submission exit 255 rather than claiming the command never ran.
        (_completed("Error: `worker` and `resource_profile` need a shell that runs in a worker"), 255),
        (_completed("[worker: other]\n" + format_shell_completion("ran elsewhere", None, return_code=0)), 255),
    ],
)
def test_workers_run_exits_125_for_typed_refusals_and_255_otherwise(
    receipt,
    expected_code,
    capsys,
    monkeypatch,
) -> None:
    """A typed refusal exits 125; other non-command receipts after submit exit 255 and print their reason on stderr."""
    _worker_client(monkeypatch, receipt)

    assert main(["workers", "run", "w", "--", "true"]) == expected_code
    output = capsys.readouterr()
    assert output.out == ""
    assert str(receipt["outcome"]) in output.err


def test_workers_run_exits_125_when_mindroom_cannot_be_reached(capsys, monkeypatch) -> None:
    monkeypatch.delenv("MINDROOM_AGENT_CLI_URL", raising=False)
    monkeypatch.delenv("MINDROOM_AGENT_CLI_TOKEN", raising=False)

    assert main(["workers", "run", "w", "--", "true"]) == 125
    output = capsys.readouterr()
    assert output.out == ""
    assert "Agent CLI authority is unavailable" in output.err


@pytest.mark.parametrize(
    "argv",
    [
        ["workers", "run", "w"],
        ["workers", "run", "w", "--"],
        ["workers", "run", "w", "true"],
        ["workers", "run", "--", "true"],
        ["workers", "run", "w", "--timeout", "0", "--", "true"],
        ["workers", "run", "w", "--tail", "-1", "--", "true"],
        ["workers", "run", "w", "--resource-profile", "huge", "--", "true"],
    ],
)
def test_workers_run_needs_a_name_a_command_and_usable_limits(argv) -> None:
    with pytest.raises(SystemExit):
        parse_arguments(argv)


@pytest.mark.parametrize("tail", [0, -1])
def test_workers_run_rejects_nonpositive_tail_without_submitting(tail, capsys, monkeypatch) -> None:
    """A zero tail would print every line rather than limiting the output."""
    result = format_shell_completion("finished", None, return_code=0)
    sent = _worker_client(monkeypatch, _completed(f"[worker: w]\n{result}"))

    assert main(["workers", "run", "w", "--tail", str(tail), "--", "true"]) == 2
    assert sent == []
    output = capsys.readouterr()
    assert output.out == ""
    assert "--tail" in output.err


def test_workers_run_prints_the_error_when_output_auto_save_fails(capsys, monkeypatch, tmp_path) -> None:
    """A refused output write returns a failure receipt, not a preview."""
    from mindroom.agent_cli.projection import project_cli_result  # noqa: PLC0415
    from mindroom.tool_system.output_files import ToolOutputFilePolicy  # noqa: PLC0415

    result = format_shell_completion("finished", None, return_code=0)
    policy = ToolOutputFilePolicy(tmp_path, max_bytes=1, auto_save_threshold_bytes=1)
    outcome = project_cli_result(f"[worker: w]\n{result}", policy, "run_shell_command")
    _worker_client(monkeypatch, _completed(outcome))

    assert main(["workers", "run", "w", "--", "true"]) == 255
    output = capsys.readouterr()
    assert output.out == ""
    assert "exceeds the 1 byte auto-save limit" in output.err


def test_a_background_script_cannot_run_commands_in_named_workers(capsys, monkeypatch, tmp_path) -> None:
    """Named workers belong to a reply, and a background script outlives the reply that started it."""
    _in_background_script(monkeypatch, tmp_path)

    assert main(["workers", "run", "w", "--", "true"]) == 125
    output = capsys.readouterr()
    assert output.out == ""
    assert "A named worker belongs to a reply in a conversation" in output.err
    assert "background script" in output.err
