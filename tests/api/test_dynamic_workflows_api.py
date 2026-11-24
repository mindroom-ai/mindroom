"""API tests for Dynamic Workflow reports."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mindroom import constants
from mindroom.api import config_lifecycle, main
from mindroom.dynamic_workflows.service import DynamicWorkflowService
from mindroom.dynamic_workflows.spec import WorkflowSpec, parse_workflow_spec
from mindroom.dynamic_workflows.store import DynamicWorkflowStore
from tests.api.conftest import (
    set_platform_auth,
    trusted_upstream_env,
    trusted_upstream_headers,
    use_trusted_upstream_runtime,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mindroom.constants import RuntimePaths
    from mindroom.dynamic_workflows.store import DynamicWorkflowRun

# Shaped like the Supabase access token JWT the platform issues.
_PLATFORM_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEyMyJ9.c2lnbmF0dXJl"  # noqa: S105
_BROWSER_ACCEPT = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _workflow_spec() -> WorkflowSpec:
    return parse_workflow_spec(
        {
            "schema_version": 1,
            "id": "competitor-research-report",
            "name": "Competitor Research Report",
            "description": "Create a cited HTML report about competitors.",
            "kind": "workflow",
            "participants": [{"id": "writer", "kind": "subagent", "system_prompt": "You write reports."}],
            "workflow": [
                {
                    "id": "write",
                    "type": "transform_step",
                    "template": "Report about {input.topic}.",
                },
            ],
            "outputs": [{"id": "report_html", "type": "html_report", "from_step": "write"}],
        },
    )


def _completed_run(runtime_paths: RuntimePaths, *, requested_by: str) -> DynamicWorkflowRun:
    """Create the report workflow for agent `general` and run it once for `requested_by`."""
    store = DynamicWorkflowStore(runtime_paths.storage_root)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )
    return asyncio.run(
        DynamicWorkflowService(store).run_workflow(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            input_data={"topic": "Agno factories"},
            requested_by=requested_by,
            base_url="https://mindroom.example.com",
        ),
    )


def _use_process_env(api_app: FastAPI, process_env: dict[str, str]) -> RuntimePaths:
    """Reinitialize the app on its config and storage with `process_env`."""
    runtime_paths = main._app_runtime_paths(api_app)
    new_runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=runtime_paths.config_path,
        storage_path=runtime_paths.storage_root,
        process_env=process_env,
    )
    main.initialize_api_app(api_app, new_runtime_paths)
    return new_runtime_paths


def _use_administrator_only_dashboard(api_app: FastAPI) -> RuntimePaths:
    """Reinitialize the app with trusted upstream auth and `MINDROOM_CONNECTIONS_AGENT`, which limits the dashboard to administrators."""
    runtime_paths = _use_process_env(api_app, trusted_upstream_env() | {"MINDROOM_CONNECTIONS_AGENT": "test_agent"})
    config_lifecycle.load_config_into_app(runtime_paths, api_app)
    return runtime_paths


def _signed_headers(matrix_user_id: str) -> dict[str, str]:
    localpart = matrix_user_id.removeprefix("@").split(":")[0]
    return trusted_upstream_headers(
        user_id=f"user-{localpart}",
        email=f"{localpart}@example.com",
        matrix_user_id=matrix_user_id,
    )


def test_private_dynamic_workflow_report_served_from_runtime_storage(test_client: TestClient) -> None:
    """Private report URLs should serve Dynamic Workflow HTML artifacts."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    # The standalone dashboard login acts as the configured owner, who asked for this run in Matrix.
    run = _completed_run(runtime_paths, requested_by="@owner:example.org")

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
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"
    assert "Competitor Research Report" in response.text
    assert "Agno factories" in response.text


