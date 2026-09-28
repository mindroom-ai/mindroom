"""Check that CI jobs which run repository and dependency code hold only the token scopes they use."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
# Jobs that install or run code chosen by the pull request, mapped to the only write scopes they may hold.
_CODE_RUNNING_JOBS = {
    ("pytest.yml", "test"): {},
    ("tach.yml", "check"): {},
    ("security-scan.yml", "scan"): {},
    ("docs.yml", "build"): {},
    ("markdown-code-runner.yml", "markdown-code-runner"): {"contents": "write"},
}


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


@pytest.mark.parametrize(("workflow_name", "job"), sorted(_CODE_RUNNING_JOBS))
def test_code_running_jobs_hold_no_extra_write_scope_or_persisted_token(workflow_name: str, job: str) -> None:
    """Dependency code these jobs install must find neither a write token nor the checkout credential on disk."""
    workflow = _load_workflow(workflow_name)
    permissions = _job_permissions(workflow, job)
    checkouts = [
        step for step in workflow["jobs"][job]["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")
    ]

    assert {scope: level for scope, level in permissions.items() if level == "write"} == _CODE_RUNNING_JOBS[
        (workflow_name, job)
    ]
    assert checkouts
    assert all(step.get("with", {}).get("persist-credentials") is False for step in checkouts)


def test_docs_workflow_grants_pages_deployment_only_to_the_deploy_job() -> None:
    """The pull-request build job must not be able to mint an OIDC token or publish Pages."""
    workflow = _load_workflow("docs.yml")

    assert workflow["permissions"] == {"contents": "read"}
    assert _job_permissions(workflow, "deploy") == {"contents": "read", "pages": "write", "id-token": "write"}
