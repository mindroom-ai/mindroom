"""Run the platform API key update script against a fake kubectl."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPOSITORY_ROOT / "cluster/scripts/update-api-keys.sh"


def test_update_warns_that_a_chart_managed_secret_needs_the_helm_values(tmp_path: Path) -> None:
    """The script patches platform-secrets directly, so it says the chart's values must change too."""
    bash = shutil.which("bash")
    assert bash is not None
    scripts = tmp_path / "cluster" / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "update-api-keys.sh"
    shutil.copy(_SCRIPT, script)
    (tmp_path / "saas-platform").mkdir()
    (tmp_path / "saas-platform" / ".env").write_text("OPENAI_API_KEY=sk-placeholder\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "kubectl-calls"
    (bin_dir / "kubectl").write_text(
        f"#!{sys.executable}\nimport json, sys\nwith open({str(log)!r}, 'a') as f:\n    f.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    (bin_dir / "kubectl").chmod(0o755)

    result = subprocess.run(
        [bash, str(script)],
        # Keys exported in the developer's shell must not reach the recorded kubectl calls.
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=True,
    )

    assert "set these keys in its Helm values (apiKeys and supabase.serviceKey)" in result.stdout
    assert "the next helm upgrade restores the previous keys" in result.stdout
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    [patch] = [call for call in calls if call[:2] == ["patch", "secret"]]
    assert patch[2] == "platform-secrets"
    operations = json.loads(next(arg for arg in patch if arg.startswith("-p=")).removeprefix("-p="))
    openai = next(operation for operation in operations if operation["path"] == "/data/openai_key")
    assert base64.b64decode(openai["value"]) == b"sk-placeholder"
