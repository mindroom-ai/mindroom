"""The agent CLI parses strict input without loading the runtime."""

# ruff: noqa: D103, ANN001
from __future__ import annotations

import json
import subprocess
import sys
from io import BytesIO, TextIOWrapper

import pytest

from mindroom.agent_cli.client import AgentCliUnavailableError
from mindroom.agent_cli.main import main, parse_arguments, read_call_arguments
from mindroom.script_runs.launch_contract import digest_arguments


def test_call_accepts_json_object() -> None:

    args = parse_arguments(["tools", "call", "calculator", "add", "--json", '{"a":1,"b":2}'])
    assert read_call_arguments(args) == {"a": 1, "b": 2}


def test_dotted_name_selects_toolkit_and_function() -> None:

    describe = parse_arguments(["tools", "describe", "matrix_message.matrix_message"])
    assert (describe.toolkit, describe.function) == ("matrix_message", "matrix_message")
    call = parse_arguments(["tools", "call", "todo.add_todo", "--json", "{}"])
    assert (call.toolkit, call.function, call.timeout) == ("todo", "add_todo", 30)


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


def test_a_failed_background_script_call_prints_its_error(capsys, monkeypatch, tmp_path) -> None:

    _in_background_script(monkeypatch, tmp_path)
    _script_gateway(monkeypatch, "failed", error={"kind": "tool_failure", "message": "boom", "retryable": False})

    assert main(["tools", "call", "slack", "slack_search", "{}"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "call_id": "call-1",
        "toolkit": "slack",
        "function": "slack_search",
        "status": "failed",
        "outcome": {"error": "boom", "kind": "tool_failure"},
    }


@pytest.mark.parametrize(
    "argv",
    [["tools", "list"], ["calls", "get", "12345678-1234-4234-8234-123456789abc"], ["context", "list"]],
)
def test_a_background_script_is_offered_only_tool_calls(argv, capsys, monkeypatch, tmp_path) -> None:

    _in_background_script(monkeypatch, tmp_path)
    assert main(argv) == 2
    assert "background script" in json.loads(capsys.readouterr().out)["error"]
