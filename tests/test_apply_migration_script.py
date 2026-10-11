"""Run the Supabase migration script against a fake Management API."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPOSITORY_ROOT / "cluster/scripts/db/apply-migration.sh"
_FAKE_CURL = r"""
import json
import os
import sys

args = sys.argv[1:]
headers = [open(arg[1:]).read() for arg in args if arg.startswith("@/dev/fd/")]
with open(os.environ["FAKE_LOG"], "w") as log:
    json.dump({"args": args, "headers": headers, "body": json.loads(sys.stdin.read())}, log)
print(os.environ["FAKE_RESPONSE"], end="")
"""


def _run(tmp_path: Path, response: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    """Apply a small migration with curl replaced at the executable boundary."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(f"#!{sys.executable}\n{_FAKE_CURL}", encoding="utf-8")
    curl.chmod(0o755)
    for name in ("jq", "sed", "tail"):
        resolved = shutil.which(name)
        assert resolved is not None, f"Required shell utility is missing: {name}"
        (bin_dir / name).symlink_to(resolved)
    bash = shutil.which("bash")
    assert bash is not None
    migration = tmp_path / "005_test.sql"
    migration.write_text("BEGIN;\nSELECT 1;\nCOMMIT;\n", encoding="utf-8")
    log = tmp_path / "curl.json"
    environment = {
        "PATH": str(bin_dir),
        "SUPABASE_ACCESS_TOKEN": "sbp_test_token",
        "SUPABASE_PROJECT_REF": "testref",
        "FAKE_LOG": str(log),
        "FAKE_RESPONSE": response,
    }
    result = subprocess.run(
        [bash, str(_SCRIPT), str(migration)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, json.loads(log.read_text())


def test_migration_is_sent_with_token_off_the_command_line(tmp_path: Path) -> None:
    """The SQL goes in the query body and the token only in a header file."""
    result, call = _run(tmp_path, "[]\n201")
    assert result.returncode == 0, result.stderr
    assert call["body"] == {"query": "BEGIN;\nSELECT 1;\nCOMMIT;\n"}
    assert call["args"][-1] == "https://api.supabase.com/v1/projects/testref/database/query"
    assert call["headers"] == ["Authorization: Bearer sbp_test_token\n"]
    assert "sbp_test_token" not in " ".join(call["args"])
    assert "sbp_test_token" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "response",
    ['{"message":"ERROR: 42601: syntax error"}\n400', '{"message":"Unauthorized"}\n401'],
)
def test_non_2xx_response_fails(tmp_path: Path, response: str) -> None:
    """A rejected query prints the API error and exits non-zero."""
    result, _ = _run(tmp_path, response)
    assert result.returncode == 1
    assert "Migration failed" in result.stderr
    assert json.loads(response.rsplit("\n", 1)[0])["message"] in result.stdout
