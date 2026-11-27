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
from mindroom.script_runs.transport import ScriptGatewayTransport
from mindroom.shell.output_capture import format_shell_completion
from tests.workers_run_frames import command_frames


def test_call_accepts_json_object() -> None:

    args = parse_arguments(["tools", "call", "calculator", "add", "--json", '{"a":1,"b":2}'])
    assert read_call_arguments(args) == {"a": 1, "b": 2}


def test_dotted_name_selects_toolkit_and_function() -> None:

    describe = parse_arguments(["tools", "describe", "matrix_message.matrix_message"])
    assert (describe.toolkit, describe.function) == ("matrix_message", "matrix_message")
    call = parse_arguments(["tools", "call", "todo.todo", "--json", "{}"])
    assert (call.toolkit, call.function, call.timeout) == ("todo", "todo", None)


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


def _script_gateway(
    monkeypatch,
    state: str,
    *,
    result=None,
    error=None,
    on_poll=None,
) -> list[dict[str, object]]:
    """Answer every submission with one receipt in *state*, and return the submissions."""
    submitted: list[dict[str, object]] = []

    def open_request(_self, request) -> BytesIO:
        assert request.headers["Authorization"] == "Bearer script-token"
        if request.method != "POST" and on_poll is not None:
            return on_poll(request)
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

    monkeypatch.setattr(ScriptGatewayTransport, "_open", open_request)
    return submitted


