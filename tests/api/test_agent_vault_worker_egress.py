"""Worker-side composition of Agent Vault proxy env for python/shell."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.api import sandbox_exec
from mindroom.constants import resolve_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_paths_with_vault(tmp_path: Path, *, with_ca: bool = True) -> tuple[object, str]:
    token_path = tmp_path / "token"
    token_path.write_text("av_sess_worker_token\n", encoding="utf-8")
    env = {
        "MINDROOM_AGENT_VAULT_PROXY_URL": "http://agent-vault:14322",
        "MINDROOM_AGENT_VAULT_TOKEN_FILE": str(token_path),
    }
    if with_ca:
        env["MINDROOM_AGENT_VAULT_CA_FILE"] = "/etc/agent-vault/ca.pem"
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=env,
    )
    return runtime_paths, "http://av_sess_worker_token:@agent-vault:14322"


def test_request_execution_env_overlays_proxy_when_no_primary_env(tmp_path: Path) -> None:
    """When the primary ships no execution env, the worker still composes the proxy."""
    runtime_paths, expected_proxy = _runtime_paths_with_vault(tmp_path)
    env = sandbox_exec.request_execution_env("shell", None, runtime_paths)
    assert env["HTTP_PROXY"] == expected_proxy
    assert env["HTTPS_PROXY"] == expected_proxy
    assert env["REQUESTS_CA_BUNDLE"] == "/etc/agent-vault/ca.pem"


def test_request_execution_env_overlays_proxy_onto_shipped_env(tmp_path: Path) -> None:
    """A non-empty primary execution env still gets the worker-local proxy overlaid."""
    runtime_paths, expected_proxy = _runtime_paths_with_vault(tmp_path)
    shipped = {"PATH": "/usr/bin", "SOME_TOOL_VAR": "x"}
    env = sandbox_exec.request_execution_env("python", shipped, runtime_paths)
    assert env["SOME_TOOL_VAR"] == "x"
    assert env["HTTP_PROXY"] == expected_proxy
    assert env["http_proxy"] == expected_proxy


def test_request_execution_env_no_overlay_without_vault_config(tmp_path: Path) -> None:
    """Without Agent Vault worker env, no proxy is injected."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={},
    )
    env = sandbox_exec.request_execution_env("shell", None, runtime_paths)
    assert "HTTP_PROXY" not in env


def test_request_execution_env_skips_non_execution_tools(tmp_path: Path) -> None:
    """Non-execution tools never get the proxy overlay."""
    runtime_paths, _ = _runtime_paths_with_vault(tmp_path)
    assert sandbox_exec.request_execution_env("website", None, runtime_paths) == {}
