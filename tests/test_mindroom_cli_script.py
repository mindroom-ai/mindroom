"""Run the operator CLI helper against a fake curl and check how it sends the provisioner key."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPOSITORY_ROOT / "cluster/scripts"
_FAKE_CURL = r"""
import json
import os
import sys

with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"args": sys.argv[1:], "stdin": sys.stdin.read()}) + "\n")
print("{}")
"""
_PROVISIONER_KEY = "provisioner-key-for-tests"


def _run_cli(tmp_path: Path, *args: str) -> list[dict]:
    """Run a copy of the script, so no repository `.env` is loaded, and return the curl invocations."""
    script = tmp_path / "cluster/scripts/mindroom-cli.sh"
    script.parent.mkdir(parents=True)
    shutil.copy(_SCRIPTS_DIR / "mindroom-cli.sh", script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(f"#!{sys.executable}\n{_FAKE_CURL}", encoding="utf-8")
    curl.chmod(0o755)
    for name in ("jq", "dirname"):
        resolved = shutil.which(name)
        if resolved is None:
            pytest.skip(f"{name} is required to run the CLI helper")
        (bin_dir / name).symlink_to(resolved)
    bash = shutil.which("bash")
    assert bash is not None
    log = tmp_path / "curl.jsonl"
    log.touch()
    completed = subprocess.run(
        [bash, str(script), *args],
        env={
            "PATH": str(bin_dir),
            "HOME": str(tmp_path),
            "FAKE_LOG": str(log),
            "PROVISIONER_API_KEY": _PROVISIONER_KEY,
            "API_URL": "https://api.example.test",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.parametrize(
    ("command", "url"),
    [
        ("provision", "https://api.example.test/system/provision"),
        ("deprovision", "https://api.example.test/system/instances/7/uninstall"),
    ],
)
def test_cli_sends_provisioner_key_only_over_verified_tls_and_stdin(tmp_path: Path, command: str, url: str) -> None:
    """The platform's root key must never go to an unverified server or appear in process arguments."""
    calls = _run_cli(tmp_path, command, "7")

    assert len(calls) == 1
    args = calls[0]["args"]
    assert url in args
    assert "-k" not in args
    assert "--insecure" not in args
    assert all(_PROVISIONER_KEY not in arg for arg in args)
    assert calls[0]["stdin"] == f"Authorization: Bearer {_PROVISIONER_KEY}\n"


def test_operator_scripts_never_disable_curl_certificate_checks() -> None:
    """Platform hosts have public certificates, so no operator script may skip their verification."""
    insecure_flag = re.compile(r"\bcurl\b.*?\s(?:--insecure\b|-[A-Za-z]*k[A-Za-z]*\b)")
    offenders = []
    for script in sorted(_SCRIPTS_DIR.rglob("*.sh")):
        logical_lines = script.read_text(encoding="utf-8").replace("\\\n", " ").splitlines()
        offenders.extend(f"{script.name}: {line.strip()}" for line in logical_lines if insecure_flag.search(line))

    assert offenders == []
