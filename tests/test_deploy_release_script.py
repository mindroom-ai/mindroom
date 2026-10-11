"""Run the SaaS release deploy script against fake cluster, registry, and API tools."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPOSITORY_ROOT / "cluster/scripts/deploy-release.sh"
_FAKE_TOOL = r"""
import base64
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
stdin = sys.stdin.read() if name == "curl" else ""
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"name": name, "args": args, "stdin": stdin}) + "\n")
if name == "git" and "--show-toplevel" in args:
    print(os.environ["FAKE_REPO"])
elif name == "helm" and args[:2] == ["get", "values"]:
    print(os.environ["FAKE_VALUES"] if "json" in args else "domain: example.test")
elif name == "kubectl" and args[:2] == ["get", "secret"]:
    key = args[-1].removeprefix("jsonpath={.data.").removesuffix("}")
    print(base64.b64encode(f"secret-{key}".encode()).decode())
elif name == "curl":
    if any("rest/v1/instances" in arg for arg in args):
        print(os.environ["FAKE_ROWS"])
    elif any(arg.endswith("/system/provision") for arg in args):
        body = json.loads(args[args.index("-d") + 1])
        failed = str(body["instance_id"]) in os.environ.get("FAKE_FAIL_IDS", "").split(",")
        print('{"detail":"boom"}\n500' if failed else '{"success":true}\n200', end="")
    else:
        print('{"status":"ok"}')
