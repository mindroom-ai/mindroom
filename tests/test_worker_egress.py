"""Tests for worker-routed brokered egress configuration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

import mindroom.agents as agents_module
import mindroom.tool_system.sandbox_proxy as sandbox_proxy_module
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _broker_config_payload() -> dict[str, object]:
    return {
        "worker_egress_brokers": {
            "agent_vault": {
                "proxy_url": "http://agent-vault-adapter:18080",
                "ca_bundle": "/etc/agent-vault/ca.pem",
                "no_proxy": "localhost,127.0.0.1,.svc",
            },
            "local": {
                "proxy_url": "http://127.0.0.1:19090",
            },
        },
        "defaults": {
            "worker_egress_broker": "agent_vault",
        },
        "agents": {
            "code": {
                "display_name": "Code",
                "tools": ["shell"],
                "worker_scope": "user_agent",
            },
            "local_code": {
                "display_name": "Local Code",
                "tools": ["shell"],
                "worker_scope": "user_agent",
                "worker_egress_broker": "local",
            },
        },
    }


def test_worker_egress_broker_resolves_proxy_env_from_default_and_agent_override() -> None:
    """Worker egress broker should resolve default and per-agent broker env."""
    config = Config.model_validate(_broker_config_payload())

    default_broker = config.get_agent_worker_egress_broker("code")
    override_broker = config.get_agent_worker_egress_broker("local_code")

    assert default_broker is not None
    assert default_broker.name == "agent_vault"
    assert default_broker.execution_env == {
        "HTTP_PROXY": "http://agent-vault-adapter:18080",
        "HTTPS_PROXY": "http://agent-vault-adapter:18080",
        "http_proxy": "http://agent-vault-adapter:18080",
        "https_proxy": "http://agent-vault-adapter:18080",
        "REQUESTS_CA_BUNDLE": "/etc/agent-vault/ca.pem",
        "CURL_CA_BUNDLE": "/etc/agent-vault/ca.pem",
        "SSL_CERT_FILE": "/etc/agent-vault/ca.pem",
        "NO_PROXY": "localhost,127.0.0.1,.svc",
        "no_proxy": "localhost,127.0.0.1,.svc",
    }
    assert override_broker is not None
    assert override_broker.name == "local"
    assert override_broker.execution_env["HTTP_PROXY"] == "http://127.0.0.1:19090"


def test_agent_can_disable_default_worker_egress_broker() -> None:
    """Agent config should disable an inherited worker egress broker with false."""
    payload = _broker_config_payload()
    payload["agents"] = {
        "open": {
            "display_name": "Open",
            "tools": ["shell"],
            "worker_scope": "user_agent",
            "worker_egress_broker": False,
        },
    }
    config = Config.model_validate(payload)

    assert config.get_agent_worker_egress_broker("open") is None


def test_unknown_worker_egress_broker_reports_available_names() -> None:
    """Unknown worker egress broker names should report configured broker names."""
    payload = _broker_config_payload()
    payload["defaults"] = {"worker_egress_broker": "missing"}
    config = Config.model_validate(payload)

    with pytest.raises(ValueError, match=r"Unknown worker_egress_broker 'missing'.*agent_vault, local"):
        config.get_agent_worker_egress_broker("code")


def test_sandbox_execution_env_payload_includes_worker_egress_env(tmp_path: Path) -> None:
    """Sandbox execution env should include broker env without provider secrets."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"OPENAI_API_KEY": "primary-secret"},
    )
    config = Config.model_validate(_broker_config_payload())
    broker = config.get_agent_worker_egress_broker("code")
    assert broker is not None

    execution_env = sandbox_proxy_module._execution_env_payload(
        "shell",
        runtime_paths=runtime_paths,
        worker_egress_env=broker.execution_env,
    )

    assert execution_env is not None
    assert execution_env["HTTP_PROXY"] == "http://agent-vault-adapter:18080"
    assert execution_env["HTTPS_PROXY"] == "http://agent-vault-adapter:18080"
    assert execution_env["REQUESTS_CA_BUNDLE"] == "/etc/agent-vault/ca.pem"
    assert "OPENAI_API_KEY" not in execution_env


def test_build_agent_toolkit_passes_worker_egress_env_to_registered_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Agent tool construction should pass resolved worker egress env to registered tools."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    config = Config.model_validate(_broker_config_payload())
    captured: dict[str, object] = {}

    def fake_resolve_agent_runtime(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            execution_scope="user_agent",
            is_private=False,
            tool_base_dir=tmp_path / "workspace",
            workspace=tmp_path / "workspace",
        )

    def fake_get_tool_by_name(*args: object, **kwargs: object) -> SimpleNamespace:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(name="shell", functions={}, async_functions={})

    monkeypatch.setattr(agents_module, "resolve_agent_runtime", fake_resolve_agent_runtime)
    monkeypatch.setattr(agents_module, "get_tool_by_name", fake_get_tool_by_name)
    monkeypatch.setattr(agents_module, "get_runtime_credentials_manager", lambda *_args: object())

    toolkit = agents_module.build_agent_toolkit(
        "shell",
        agent_name="code",
        config=config,
        runtime_paths=runtime_paths,
        worker_tools=["shell"],
        execution_identity=None,
    )

    assert toolkit is not None
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["worker_egress_env"] == {
        "HTTP_PROXY": "http://agent-vault-adapter:18080",
        "HTTPS_PROXY": "http://agent-vault-adapter:18080",
        "http_proxy": "http://agent-vault-adapter:18080",
        "https_proxy": "http://agent-vault-adapter:18080",
        "REQUESTS_CA_BUNDLE": "/etc/agent-vault/ca.pem",
        "CURL_CA_BUNDLE": "/etc/agent-vault/ca.pem",
        "SSL_CERT_FILE": "/etc/agent-vault/ca.pem",
        "NO_PROXY": "localhost,127.0.0.1,.svc",
        "no_proxy": "localhost,127.0.0.1,.svc",
    }