def test_a_background_script_calls_tools_with_its_own_capability(capsys, monkeypatch, tmp_path) -> None:

    _in_background_script(monkeypatch, tmp_path)
    submitted = _script_gateway(monkeypatch, "completed", result={"messages": 3})
    # Arguments may share a name with the SDK's own parameters.
    arguments = {"query": "launch", "function_name": "kept"}

    assert main(["tools", "call", "slack.slack_read", json.dumps(arguments)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "toolkit": "slack",
        "function": "slack_read",
        "status": "completed",
        "outcome": {"messages": 3},
    }
    assert [(call["toolkit_name"], call["function_name"], call["arguments"]) for call in submitted] == [
        ("slack", "slack_read", arguments),
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

    assert main(["tools", "call", "slack", "slack_read", "{}"]) == exit_code
    assert json.loads(capsys.readouterr().out) == {
        "call_id": "call-1",
        "toolkit": "slack",
        "function": "slack_read",
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

    def refuse_poll(request) -> BytesIO:
        # The requester lost access after the call was accepted, so the gateway refuses to show its receipt.
        raise HTTPError(request.full_url, 404, "Not Found", Message(), BytesIO(b'{"detail":"unavailable"}'))

    pending = _script_gateway(monkeypatch, "pending", on_poll=refuse_poll)
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

    def open_request(_self, request) -> BytesIO:
        # A tool outside the run's grants is refused before the gateway records the call.
        raise HTTPError(request.full_url, 404, "Not Found", Message(), BytesIO(b'{"detail":"unavailable"}'))

    monkeypatch.setattr(ScriptGatewayTransport, "_open", open_request)

    assert main(["tools", "call", "memory.memory", "{}"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def _worker_client(monkeypatch, *frames: dict[str, object]) -> list[dict[str, object]]:
    """Answer one submitted call with the stream *frames*, and return what was sent."""
    sent: list[dict[str, object]] = []

    class Client:
        def operation(self, payload) -> dict[str, object]:
            sent.append(payload)
            return {"status": "queued"}

        def stream(self, call_id) -> object:
            assert call_id == sent[0]["call_id"]
            return iter(frames)

        def stdin(self, call_id, seq, data, *, eof) -> dict[str, object]:
            # Under pytest the CLI's stdin cannot be read, which it forwards as an empty one.
            assert (call_id, seq, data, eof) == (sent[0]["call_id"], 0, b"", True)
            return {"open": False}

    monkeypatch.setattr("mindroom.agent_cli.main.AgentCliClient", Client)
    monkeypatch.setattr("mindroom.agent_cli.main.time.sleep", lambda _seconds: None)
    return sent


def _completed(outcome: object) -> dict[str, object]:
    return {"status": "completed", "outcome": outcome}


def test_workers_run_sends_one_shell_call_that_names_the_worker(capsys, monkeypatch) -> None:
    """Everything after ``--`` is the command, and the call is read however long it takes."""
    sent = _worker_client(
        monkeypatch,
        {"t": "progress", "text": "starting worker e2e-3..."},
        {"t": "keepalive"},
        *command_frames(b"shard 3 ok\n", header="[worker: e2e-3, started]"),
    )

    argv = ["workers", "run", "e2e-3", "--resource-profile", "large", "--timeout", "900", "--tail", "20"]
    assert main([*argv, "--", "bash", "e2e/shard.sh", "--timeout", "3"]) == 0

    [call] = sent
    assert (call["operation"], call["toolkit"], call["function"]) == ("tools.call", "shell", "run_shell_command")
    # `--tail` stays an argument of the call, so approval, hooks and audit see the call they saw before streaming.
    assert call["arguments"] == {
        "args": ["bash", "e2e/shard.sh", "--timeout", "3"],
        "worker": "e2e-3",
        "resource_profile": "large",
        "timeout": 900,
        "tail": 20,
    }
    assert call["stream"] is True
    output = capsys.readouterr()
    assert output.out == "shard 3 ok\n"
    assert output.err == "mindroom-agent: starting worker e2e-3...\nmindroom-agent: started worker e2e-3\n"


def test_workers_run_defaults_to_an_hour_and_the_normal_worker_size(monkeypatch) -> None:
    sent = _worker_client(monkeypatch, *command_frames(header="[worker: build]"))

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
        "[worker: w]\nOutput:\nno final newline",
    ],
)
def test_workers_run_keeps_successful_command_output_verbatim(stdout, capsys, monkeypatch) -> None:
    """Output that resembles CLI or shell metadata is printed as it is, with nothing added, not even a newline."""
    _worker_client(monkeypatch, *command_frames(stdout.encode()))

    assert main(["workers", "run", "w", "--timeout", "60", "--", "pytest"]) == 0
    output = capsys.readouterr()
    assert (output.out, output.err) == (stdout, "")


@pytest.mark.parametrize(
    ("return_code", "exit_code"),
    [(1, 1), (3, 3), (137, 137), (255, 255), (-9, 137), (-15, 143)],
)
@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [("", ""), ("ran 4 tests", ""), ("", "Killed"), ("ran 4 tests", "1 failed")],
)
def test_workers_run_exits_with_its_command_s_exit_code(
    return_code,
    exit_code,
    stdout,
    stderr,
    capsys,
    monkeypatch,
) -> None:
    """A failed command's exit code is the CLI's, after its output; a command a signal killed exits 128 + signal."""
    _worker_client(monkeypatch, *command_frames(stdout.encode(), stderr.encode(), code=return_code))

    assert main(["workers", "run", "w", "--", "pytest"]) == exit_code
    output = capsys.readouterr()
    assert (output.out, output.err) == (stdout, stderr)


@pytest.mark.parametrize("timeout", [1, 90, 3600])
@pytest.mark.parametrize(("stdout", "stderr"), [("", ""), ("still going\n", ""), ("still going\n", "warning\n")])
def test_workers_run_exits_124_for_a_command_stopped_at_its_time_limit(
    timeout,
    stdout,
    stderr,
    capsys,
    monkeypatch,
) -> None:
    """A command stopped at its time limit makes the CLI exit 124 and say so on stderr, after the output so far."""
    _worker_client(monkeypatch, *command_frames(stdout.encode(), stderr.encode(), code=-9, timed_out=True))

    assert main(["workers", "run", "w", "--timeout", str(timeout), "--", "sleep", "9999"]) == 124
    output = capsys.readouterr()
    assert output.out == stdout
    assert output.err == f"{stderr}mindroom-agent: stopped after {timeout}s\n"


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
    frames_before_failure: int = 0,
) -> tuple:
    """Simulate consecutive HTTP failures at the transport seam, using the actual client's timeouts.

    Each failed read of the stream first yields ``frames_before_failure`` of the command's frames.
    """
    clock = _Clock()
    reads: list[float] = []
    submissions: list[dict[str, object]] = []
    url = "http://primary:8765"
    lines = [json.dumps(frame).encode() + b"\n" for frame in command_frames(b"shard ok\n")]

    class Broken(BytesIO):
        def readline(self, size: int | None = -1) -> bytes:
            line = super().readline(size)
            if not line:
                raise TimeoutError
            return line

    def open_request(_opener, request: urllib.request.Request, *, timeout: float) -> BytesIO:
        if request.full_url.endswith("/stdin"):
            return BytesIO(b'{"open":false}')
        if request.get_method() == "POST":
            assert request.data is not None
            submissions.append(json.loads(request.data))
            if fail_submission:
                raise TimeoutError
            return BytesIO(b'{"status":"running"}')
        assert request.full_url == f"{url}/api/agent-cli/calls/{submissions[0]['call_id']}/stream"
        reads.append(clock.now)
        if failed_reads is None or len(reads) <= failed_reads:
            clock.sleep(min(failed_read_seconds, timeout))
            if frames_before_failure:
                return Broken(b"".join(lines[:frames_before_failure]))
            raise TimeoutError
        return BytesIO(b"".join(lines))

    monkeypatch.setenv("MINDROOM_AGENT_CLI_URL", url)
    monkeypatch.setenv("MINDROOM_AGENT_CLI_TOKEN", "private-capability")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_request)
    monkeypatch.setattr("mindroom.agent_cli.main.time", clock)
    return clock, reads, submissions


