"""Check CI workflow policies: least-privilege tokens, pinned actions, and cancelling superseded pull request runs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
# Jobs that install or run repository or dependency code chosen by the pushed commit.
_CODE_RUNNING_JOBS = (
    ("docs.yml", "build"),
    ("macos-tests.yml", "packaging"),
    ("macos-tests.yml", "swift-tests"),
    ("markdown-code-runner.yml", "markdown-code-runner"),
    ("pytest.yml", "test"),
    ("security-scan.yml", "scan"),
    ("smoke-stacks.yml", "smoke"),
    ("tach.yml", "check"),
)


def _load_workflow(name: str) -> dict[str, Any]:
    workflow = yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
    if True in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


def _job_permissions(workflow: dict[str, Any], job: str) -> dict[str, str]:
    """Return the effective token permissions of one job, failing when it falls back to the repository default."""
    permissions = workflow["jobs"][job].get("permissions", workflow.get("permissions"))
    assert isinstance(permissions, dict), "the job must not inherit the repository-default token scope"
    return permissions


@pytest.mark.parametrize("workflow_path", sorted(WORKFLOW_DIR.glob("*.yml")), ids=lambda path: path.name)
def test_every_workflow_job_declares_token_permissions(workflow_path: Path) -> None:
    """A job without a permissions block would get whatever the repository default grants."""
    workflow = _load_workflow(workflow_path.name)

    for job in workflow["jobs"]:
        _job_permissions(workflow, job)


@pytest.mark.parametrize(("workflow_name", "job"), _CODE_RUNNING_JOBS)
def test_code_running_jobs_hold_no_write_scope_or_persisted_token(workflow_name: str, job: str) -> None:
    """Dependency code these jobs install must find neither a write token nor the checkout credential on disk."""
    workflow = _load_workflow(workflow_name)
    permissions = _job_permissions(workflow, job)
    checkouts = [
        step for step in workflow["jobs"][job]["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")
    ]

    assert "write" not in permissions.values()
    assert checkouts
    assert all(step.get("with", {}).get("persist-credentials") is False for step in checkouts)


def test_generated_docs_are_checked_rather_than_pushed_from_ci() -> None:
    """Generated Markdown can run code, so CI reports stale docs instead of committing what dependency code produced."""
    workflow = _load_workflow("markdown-code-runner.yml")
    steps = workflow["jobs"]["markdown-code-runner"]["steps"]

    assert list(workflow["jobs"]) == ["markdown-code-runner"]
    assert not any("git push" in step.get("run", "") or "git commit" in step.get("run", "") for step in steps)
    assert not any("push-action" in step.get("uses", "") or "artifact" in step.get("uses", "") for step in steps)
    assert "exit 1" in steps[-1]["run"]


@pytest.mark.parametrize("name", ["release.yml", "markdown-code-runner.yml"])
def test_release_and_docs_workflows_pin_actions_to_commits(name: str) -> None:
    """A moved upstream tag or branch must not change the action code these workflows run, signing steps included."""
    workflow = _load_workflow(name)
    actions = [step["uses"] for job in workflow["jobs"].values() for step in job["steps"] if "uses" in step]

    assert actions
    assert all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action) for action in actions), actions


def test_security_scan_pins_the_tools_it_installs() -> None:
    """The scan tool itself is pinned, so a new release cannot change it silently; its dependencies still float."""
    steps = _load_workflow("security-scan.yml")["jobs"]["scan"]["steps"]
    installs = [step["run"] for step in steps if step.get("run", "").startswith("pip install")]

    assert installs == ["pip install pip-audit==2.10.1"]


def test_docs_workflow_grants_pages_deployment_only_to_the_deploy_job() -> None:
    """The pull-request build job must not be able to mint an OIDC token or publish Pages."""
    workflow = _load_workflow("docs.yml")

    assert workflow["permissions"] == {"contents": "read"}
    assert _job_permissions(workflow, "deploy") == {"contents": "read", "pages": "write", "id-token": "write"}


# Pull request runs share one group per ref; every other event gets its own group, so it is never cancelled.
PULL_REQUEST_CONCURRENCY = {
    "group": "${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}",
    "cancel-in-progress": True,
}
# Workflows that deliberately keep their own older concurrency policy.
_OWN_CONCURRENCY_POLICY = {"docs.yml", "plugin-fleet.yml"}
_PULL_REQUEST_WORKFLOWS = sorted(
    path.name
    for path in WORKFLOW_DIR.glob("*.yml")
    if path.name not in _OWN_CONCURRENCY_POLICY and "pull_request" in _load_workflow(path.name)["on"]
)


@pytest.mark.parametrize("name", _PULL_REQUEST_WORKFLOWS)
def test_pull_request_workflows_cancel_only_superseded_pull_request_runs(name: str) -> None:
    """A newer push to a pull request cancels its older runs, and no other event ever shares their group."""
    assert _load_workflow(name).get("concurrency") == PULL_REQUEST_CONCURRENCY
