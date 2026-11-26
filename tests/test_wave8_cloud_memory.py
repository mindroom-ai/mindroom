"""Tests for Wave 8 Batch 4 native cloud, orchestration & memory toolkits."""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tools.airflow import AirflowTools
from mindroom.tools.aws_lambda import AWSLambdaTools
from mindroom.tools.mem0 import Mem0Tools
from mindroom.tools.zep import ZepTools

if TYPE_CHECKING:
    from pathlib import Path


def test_batch4_action_scoped_grants_and_toggles() -> None:
    """Batch 4 toolkits declare per-toolkit action_scoped_grants and honor explicit None toggles."""
    registry = builtin_tool_registry()
    assert registry.entries["aws_lambda"].action_scoped_grants == ()
    assert registry.entries["airflow"].action_scoped_grants == ()
    assert registry.entries["composio"].action_scoped_grants == ()
    assert registry.entries["mem0"].action_scoped_grants == ("mem0_read",)
    assert registry.entries["zep"].action_scoped_grants == ("zep_read",)

    assert list(AirflowTools(enable_save_dag_file=None, enable_read_dag_file=None).functions) == []
    assert (
        list(
            Mem0Tools(
                enable_add_memory=None,
                enable_search_memory=None,
                enable_get_all_memories=None,
                enable_delete_all_memories=None,
            ).functions,
        )
        == []
    )
    assert (
        list(
            ZepTools(
                api_key="zep-key",
                enable_add_zep_message=None,
                enable_get_zep_memory=None,
                enable_search_zep_memory=None,
            ).functions,
        )
        == []
    )


def test_aws_lambda_scoped_credentials_toggles_and_invoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AWS Lambda scopes credentials from runtime_paths, respects None toggles, and validates JSON payloads."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "host-ak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "host-sk")
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "AWS_ACCESS_KEY_ID": "scoped-ak",
            "AWS_SECRET_ACCESS_KEY": "scoped-sk",
            "AWS_SESSION_TOKEN": "scoped-st",
        },
    )
    fake_client = MagicMock()
    fake_client.list_functions.return_value = {"Functions": [{"FunctionName": "fn-a"}, {"FunctionName": "fn-b"}]}
    fake_client.invoke.return_value = {"StatusCode": 200, "Payload": io.BytesIO(b'{"ok":true}')}
    session_kwargs_seen: list[dict[str, object]] = []

    class _FakeSession:
        def __init__(self, **kwargs: object) -> None:
            session_kwargs_seen.append(kwargs)

        def client(self, service: str, *, region_name: str) -> MagicMock:
            assert service == "lambda"
            assert region_name == "eu-west-1"
            return fake_client

    with patch("boto3.session.Session", _FakeSession):
        disabled = AWSLambdaTools(
            region_name="eu-west-1",
            enable_list_functions=None,
            enable_invoke_function=None,
            runtime_paths=paths,
        )
        assert list(disabled.functions) == []

        toolkit = AWSLambdaTools(region_name="eu-west-1", runtime_paths=paths)

    assert session_kwargs_seen[0] == {
        "region_name": "eu-west-1",
        "aws_access_key_id": "scoped-ak",
        "aws_secret_access_key": "scoped-sk",
        "aws_session_token": "scoped-st",
    }
    assert "fn-a, fn-b" in toolkit.list_functions()
    assert '{"ok":true}' in toolkit.invoke_function("fn-a", '{"x": 1}')
    assert toolkit.invoke_function("fn-a", "not-json").startswith("Error invoking function:")


def test_zep_no_env_leak_lazy_init_and_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zep requires scoped api_key, stays offline at init, and uses zep-cloud 3.x APIs cleanly."""
    monkeypatch.setenv("ZEP_API_KEY", "host-zep-secret")
    with pytest.raises(ValueError, match="Zep API key"):
        ZepTools(api_key=None)

    fake_zep = MagicMock()
    fake_zep.thread.get_user_context.return_value = SimpleNamespace(context="User likes tea.")
    fake_zep.thread.get.return_value = SimpleNamespace(messages=[SimpleNamespace(role="user", content="Hi")])
    fake_zep.graph.search.return_value = SimpleNamespace(
        edges=[SimpleNamespace(fact="Prefers green tea")],
        nodes=[SimpleNamespace(name="Tea", summary="Beverage")],
    )

    with patch("zep_cloud.client.Zep", return_value=fake_zep) as zep_cls:
        toolkit = ZepTools(
            api_key="scoped-zep-key",
            user_id="alice",
            session_id="sess-1",
            ignore_assistant_messages=True,
        )
        # Lazy init: Zep client is not constructed during __init__.
        zep_cls.assert_not_called()

        added = toolkit.zep_add_message("user", "I love green tea")
        assert "added successfully" in added
        zep_cls.assert_called_once_with(api_key="scoped-zep-key")
        call_kwargs = fake_zep.thread.add_messages.call_args.kwargs
        assert call_kwargs["thread_id"] == "sess-1"
        assert call_kwargs["ignore_roles"] == ["assistant"]

        assert toolkit.zep_read(action="context") == "User likes tea."
        fake_zep.thread.get_user_context.assert_called_once_with(thread_id="sess-1")
        assert "Found 1 facts:" in toolkit.zep_read(action="search", query="tea", search_scope="edges")
        assert "Tea: Beverage" in toolkit.zep_read(action="search", query="tea", search_scope="nodes")

    # Disabled search sub-action is enforced cleanly.
    read_only_context = ZepTools(
        api_key="scoped-zep-key",
        user_id="alice",
        session_id="sess-1",
        enable_search_zep_memory=False,
    )
    read_only_context.zep_client = fake_zep
    read_only_context.user_id = "alice"
    read_only_context.session_id = "sess-1"
    read_only_context._initialized = True
    assert "disabled" in read_only_context.zep_read(action="search", query="tea")
