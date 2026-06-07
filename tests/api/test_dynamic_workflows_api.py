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
        "workflow": [{"id": "write", "type": "agent_step", "participant": "writer"}],
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

    response = test_client.get(f"/reports/private/{run.run_id}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Competitor Research Report" in response.text
    assert "Agno factories" in response.text
