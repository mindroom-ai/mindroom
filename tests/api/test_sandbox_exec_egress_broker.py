"""Tests for how sandbox_exec applies the native broker env a worker call carries."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import pytest

from mindroom.api import sandbox_exec
from mindroom.constants import resolve_runtime_paths
from mindroom.egress_broker.env import BROKER_CA_PEM_ENV, broker_execution_env


@pytest.mark.parametrize("tool_name", ["shell", "python"])
def test_broker_env_keeps_its_proxy_and_materializes_its_ca(tmp_path: Path, test_ca_pem: str, tool_name: str) -> None:
    """The broker's proxy reaches the call, and its CA PEM becomes bundle files the CA env vars point at."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    broker_env = broker_execution_env(
        broker_url="http://broker:8768",
        token="mrb1.token",  # noqa: S106
        ca_pem=test_ca_pem,
        placeholder_env={},
        extra_no_proxy_hosts=[],
    )

    final_env = sandbox_exec.request_execution_env(tool_name, broker_env, runtime_paths)

    assert final_env["HTTPS_PROXY"] == broker_env["HTTPS_PROXY"]
    assert BROKER_CA_PEM_ENV not in final_env
    assert "SSL_CERT_FILE" in final_env
    assert "REQUESTS_CA_BUNDLE" in final_env
    assert "NODE_EXTRA_CA_CERTS" in final_env


def test_non_execution_tools_get_no_request_env(tmp_path: Path, test_ca_pem: str) -> None:
    """Only `shell` and `python` get an execution env, even when the call carries broker env."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})
    broker_env = broker_execution_env(
        broker_url="http://broker:8768",
        token="mrb1.token",  # noqa: S106
        ca_pem=test_ca_pem,
        placeholder_env={},
        extra_no_proxy_hosts=[],
    )

    assert sandbox_exec.request_execution_env("website", broker_env, runtime_paths) == {}


def test_request_env_without_broker_ca_is_kept_as_sent(tmp_path: Path) -> None:
    """Without a broker CA the call's env is applied unchanged, with no proxy or CA bundle added."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})

    final_env = sandbox_exec.request_execution_env("shell", {"KEEP": "1"}, runtime_paths)

    assert final_env == {"KEEP": "1"}
