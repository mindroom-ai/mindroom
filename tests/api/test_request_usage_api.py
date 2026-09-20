"""Request detail stays opt-in behind existing organization authentication."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.metrics import RunMetrics
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession

from mindroom.agent_storage import create_state_storage, save_independent_usage
from mindroom.api import config_lifecycle
from tests.api.test_private_usage_api import _client as _private_client
from tests.api.test_usage_service_auth import (
    _initialize_usage_runtime,
    _install_manual_export_runner,
    _service_assertion,
    usage_service_auth,  # noqa: F401
)
from tests.test_request_usage import _run

if TYPE_CHECKING:
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric import rsa

    from mindroom.usage_storage import IndependentUsageKind


@pytest.mark.parametrize("route", ["/api/usage", "/api/usage/export"])
def test_request_export_flag_authentication_and_cache_variants(
    temp_config_file: Path,
    tmp_path: Path,
    usage_service_auth: tuple[dict[str, str], rsa.RSAPrivateKey],  # noqa: F811
    route: str,
) -> None:
    """Each daily/request variant keeps its own cached result while sharing one scan slot."""
    env, key = usage_service_auth
    if route == "/api/usage":
        env = {"MINDROOM_API_KEY": "test-usage-key"}
        headers = {"Authorization": "Bearer test-usage-key"}
    else:
        headers = {"Cf-Access-Jwt-Assertion": _service_assertion(key)}
    client, storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, env)
    storage = create_state_storage(
        "test_agent",
        storage_root / "agents/test_agent",
        subdir="sessions",
        session_table="test_agent_sessions",
    )
    run = _run()
    run.agent_id = "test_agent"
    assert run.metrics is not None
    storage.upsert_session(
        AgentSession(
            session_id="session",
            agent_id="test_agent",
            session_data={"session_metrics": run.metrics.to_dict()},
        ),
    )
    storage.upsert_run(run, session_id="session")
    storage.close()
    runner, workers = _install_manual_export_runner(client)
    variants = [
        {"include_daily": "false", "include_requests": "false"},
        {"include_daily": "false", "include_requests": "true"},
        {"include_daily": "true", "include_requests": "false"},
        {"include_daily": "true", "include_requests": "true"},
    ]
    payloads = []
    try:
        for params in variants:
            assert client.get(route, params=params).status_code == 401
            assert client.get(route, headers=headers, params=params).status_code == 202
            for other in variants:
                client.get(route, headers=headers, params=other)
            assert len(workers.targets) == 1
            workers.run_next()
            response = client.get(route, headers=headers, params=params)
            assert response.status_code == 200
            payload = response.json()
            payloads.append(payload)
            assert payload["schema_version"] == 1
            assert ("request_breakdown" in payload) is (params["include_requests"] == "true")
            assert ("daily_breakdown" in payload) is (params["include_daily"] == "true")
            if params["include_requests"] == "true":
                assert [entry["totals"]["input_tokens"] for entry in payload["request_breakdown"]] == [120_000, 130_000]
                assert all(entry["entity"] == "test_agent" for entry in payload["request_breakdown"])
        for params, payload in zip(variants, payloads, strict=True):
            assert client.get(route, params=params).status_code == 401
            assert client.get(route, headers=headers, params=params).json() == payload
        assert workers.targets == []
    finally:
        runner.close()
        config_lifecycle.app_state(client.app).usage_export_runner = None


@pytest.mark.parametrize("kind", ["memory_auto_flush", "dynamic_workflow"])
def test_helper_usage_exports_once_without_counting_another_reply(
    temp_config_file: Path,
    tmp_path: Path,
    kind: IndependentUsageKind,
) -> None:
    """The authenticated API must include independent helper usage from the real SQLite store."""
    client, storage_root = _initialize_usage_runtime(temp_config_file, tmp_path, {"MINDROOM_API_KEY": "test-usage-key"})
    storage = create_state_storage(
        "test_agent",
        storage_root / "agents/test_agent",
        subdir="sessions",
        session_table="test_agent_sessions",
    )
    run = RunOutput(
        run_id="ordinary",
        model="test-model",
        model_provider="test-provider",
        metrics=RunMetrics(input_tokens=10, output_tokens=1, total_tokens=11),
    )
    assert run.metrics is not None
    storage.upsert_session(
        AgentSession(
            session_id="session",
            agent_id="test_agent",
            session_data={"session_metrics": run.metrics.to_dict()},
        ),
    )
    storage.upsert_run(run, session_id="session")
    helper = {
        "model": "helper-model",
        "model_provider": "test-provider",
        "created_at": 1_700_000_000,
        "metrics": {"input_tokens": 20, "output_tokens": 2, "total_tokens": 22},
        "messages": [
            {
                "role": "assistant",
                "content": "Sample response.",
                "created_at": 1_700_000_000,
                "metrics": {"input_tokens": 20, "output_tokens": 2, "total_tokens": 22},
            },
        ],
    }
    try:
        for _ in range(2):
            save_independent_usage(
                storage,
                session_id="session",
                usage_id="helper",
                kind=kind,
                requester_id=None,
                run=helper,
            )
    finally:
        storage.close()
    runner, workers = _install_manual_export_runner(client)
    headers = {"Authorization": "Bearer test-usage-key"}
    try:
        assert client.get("/api/usage", params={"include_requests": "true"}).status_code == 401
        assert client.get("/api/usage", headers=headers, params={"include_requests": "true"}).status_code == 202
        workers.run_next()
        response = client.get("/api/usage", headers=headers, params={"include_requests": "true"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["totals"]["total_tokens"] == 33
        assert sum(row["run_count"] for row in payload["model_breakdown"]) == 1
        assert len(payload["request_breakdown"]) == 1
        assert payload["request_breakdown"][0]["kind"] == kind
        assert payload["request_breakdown"][0]["totals"]["total_tokens"] == 22
        assert "Sample response" not in response.text
    finally:
        runner.close()
        config_lifecycle.app_state(client.app).usage_export_runner = None


def test_private_usage_rejects_request_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Personal usage cannot opt into the organization request export."""
    client, headers = _private_client(tmp_path, monkeypatch)
    response = client.get(
        "/api/usage/me/private-agents",
        headers=headers["alice"],
        params={"include_requests": "true"},
    )
    assert response.status_code == 400
    default = client.get("/api/usage/me/private-agents", headers=headers["alice"])
    assert default.status_code == 200
    assert "request_breakdown" not in default.json()
    assert "request_coverage" not in default.json()
