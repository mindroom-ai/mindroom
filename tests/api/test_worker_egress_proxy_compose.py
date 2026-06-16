"""Worker-side composition of Agent Vault proxy env for python/shell."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom import constants
from mindroom.api import sandbox_exec
from mindroom.constants import resolve_runtime_paths, worker_proxy_execution_env
from mindroom.runtime_env_policy import WORKER_EGRESS_PROXY_ENV_BY_KEY
from mindroom.workers.backends import kubernetes_resources as resources

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_paths_with_vault(tmp_path: Path, *, with_ca: bool = True) -> tuple[object, str]:
    token_path = tmp_path / "token"
    token_path.write_text("av_sess_worker_token\n", encoding="utf-8")
    env = {
        "MINDROOM_WORKER_EGRESS_PROXY_URL": "http://agent-vault:14322",
        "MINDROOM_WORKER_EGRESS_PROXY_TOKEN_FILE": str(token_path),
        "MINDROOM_WORKER_EGRESS_PROXY_VAULT": "agent-vault-worker",
    }
    if with_ca:
        env["MINDROOM_WORKER_EGRESS_PROXY_CA_FILE"] = "/etc/agent-vault/ca.pem"
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=env,
    )
    return runtime_paths, "http://av_sess_worker_token:agent-vault-worker@agent-vault:14322"


def test_request_execution_env_overlays_proxy_when_no_primary_env(tmp_path: Path) -> None:
    """When the primary ships no execution env, the worker still composes the proxy."""
    runtime_paths, expected_proxy = _runtime_paths_with_vault(tmp_path)
    env = sandbox_exec.request_execution_env("shell", None, runtime_paths)
    assert env["HTTP_PROXY"] == expected_proxy
    assert env["HTTPS_PROXY"] == expected_proxy
    assert env["REQUESTS_CA_BUNDLE"] == "/etc/agent-vault/ca.pem"
    # git and node ignore the curl/python bundles; they need their own keys.
    assert env["GIT_SSL_CAINFO"] == "/etc/agent-vault/ca.pem"
    assert env["NODE_EXTRA_CA_CERTS"] == "/etc/agent-vault/ca.pem"


def test_worker_proxy_execution_env_configures_git_proxy_auth(tmp_path: Path) -> None:
    """Git must receive proxy auth as config, not only env proxy userinfo."""
    _, expected_proxy = _runtime_paths_with_vault(tmp_path)
    token_path = tmp_path / "token"

    env = worker_proxy_execution_env(
        {
            "MINDROOM_WORKER_EGRESS_PROXY_URL": "http://agent-vault:14322",
            "MINDROOM_WORKER_EGRESS_PROXY_TOKEN_FILE": str(token_path),
            "MINDROOM_WORKER_EGRESS_PROXY_VAULT": "agent-vault-worker",
        },
    )

    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "http.proxy"
    assert env["GIT_CONFIG_VALUE_0"] == expected_proxy
    assert env["GIT_CONFIG_KEY_1"] == "http.proxyAuthMethod"
    assert env["GIT_CONFIG_VALUE_1"] == "basic"


def test_worker_proxy_execution_env_appends_to_existing_git_config(tmp_path: Path) -> None:
    """Worker proxy Git config must not discard caller-supplied Git config."""
    _, expected_proxy = _runtime_paths_with_vault(tmp_path)
    token_path = tmp_path / "token"

    env = worker_proxy_execution_env(
        {
            "MINDROOM_WORKER_EGRESS_PROXY_URL": "http://agent-vault:14322",
            "MINDROOM_WORKER_EGRESS_PROXY_TOKEN_FILE": str(token_path),
            "MINDROOM_WORKER_EGRESS_PROXY_VAULT": "agent-vault-worker",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        },
    )

    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == "*"
    assert env["GIT_CONFIG_KEY_1"] == "http.proxy"
    assert env["GIT_CONFIG_VALUE_1"] == expected_proxy
    assert env["GIT_CONFIG_KEY_2"] == "http.proxyAuthMethod"
    assert env["GIT_CONFIG_VALUE_2"] == "basic"


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


def test_worker_proxy_execution_env_percent_encodes_token(tmp_path: Path) -> None:
    """Proxy userinfo with URL-significant characters must be percent-encoded."""
    token_path = tmp_path / "token"
    token_path.write_text("av/sess+a:b@c\n", encoding="utf-8")
    env = worker_proxy_execution_env(
        {
            "MINDROOM_WORKER_EGRESS_PROXY_URL": "http://agent-vault:14322",
            "MINDROOM_WORKER_EGRESS_PROXY_TOKEN_FILE": str(token_path),
            "MINDROOM_WORKER_EGRESS_PROXY_VAULT": "vault/a:b@c",
        },
    )
    assert env["HTTP_PROXY"] == "http://av%2Fsess%2Ba%3Ab%40c:vault%2Fa%3Ab%40c@agent-vault:14322"


@pytest.mark.parametrize(
    "vault_env",
    [
        pytest.param(None, id="absent"),
        pytest.param(" \t\n", id="blank"),
    ],
)
def test_worker_proxy_execution_env_allows_missing_vault_as_empty_proxy_password(
    tmp_path: Path,
    vault_env: str | None,
) -> None:
    """Missing/blank vault keeps the backwards-compatible token-only proxy URL."""
    token_path = tmp_path / "token"
    token_path.write_text("av/sess+a:b@c\n", encoding="utf-8")
    process_env = {
        "MINDROOM_WORKER_EGRESS_PROXY_URL": "http://agent-vault:14322",
        "MINDROOM_WORKER_EGRESS_PROXY_TOKEN_FILE": str(token_path),
    }
    if vault_env is not None:
        process_env["MINDROOM_WORKER_EGRESS_PROXY_VAULT"] = vault_env

    env = worker_proxy_execution_env(process_env)

    expected_proxy = "http://av%2Fsess%2Ba%3Ab%40c:@agent-vault:14322"
    assert env["HTTP_PROXY"] == expected_proxy
    assert env["HTTPS_PROXY"] == expected_proxy
    assert env["GIT_CONFIG_VALUE_0"] == expected_proxy


def test_worker_proxy_execution_env_fail_closed_guards(tmp_path: Path) -> None:
    """Every missing/invalid precondition returns exactly {} (no half-formed proxy)."""
    token_path = tmp_path / "token"
    token_path.write_text("tok\n", encoding="utf-8")
    base = {
        "MINDROOM_WORKER_EGRESS_PROXY_URL": "http://agent-vault:14322",
        "MINDROOM_WORKER_EGRESS_PROXY_TOKEN_FILE": str(token_path),
        "MINDROOM_WORKER_EGRESS_PROXY_VAULT": "agent-vault-worker",
    }
    # scheme-less proxy URL
    assert worker_proxy_execution_env({**base, "MINDROOM_WORKER_EGRESS_PROXY_URL": "agent-vault:14322"}) == {}
    # token-file env absent
    assert worker_proxy_execution_env({"MINDROOM_WORKER_EGRESS_PROXY_URL": "http://agent-vault:14322"}) == {}
    # token file does not exist
    assert worker_proxy_execution_env({**base, "MINDROOM_WORKER_EGRESS_PROXY_TOKEN_FILE": str(tmp_path / "nope")}) == {}
    # whitespace-only token
    empty = tmp_path / "empty"
    empty.write_text("   \n", encoding="utf-8")
    assert worker_proxy_execution_env({**base, "MINDROOM_WORKER_EGRESS_PROXY_TOKEN_FILE": str(empty)}) == {}


def test_worker_egress_proxy_env_names_have_single_source() -> None:
    """Writer (k8s backend) and reader (constants) must use the same env names.

    Both derive from runtime_env_policy.WORKER_EGRESS_PROXY_ENV_BY_KEY, so a
    rename cannot silently desync the pod env writer from the runner reader.
    """
    assert (
        resources._WORKER_EGRESS_PROXY_URL_ENV
        == constants._WORKER_EGRESS_PROXY_URL_ENV
        == WORKER_EGRESS_PROXY_ENV_BY_KEY["proxy_url"]
    )
    assert (
        resources._WORKER_EGRESS_PROXY_TOKEN_FILE_ENV
        == constants._WORKER_EGRESS_PROXY_TOKEN_FILE_ENV
        == WORKER_EGRESS_PROXY_ENV_BY_KEY["token_file"]
    )
    assert (
        resources._WORKER_EGRESS_PROXY_VAULT_ENV
        == constants._WORKER_EGRESS_PROXY_VAULT_ENV
        == WORKER_EGRESS_PROXY_ENV_BY_KEY["vault"]
    )
    assert (
        resources._WORKER_EGRESS_PROXY_CA_FILE_ENV
        == constants._WORKER_EGRESS_PROXY_CA_FILE_ENV
        == WORKER_EGRESS_PROXY_ENV_BY_KEY["ca_file"]
    )
