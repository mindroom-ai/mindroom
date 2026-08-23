"""Tests for the post-deployment documentation smoke check."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS_WORKFLOW = ROOT / ".github" / "workflows" / "docs.yml"


@pytest.fixture(scope="module")
def docs_workflow() -> dict[str, Any]:
    """Return the parsed documentation workflow."""
    workflow = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding="utf-8"))
    if True in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


def _smoke_step(workflow: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the deployed-docs smoke job and its only shell step."""
    jobs = workflow["jobs"]
    assert "smoke-deployment" in jobs, "the docs deployment must have a downstream smoke job"
    job = jobs["smoke-deployment"]
    matches = [step for step in job["steps"] if step.get("id") == "smoke_deployed_docs"]
    assert len(matches) == 1
    return job, matches[0]


def _run_smoke_script(script: str, pages: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the real workflow script with only curl and sleep replaced."""
    bash = shutil.which("bash")
    assert bash is not None, "docs workflow tests require bash"

    responses = tmp_path / "responses"
    responses.mkdir()
    for index, page in enumerate(pages, start=1):
        (responses / f"{index}.html").write_text(page, encoding="utf-8")
    (responses / "default.html").write_text(pages[-1], encoding="utf-8")

    calls = tmp_path / "curl-calls.txt"
    calls.write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        f"""#!{bash}
set -euo pipefail
call_number=$(( $(wc -l <"$STUB_CURL_CALLS") + 1 ))
printf '%s\n' "$*" >>"$STUB_CURL_CALLS"
output=''
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--output" ]]; then
    output="$2"
    break
  fi
  shift
done
[[ -n "$output" ]]
response="$STUB_RESPONSES/${{call_number}}.html"
[[ -f "$response" ]] || response="$STUB_RESPONSES/default.html"
cp "$response" "$output"
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text(f"#!{bash}\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)

    return subprocess.run(
        [bash, "-c", script],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PAGE_URL": "https://docs.example.test/mindroom/",
            "STUB_CURL_CALLS": str(calls),
            "STUB_RESPONSES": str(responses),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _curl_calls(tmp_path: Path) -> list[str]:
    """Return the recorded curl argument lists."""
    return (tmp_path / "curl-calls.txt").read_text(encoding="utf-8").splitlines()


def test_docs_smoke_waits_for_the_deployed_content(
    docs_workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A stale Pages response must be retried until the new scheduling docs are served."""
    job, step = _smoke_step(docs_workflow)
    deploy = docs_workflow["jobs"]["deploy"]
    assert deploy["outputs"]["page_url"] == "${{ steps.deployment.outputs.page_url }}"
    assert job["needs"] == "deploy"
    assert job["if"] == "github.event_name != 'pull_request'"
    assert step["env"]["PAGE_URL"] == "${{ needs.deploy.outputs.page_url }}"

    result = _run_smoke_script(
        step["run"],
        ["<h1>Scheduling</h1>", "<h2>Silent Delivery</h2>"],
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "https://docs.example.test/mindroom/scheduling/" in result.stdout
    calls = _curl_calls(tmp_path)
    assert len(calls) == 2
    assert all("https://docs.example.test/mindroom/scheduling/" in call.split() for call in calls)


def test_docs_smoke_fails_when_deployed_content_never_appears(
    docs_workflow: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A deployment that never serves the expected section must fail CI after retrying."""
    _, step = _smoke_step(docs_workflow)

    result = _run_smoke_script(step["run"], ["<h1>Scheduling</h1>"], tmp_path)

    assert result.returncode != 0
    assert "Silent Delivery" in result.stderr
    calls = _curl_calls(tmp_path)
    assert len(calls) == 12
    assert all("https://docs.example.test/mindroom/scheduling/" in call.split() for call in calls)
