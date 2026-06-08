"""API tests for Dynamic Workflow reports."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from mindroom.api import main
from mindroom.api.dynamic_workflows import _authorize_private_report_request
from mindroom.dynamic_workflows.service import DynamicWorkflowService
from mindroom.dynamic_workflows.store import DynamicWorkflowStore
from tests.api.conftest import trusted_upstream_headers, use_trusted_upstream_runtime

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
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )
    run = service.run_workflow(
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
    runtime_paths = main._app_runtime_paths(test_client.app)

    response = test_client.get("/reports/private/agent/general/competitor-research-report/run_missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "Private Dynamic Workflow report was not found."
    assert str(runtime_paths.storage_root) not in response.text


def test_private_dynamic_workflow_report_rejects_other_trusted_upstream_user(test_client: TestClient) -> None:
    """Private report URLs should not leak reports across authenticated hosted users."""
    runtime_paths = use_trusted_upstream_runtime(test_client.app)
    store = DynamicWorkflowStore(runtime_paths.storage_root)
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )
    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="@alice:example.org",
        base_url="https://acme.mindroom.chat",
    )

    response = test_client.get(
        f"/reports/private/agent/general/competitor-research-report/{run.run_id}",
        headers=trusted_upstream_headers(
            user_id="bob",
            email="bob@example.com",
            matrix_user_id="@bob:example.org",
        ),
    )

    assert response.status_code == 403


def test_private_dynamic_workflow_report_rejects_access_token_for_other_hosted_user(test_client: TestClient) -> None:
    """Private report URLs should not become bearer links across hosted users."""
    runtime_paths = use_trusted_upstream_runtime(test_client.app)
    store = DynamicWorkflowStore(runtime_paths.storage_root)
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )
    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="@alice:example.org",
        base_url="https://acme.mindroom.chat",
    )

    response = test_client.get(
        f"/reports/private/agent/general/competitor-research-report/{run.run_id}",
        params={"access_token": "leaked-token"},
        headers=trusted_upstream_headers(
            user_id="user-bob",
            email="bob@example.com",
            matrix_user_id="",
        ),
    )

    assert response.status_code == 403


def test_private_dynamic_workflow_report_accepts_matching_trusted_upstream_user(test_client: TestClient) -> None:
    """Private report auth should allow the Matrix requester that started the run."""
    runtime_paths = use_trusted_upstream_runtime(test_client.app)
    store = DynamicWorkflowStore(runtime_paths.storage_root)
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )
    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="@alice:example.org",
        base_url="https://acme.mindroom.chat",
    )

    response = test_client.get(
        f"/reports/private/agent/general/competitor-research-report/{run.run_id}",
        headers=trusted_upstream_headers(
            user_id="user-alice",
            email="alice@example.com",
            matrix_user_id="@alice:example.org",
        ),
    )

    assert response.status_code == 200
    assert "Agno factories" in response.text


def test_private_dynamic_workflow_report_rejects_other_platform_user(test_client: TestClient) -> None:
    """Private report auth should deny Supabase users that do not match the run requester."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    store = DynamicWorkflowStore(runtime_paths.storage_root)
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )
    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="user-alice",
        base_url="https://acme.mindroom.chat",
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/reports/private/agent/general/competitor-research-report/{run.run_id}",
            "query_string": b"",
            "headers": [],
            "auth_user": {"user_id": "user-bob", "email": "bob@example.com"},
        },
    )

    with pytest.raises(HTTPException) as exc_info:
        _authorize_private_report_request(request, run)

    assert exc_info.value.status_code == 403


def test_private_dynamic_workflow_report_rejects_unscoped_run_id(test_client: TestClient) -> None:
    """Private reports should not be served through global run-id lookup."""
    response = test_client.get("/reports/private/run_missing")

    assert response.status_code == 404
