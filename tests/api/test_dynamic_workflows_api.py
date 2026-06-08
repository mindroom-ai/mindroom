"""API tests for Dynamic Workflow reports."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.api import main
from mindroom.dynamic_workflows.store import DynamicWorkflowStore

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def _workflow_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "competitor-research-report",
        "name": "Competitor Research Report",
        "description": "Create a cited HTML report about competitors.",
        "kind": "workflow",
        "participants": [{"id": "writer", "kind": "ephemeral_agent", "name": "Report Writer"}],
        "workflow": [
            {
                "id": "write",
                "type": "transform_step",
                "template": "Report about {input.topic}.",
            },
        ],
        "outputs": [{"id": "report_html", "type": "html_report", "from_step": "write"}],
    }


def test_private_dynamic_workflow_report_served_from_runtime_storage(test_client: TestClient) -> None:
    """Private report URLs should serve Dynamic Workflow HTML artifacts."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    store = DynamicWorkflowStore(runtime_paths.storage_root)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )
    run = store.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
    )

    response = test_client.get(f"/reports/private/agent/general/competitor-research-report/{run.run_id}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; "
        "img-src 'self' data: https:; "
        "style-src 'unsafe-inline'; "
        "font-src 'self' data:; "
        "base-uri 'none'; "
        "frame-ancestors 'self'"
    )
    assert "Competitor Research Report" in response.text
    assert "Agno factories" in response.text


def test_private_dynamic_workflow_report_returns_404_for_unknown_run_id(test_client: TestClient) -> None:
    """Private report URLs should return 404 for unknown Dynamic Workflow runs."""
    response = test_client.get("/reports/private/agent/general/competitor-research-report/run_missing")

    assert response.status_code == 404


def test_private_dynamic_workflow_report_rejects_unscoped_run_id(test_client: TestClient) -> None:
    """Private reports should not be served through global run-id lookup."""
    response = test_client.get("/reports/private/run_missing")

    assert response.status_code == 404