"""
_ROWS = [
    {"instance_id": 1, "subscription_id": "s1", "account_id": "a1", "status": "running", "lifecycle_stopped_at": None},
    {"instance_id": 2, "subscription_id": "s2", "account_id": "a2", "status": "stopped", "lifecycle_stopped_at": "t"},
    {"instance_id": 3, "subscription_id": "s3", "account_id": "a3", "status": "stopped", "lifecycle_stopped_at": None},
    {
        "instance_id": 4,
        "subscription_id": "s4",
        "account_id": "a4",
        "status": "deprovisioned",
        "lifecycle_stopped_at": "t",
    },
]
_VALUES = {"domain": "example.test", "environment": "staging", "supabase": {"url": "https://sb.test"}}


class _Run(NamedTuple):
    """Exit status, output, and every fake tool invocation of one script run."""

    returncode: int
    output: str
    calls: list[dict]

    def provisioned(self) -> list[dict]:
        """Return the provision request bodies in order."""
        return [
            json.loads(call["args"][call["args"].index("-d") + 1])
            for call in self.calls
            if call["name"] == "curl" and call["args"][-1].endswith("/system/provision")
        ]


def _run(tmp_path: Path, *args: str, values: dict = _VALUES, fail_ids: str = "") -> _Run:
    """Run the script with cluster, network, git, and delay commands replaced at the executable boundary."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("git", "tar", "helm", "kubectl", "curl", "sudo", "sleep"):
        executable = bin_dir / name
        executable.write_text(f"#!{sys.executable}\n{_FAKE_TOOL}", encoding="utf-8")
        executable.chmod(0o755)
    for name in ("jq", "base64", "sed", "tail", "date", "mktemp", "mkdir", "rm", "dirname"):
        resolved = shutil.which(name)
        assert resolved is not None, f"Required shell utility is missing: {name}"
        (bin_dir / name).symlink_to(resolved)
    bash = shutil.which("bash")
    assert bash is not None
    log = tmp_path / "calls.jsonl"
    log.touch()
    rows = [{**row, "subscriptions": {"tier": "pro"}} for row in _ROWS]
    environment = {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "BACKUP_DIR": str(tmp_path / "backup"),
        "FAKE_LOG": str(log),
        "FAKE_REPO": str(tmp_path),
        "FAKE_VALUES": json.dumps(values),
        "FAKE_ROWS": json.dumps(rows),
        "FAKE_FAIL_IDS": fail_ids,
    }
    result = subprocess.run(
        [bash, str(_SCRIPT), "v2026.9.1", *args],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return _Run(result.returncode, result.stdout + result.stderr, calls)


@pytest.mark.parametrize(
    ("selection", "expected_ids"),
    [(None, [1]), ("running", [1]), ("all", [1, 2]), ("2,1", [1, 2]), ("none", [])],
)
def test_instance_selection_skips_manually_stopped_and_deprovisioned(
    tmp_path: Path,
    selection: str | None,
    expected_ids: list[int],
) -> None:
    """Default and all selections never include manually stopped or deprovisioned instances."""
    run = _run(tmp_path, *(["--instances", selection] if selection else []))
    assert run.returncode == 0, run.output
    assert [body["instance_id"] for body in run.provisioned()] == expected_ids
    assert not any("/stop" in arg for call in run.calls for arg in call["args"])


@pytest.mark.parametrize("instance_id", ["3", "4", "1,3"])
def test_explicit_manually_stopped_or_deprovisioned_ids_are_refused(tmp_path: Path, instance_id: str) -> None:
    """Explicitly requested manually stopped or deprovisioned instances stop the run before any change."""
    run = _run(tmp_path, "--instances", instance_id)
    assert run.returncode == 1
    assert "Refusing manually stopped or deprovisioned instances" in run.output
    assert not any(call["name"] in {"sudo", "tar"} for call in run.calls)
    assert run.provisioned() == []


def test_dry_run_makes_no_mutating_calls(tmp_path: Path) -> None:
    """A dry run reads state and renders Helm, but never pulls, upgrades, provisions, or waits on rollouts."""
    run = _run(tmp_path, "--instances", "all", "--dry-run")
    assert run.returncode == 0, run.output
    helm_upgrades = [call["args"] for call in run.calls if call["name"] == "helm" and call["args"][0] == "upgrade"]
    assert helm_upgrades
    assert all("--dry-run" in args for args in helm_upgrades)
    assert not any(call["name"] == "sudo" for call in run.calls)
    assert not any(call["name"] == "kubectl" and "rollout" in call["args"] for call in run.calls)
    assert run.provisioned() == []


def test_payload_tier_secrets_and_namespaces(tmp_path: Path) -> None:
    """The tier comes from the subscription, secrets stay off the command line, and the Secret is read from the chart namespace."""
    run = _run(tmp_path)
    assert run.returncode == 0, run.output
    assert run.provisioned() == [{"subscription_id": "s1", "account_id": "a1", "tier": "pro", "instance_id": 1}]
    secret_reads = [
        call["args"] for call in run.calls if call["name"] == "kubectl" and call["args"][:2] == ["get", "secret"]
    ]
    assert all(args[args.index("-n") + 1] == "mindroom-staging" for args in secret_reads)
    helm_calls = [call["args"] for call in run.calls if call["name"] == "helm"]
    assert all(args[args.index("-n") + 1] == "mindroom-production" for args in helm_calls)
    all_args = " ".join(arg for call in run.calls for arg in call["args"])
    assert "secret-" not in all_args
    assert "secret-" not in run.output


def test_empty_instance_base_domain_falls_back_to_domain(tmp_path: Path) -> None:
    """An empty provisioner.instanceBaseDomain is treated as unset for instance health checks."""
    run = _run(tmp_path, values={**_VALUES, "provisioner": {"instanceBaseDomain": ""}})
    assert run.returncode == 0, run.output
    assert any(call["args"][-1] == "https://1.example.test/api/health" for call in run.calls if call["name"] == "curl")


def test_failed_instance_does_not_stop_the_run(tmp_path: Path) -> None:
    """A failed provision is recorded, later instances still run, and the script exits non-zero."""
    run = _run(tmp_path, "--instances", "all", fail_ids="1")
    assert run.returncode == 1
    assert [body["instance_id"] for body in run.provisioned()] == [1, 2]
    assert "Finished with failures: 1" in run.output
