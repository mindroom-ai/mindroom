"""The worker CLI parses strict input without loading the runtime."""

# ruff: noqa: D103, ANN001
from __future__ import annotations

import json
import subprocess
import sys
from io import BytesIO, TextIOWrapper

import pytest

from mindroom.agent_cli.main import main, parse_arguments, read_call_arguments


def test_call_accepts_json_object() -> None:

    args = parse_arguments(["tools", "call", "calculator", "add", "--json", '{"a":1,"b":2}'])
    assert read_call_arguments(args) == {"a": 1, "b": 2}


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
            "import sys; from mindroom.agent_cli.main import main; assert not any(x.startswith(('agno', 'pydantic', 'mindroom.api', 'mindroom.config', 'mindroom.credentials')) for x in sys.modules); main(['--help'])",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "tools" in result.stdout


def test_main_reports_missing_authority(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.delenv("MINDROOM_AGENT_CLI_GATEWAY_URL", raising=False)
    monkeypatch.delenv("MINDROOM_AGENT_CLI_TOKEN_PATH", raising=False)
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


def test_unknown_transport_preserves_exact_call_id(capsys, monkeypatch) -> None:

    monkeypatch.delenv("MINDROOM_AGENT_CLI_GATEWAY_URL", raising=False)
    call_id = "12345678-1234-4234-8234-123456789abc"
    assert main(["tools", "call", "a", "b", "--call-id", call_id]) == 4
    assert json.loads(capsys.readouterr().out)["call_id"] == call_id