def test_private_dynamic_workflow_report_rejects_standalone_user_for_matrix_requested_run(
    test_client: TestClient,
) -> None:
    """Standalone dashboard auth should not be a wildcard for Matrix-requested reports."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    run = _completed_run(runtime_paths, requested_by="@alice:example.org")

    response = test_client.get(f"/reports/private/agent/general/competitor-research-report/{run.run_id}")

    assert response.status_code == 403


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
    run = _completed_run(runtime_paths, requested_by="@alice:example.org")

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
    run = _completed_run(runtime_paths, requested_by="@alice:example.org")

    response = test_client.get(
        f"/reports/private/agent/general/competitor-research-report/{run.run_id}",
        params={"access_token": "leaked-token"},
        headers=trusted_upstream_headers(
            user_id="user-bob",
            email="bob@example.com",
            matrix_user_id="@bob:example.org",
        ),
    )

    assert response.status_code == 403


def test_private_dynamic_workflow_report_accepts_gateway_user_id_that_is_the_matrix_id(
    test_client: TestClient,
) -> None:
    """A gateway may sign the Matrix ID as its user-ID claim without a Matrix claim; that login still opens its report."""
    runtime_paths = _use_process_env(test_client.app, trusted_upstream_env(matrix_user_id_claim=None))
    run = _completed_run(runtime_paths, requested_by="@alice:example.org")

    response = test_client.get(
        f"/reports/private/agent/general/competitor-research-report/{run.run_id}",
        headers=trusted_upstream_headers(user_id="@alice:example.org"),
    )

    assert response.status_code == 200, response.text


def test_private_dynamic_workflow_report_refuses_a_gateway_user_named_like_the_standalone_login(
    test_client: TestClient,
) -> None:
    """A gateway user whose user-ID claim is literally `standalone` is not the standalone operator."""
    runtime_paths = _use_process_env(test_client.app, trusted_upstream_env(matrix_user_id_claim=None))
    run = _completed_run(runtime_paths, requested_by="standalone")

    response = test_client.get(
        f"/reports/private/agent/general/competitor-research-report/{run.run_id}",
        headers=trusted_upstream_headers(user_id="standalone"),
    )

    assert response.status_code == 403


def test_private_dynamic_workflow_report_accepts_matching_trusted_upstream_user(test_client: TestClient) -> None:
    """Private report auth should allow the Matrix requester that started the run."""
    runtime_paths = use_trusted_upstream_runtime(test_client.app)
    run = _completed_run(runtime_paths, requested_by="@alice:example.org")

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


def test_private_dynamic_workflow_report_accepts_derived_trusted_upstream_matrix_user(test_client: TestClient) -> None:
    """Trusted upstream auth can use an email-derived Matrix identity to read a Matrix-requested report."""
    runtime_paths = _use_process_env(
        test_client.app,
        trusted_upstream_env(matrix_user_id_claim=None)
        | {
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE": "@{localpart}:example.org",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN": "example.com",
        },
    )
    run = _completed_run(runtime_paths, requested_by="@alice:example.org")

    response = test_client.get(
        f"/reports/private/agent/general/competitor-research-report/{run.run_id}",
        # Without a Matrix claim, the email names the requester.
        headers=trusted_upstream_headers(user_id="user-alice", matrix_user_id="@ignored:example.org"),
    )

    assert response.status_code == 200
    assert "Agno factories" in response.text


@pytest.mark.parametrize("accept", [_BROWSER_ACCEPT, {}], ids=["browser", "api"])
@pytest.mark.parametrize("requester", ["@alice:example.org", "@owner:example.org"], ids=["user", "administrator"])
def test_private_dynamic_workflow_report_opens_for_requester_when_dashboard_is_administrator_only(
    test_client: TestClient,
    requester: str,
    accept: dict[str, str],
) -> None:
    """The requester opens their own report whether or not `administrators` lists them."""
    runtime_paths = _use_administrator_only_dashboard(test_client.app)
    run = _completed_run(runtime_paths, requested_by=requester)

    response = test_client.get(
        f"/reports/private/agent/general/competitor-research-report/{run.run_id}",
        headers=_signed_headers(requester) | accept,
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "Agno factories" in response.text


@pytest.mark.parametrize("accept", [_BROWSER_ACCEPT, {}], ids=["browser", "api"])
@pytest.mark.parametrize("viewer", ["@bob:example.org", "@owner:example.org"], ids=["user", "administrator"])
def test_private_dynamic_workflow_report_rejects_other_user_when_dashboard_is_administrator_only(
    test_client: TestClient,
    viewer: str,
    accept: dict[str, str],
) -> None:
    """Neither another user nor an administrator opens someone else's report."""
    runtime_paths = _use_administrator_only_dashboard(test_client.app)
    run = _completed_run(runtime_paths, requested_by="@alice:example.org")

    response = test_client.get(
        f"/reports/private/agent/general/competitor-research-report/{run.run_id}",
        headers=_signed_headers(viewer) | accept,
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Private Dynamic Workflow report access denied."


def test_private_dynamic_workflow_report_requires_sign_in_when_dashboard_is_administrator_only(
    test_client: TestClient,
) -> None:
    """A request without the gateway's signed identity gets no report."""
    runtime_paths = _use_administrator_only_dashboard(test_client.app)
    run = _completed_run(runtime_paths, requested_by="@alice:example.org")

    response = test_client.get(
        f"/reports/private/agent/general/competitor-research-report/{run.run_id}",
        headers=_BROWSER_ACCEPT,
        follow_redirects=False,
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("requested_by", "expected_status"),
    [("@owner:example.org", 200), ("@alice:example.org", 403)],
)
def test_private_dynamic_workflow_report_admits_the_platform_owner_only(
    test_client: TestClient,
    requested_by: str,
    expected_status: int,
) -> None:
    """A hosted platform login acts as the instance owner, so it opens the owner's reports and no one else's."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    set_platform_auth(valid_tokens={_PLATFORM_TOKEN}, account_id="user-123")
    run = _completed_run(runtime_paths, requested_by=requested_by)

    response = test_client.get(
        f"/reports/private/agent/general/competitor-research-report/{run.run_id}",
        headers={"Authorization": f"Bearer {_PLATFORM_TOKEN}"},
        follow_redirects=False,
    )

    assert response.status_code == expected_status, response.text