def test_workers_run_keeps_trying_when_its_stream_cannot_be_reached_for_a_while(capsys, monkeypatch) -> None:
    """The command runs on in its worker, so a stream that cannot be reached is tried again."""
    _clock, reads, _submissions = _unreachable_worker_client(monkeypatch, failed_reads=5)

    assert main(["workers", "run", "w", "--", "bash", "shard.sh"]) == 0
    assert len(reads) == 6
    output = capsys.readouterr()
    assert (output.out, output.err) == ("shard ok\n", "")


@pytest.mark.parametrize("failed_read_seconds", [0, 30])
def test_workers_run_exits_255_when_the_stream_stays_unreachable(failed_read_seconds, capsys, monkeypatch) -> None:
    """After a minute of failed reads (including time spent in HTTP), report that the command may still run."""
    clock, reads, submissions = _unreachable_worker_client(
        monkeypatch,
        failed_reads=None,
        failed_read_seconds=failed_read_seconds,
    )

    assert main(["workers", "run", "w", "--", "bash", "shard.sh"]) == 255
    assert len(submissions) == 1
    assert len(reads) > 1
    assert clock.now - reads[0] == pytest.approx(60, abs=1)
    output = capsys.readouterr()
    assert output.out == ""
    assert "Agent CLI transport outcome is unknown" in output.err
    assert "may still be running in worker w" in output.err


@pytest.mark.parametrize("frames_before_failure", [1, 2])
def test_workers_run_does_not_read_a_stream_again_that_broke_after_it_began(
    frames_before_failure,
    capsys,
    monkeypatch,
) -> None:
    """MindRoom keeps no output for a second reader, so a stream lost midway never ends as a success."""
    _clock, reads, submissions = _unreachable_worker_client(
        monkeypatch,
        failed_reads=1,
        frames_before_failure=frames_before_failure,
    )

    assert main(["workers", "run", "w", "--", "bash", "shard.sh"]) == 255
    assert len(reads) == 1
    output = capsys.readouterr()
    assert output.out == ("shard ok\n" if frames_before_failure == 2 else "")
    assert f"may still be running in worker w (call {submissions[0]['call_id']})" in output.err


@pytest.mark.parametrize(("failed_reads", "expected_code"), [(None, 255), (2, 0)])
def test_workers_run_recovers_after_a_lost_submission_response(
    capsys,
    monkeypatch,
    failed_reads,
    expected_code,
) -> None:
    """Only read the stream: another submission could run the command twice."""
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
        # A call that ended without its command's exit is no success, whatever its receipt's text says.
        (_completed("[worker: w]\n" + format_shell_completion("ran unstreamed", None, return_code=0)), 255),
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
    """`--tail` no longer limits anything, and a value that never made sense is still refused."""
    sent = _worker_client(monkeypatch, *command_frames(b"finished\n"))

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


def test_workers_run_passes_sandbox_flags_and_omits_unset_flags(monkeypatch) -> None:
    """Sandbox flags map directly to tool arguments; omitted flags stay absent so follow-ups inherit locked settings."""
    sent = _worker_client(monkeypatch, *command_frames(b"ok\n"))
    assert main(["workers", "run", "sbx", "--sandbox", "--", "echo", "hi"]) == 0
    assert sent[0]["arguments"] == {
        "args": ["echo", "hi"],
        "worker": "sbx",
        "timeout": 3600,
        "tail": 100,
        "sandbox": True,
    }

    sent = _worker_client(monkeypatch, *command_frames(b"ok\n"))
    assert main(["workers", "run", "sbx", "--mount-dir", "scratch/job1", "--mount-read-only", "--", "ls"]) == 0
    assert sent[0]["arguments"] == {
        "args": ["ls"],
        "worker": "sbx",
        "timeout": 3600,
        "tail": 100,
        "mount_dir": "scratch/job1",
        "mount_read_only": True,
    }

    sent = _worker_client(monkeypatch, *command_frames(b"ok\n"))
    assert main(["workers", "run", "sbx", "--", "pwd"]) == 0
    assert sent[0]["arguments"] == {
        "args": ["pwd"],
        "worker": "sbx",
        "timeout": 3600,
        "tail": 100,
    }
