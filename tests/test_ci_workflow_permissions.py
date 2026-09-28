"""Check that CI jobs which run repository and dependency code hold only the token scopes they use."""

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


def test_docs_push_job_runs_no_repository_code_with_its_write_token() -> None:
    """The generated docs arrive as a patch, and the pushing job only applies, checks, commits, and pushes it."""
    workflow = _load_workflow("markdown-code-runner.yml")
    push_job = workflow["jobs"]["push-docs"]
    actions = [step["uses"].split("@")[0] for step in push_job["steps"] if "uses" in step]
    commands = [
        match.group(1)
        for step in push_job["steps"]
        for line in step.get("run", "").splitlines()
        if (match := re.match(r"\s*([^\s;]+)", line))
    ]

    assert push_job["needs"] == "markdown-code-runner"
    assert _job_permissions(workflow, "push-docs") == {"contents": "write"}
    assert actions == ["actions/checkout", "actions/download-artifact"]
    assert set(commands) <= {"git", "if", "echo", "exit", "fi"}


def test_docs_workflow_grants_pages_deployment_only_to_the_deploy_job() -> None:
    """The pull-request build job must not be able to mint an OIDC token or publish Pages."""
    workflow = _load_workflow("docs.yml")

    assert workflow["permissions"] == {"contents": "read"}
    assert _job_permissions(workflow, "deploy") == {"contents": "read", "pages": "write", "id-token": "write"}
