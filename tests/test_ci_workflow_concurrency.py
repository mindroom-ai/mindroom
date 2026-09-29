"""Check that superseded pull request runs are cancelled without ever touching main, schedule, or release runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
# Pull request runs share one group per ref; every other event gets its own group, so it is never cancelled.
PULL_REQUEST_CONCURRENCY = {
    "group": "${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}",
    "cancel-in-progress": True,
}
# Workflows that deliberately keep their own older concurrency policy.
_OWN_CONCURRENCY_POLICY = {"docs.yml", "plugin-fleet.yml"}


def _load_workflow(path: Path) -> dict[str, Any]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


@pytest.mark.parametrize("path", sorted(WORKFLOW_DIR.glob("*.yml")), ids=lambda path: path.name)
def test_pull_request_workflows_cancel_only_superseded_pull_request_runs(path: Path) -> None:
    """A newer push to a pull request cancels its older runs, and no other event ever shares their group."""
    workflow = _load_workflow(path)
    if "pull_request" not in workflow["on"] or path.name in _OWN_CONCURRENCY_POLICY:
        return
    assert workflow.get("concurrency") == PULL_REQUEST_CONCURRENCY
