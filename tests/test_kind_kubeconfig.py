"""Run kind diagnostics with fake tools to check cluster selection safely."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_FAKE_TOOL = r"""
import json
import os
import signal
import sys
import time
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
if name == "kubectl":
    record = {"kubeconfig": os.environ.get("KUBECONFIG"), "args": args}
    (Path(os.environ["KIND_TEST_LOG_DIR"]) / f"{os.getpid()}.json").write_text(json.dumps(record))
    if "port-forward" in args:
        local_port, remote_port = args[-1].split(":")
        print(f"Forwarding from 127.0.0.1:{local_port} -> {remote_port}", flush=True)
        signal.pause()
    elif "get" in args and "pods" in args:
        print("platform-frontend 1/1 Running 0 1m")
        print("platform-backend 1/1 Running 0 1m")
elif name == "kind":
    if args == ["get", "clusters"]:
        print("mindroom")
    else:
        raise SystemExit("Unexpected kind operation")
elif name == "curl":
    print("200" if "%{http_code}" in args else '{"status":"ok"}')
elif name == "sleep":
    time.sleep(0.1)
elif name != "pkill":
    raise SystemExit(f"Unexpected tool: {name}")
"""


class _Invocation(NamedTuple):
    """One kubectl process observed at the command boundary."""

    kubeconfig: str | None
    args: list[str]


def _run_diagnostic(tmp_path: Path, script: str, kubeconfig: str | None) -> list[_Invocation]:
    """Replace cluster, network, process-search, and delay commands at the executable boundary."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_dir = tmp_path / "commands"
    log_dir.mkdir()
    for name in ("kubectl", "kind", "curl", "sleep", "pkill"):
        executable = bin_dir / name
        executable.write_text(f"#!{sys.executable}\n{_FAKE_TOOL}", encoding="utf-8")
        executable.chmod(0o755)
    for name in ("grep", "wc", "awk", "cat", "mktemp", "rm"):
        resolved = shutil.which(name)
        assert resolved is not None, f"Required shell utility is missing: {name}"
        (bin_dir / name).symlink_to(resolved)
    bash = shutil.which("bash")
    assert bash is not None, "Bash is required for kind diagnostic scripts"
    source = (_REPOSITORY_ROOT / "cluster/k8s/kind" / script).read_text(encoding="utf-8")
    isolated_script = tmp_path / script
    isolated_script.write_text(source.replace("/tmp/", f"{tmp_path}/"), encoding="utf-8")  # noqa: S108 - redirect helper logs into the isolated fixture.
    environment = {"PATH": str(bin_dir), "KIND_TEST_LOG_DIR": str(log_dir), "TMPDIR": str(tmp_path)}
    if kubeconfig is not None:
        environment["KUBECONFIG"] = kubeconfig
    result = subprocess.run(
        [bash, str(isolated_script)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    invocations = []
    for path in log_dir.glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        invocations.append(_Invocation(record["kubeconfig"], record["args"]))
    assert len(invocations) == (6 if script == "test-access.sh" else 3), result.stdout
    return invocations


@pytest.mark.parametrize("script", ["test-access.sh", "validate.sh"])
@pytest.mark.parametrize(
    "kubeconfig",
    [None, "", "/synthetic/custom config", "/synthetic/first:/synthetic/second"],
    ids=["unset", "empty", "custom-file", "merged-files"],
)
def test_kind_diagnostics_preserve_bootstrap_kubeconfig(
    tmp_path: Path,
    script: str,
    kubeconfig: str | None,
) -> None:
    """Diagnostics must not redirect the caller to an unrelated kubeconfig file."""
    invocations = _run_diagnostic(tmp_path, script, kubeconfig)
    assert all(call.kubeconfig == kubeconfig for call in invocations), invocations


@pytest.mark.parametrize("script", ["test-access.sh", "validate.sh"])
def test_kind_diagnostics_select_the_named_cluster(tmp_path: Path, script: str) -> None:
    """Every query and forward must use the intended cluster regardless of current context."""
    invocations = _run_diagnostic(tmp_path, script, "/synthetic/custom config")
    for call in invocations:
        args = call.args
        assert "--context=kind-mindroom" in args or any(
            args[index : index + 2] == ["--context", "kind-mindroom"] for index in range(len(args) - 1)
        ), args
