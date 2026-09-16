"""Usage API authentication and retained-storage integration."""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle, main


@pytest.mark.parametrize("include_daily", [None, False, True])
def test_usage_endpoint_reads_retained_tokens_and_requires_dashboard_auth(
    temp_config_file: Path,
    tmp_path: Path,
    include_daily: bool | None,
) -> None:
    """Missing auth must not expose usage; valid auth reads real retained sessions."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=temp_config_file,
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_API_KEY": "test-usage-key"},
    )
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, main.app)
    database = runtime_paths.storage_root / "agents/test_agent/sessions/test_agent.db"
    database.parent.mkdir(parents=True)
    metrics = {
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "cache_read_tokens": 9,
        "cache_write_tokens": 3,
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE test_agent_sessions (session_id TEXT, session_type TEXT, agent_id TEXT, "
            "team_id TEXT, user_id TEXT, session_data TEXT, runs TEXT)",
        )
        connection.execute(
            "INSERT INTO test_agent_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "session",
                "agent",
                "test_agent",
                None,
                "@alice:example.org",
                json.dumps({"session_metrics": metrics}),
                json.dumps(
                    [
                        {
                            "run_id": "run",
                            "model": "test-model",
                            "model_provider": "ollama",
                            "created_at": 1_700_000_000,
                            "metrics": {
                                **metrics,
                                "details": {
                                    "model": [
                                        {
                                            "id": "test-model",
                                            "provider": "ollama",
                                            "input_tokens": 10,
                                            "output_tokens": 5,
                                            "total_tokens": 15,
                                            "cache_read_tokens": 7,
                                            "cache_write_tokens": 2,
                                        },
                                    ],
                                    "output_model": [
                                        {
                                            "id": "other-model",
                                            "provider": "ollama",
                                            "input_tokens": 2,
                                            "output_tokens": 3,
                                            "total_tokens": 5,
                                            "cache_read_tokens": 2,
                                            "cache_write_tokens": 1,
                                        },
                                    ],
                                },
                            },
                            "content": "private message",
                            "messages": [{"content": "private prompt"}],
                        },
                    ],
                ),
            ),
        )
    client = TestClient(main.app)
    params = {} if include_daily is None else {"include_daily": str(include_daily).lower()}
    assert client.get("/api/usage", params=params).status_code == 401
    assert client.get("/api/usage", params=params, headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/api/usage", params=params, headers={"Authorization": "Bearer test-usage-key"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["totals"]["total_tokens"] == 20
    assert payload["user_breakdown"][0]["user_id"] == "@alice:example.org"
    assert payload["user_breakdown"][0]["run_count"] == 1
    assert payload["user_breakdown"][0]["model_breakdown"] == payload["model_breakdown"]
    assert [
        (
            row["model"],
            row["totals"]["input_tokens"],
            row["totals"]["output_tokens"],
            row["totals"]["cache_read_tokens"],
            row["totals"]["cache_write_tokens"],
        )
        for row in payload["model_breakdown"]
    ] == [
        ("test-model", 10, 5, 7, 2),
        ("other-model", 2, 3, 2, 1),
    ]
    if include_daily:
        day = payload["daily_breakdown"][0]
        assert day["date"] == "2023-11-14"
        assert day["run_count"] == 1
        assert day["model_breakdown"] == payload["model_breakdown"]
        assert {key: day["totals"][key] for key in metrics} == metrics
        assert "daily_coverage" in payload
    else:
        assert "daily_breakdown" not in payload
        assert "daily_coverage" not in payload
    assert "private message" not in response.text
    assert "private prompt" not in response.text
    assert str(database) not in response.text
