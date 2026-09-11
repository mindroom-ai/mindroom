"""Usage API authentication and retained-storage integration."""

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle, main


def test_usage_endpoint_reads_retained_tokens_and_requires_dashboard_auth(
    temp_config_file: Path, tmp_path: Path
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
    metrics = {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20, "cache_read_tokens": 9}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE test_agent_sessions (session_id TEXT, session_type TEXT, agent_id TEXT, "
            "team_id TEXT, user_id TEXT, session_data TEXT, runs TEXT)"
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
                            "metrics": metrics,
                            "content": "private message",
                            "messages": [{"content": "private prompt"}],
                        }
                    ]
                ),
            ),
        )
    client = TestClient(main.app)
    assert client.get("/api/usage").status_code == 401
    assert client.get("/api/usage", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/api/usage", headers={"Authorization": "Bearer test-usage-key"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["totals"]["total_tokens"] == 20
    assert payload["user_breakdown"][0]["user_id"] == "@alice:example.org"
    assert payload["user_breakdown"][0]["model_breakdown"][0]["totals"]["cache_read_tokens"] == 9
    assert "private message" not in response.text
    assert "private prompt" not in response.text
    assert str(database) not in response.text
