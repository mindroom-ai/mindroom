"""Tests for worker-routed brokered egress configuration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

import mindroom.agents as agents_module
import mindroom.tool_system.sandbox_proxy as sandbox_proxy_module
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target, worker_id_for_key

if TYPE_CHECKING:
    from pathlib import Path


def _broker_config_payload() -> dict[str, object]:
    return {
        "worker_egress_brokers": {
            "agent_vault": {
                "kind": "static",
                "proxy_url": "http://agent-vault-adapter:18080",
                "ca_bundle": "/etc/agent-vault/ca.pem",
                "no_proxy": "localhost,127.0.0.1,.svc",
            },
            "local": {
                "kind": "static",
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


def _agent_vault_env() -> dict[str, str]:
    return {
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


def _worker_scoped_proxy_payload() -> dict[str, object]:
    return {
        "worker_egress_brokers": {
            "agent_vault": {
                "kind": "worker_scoped_proxy",
                "service_name_prefix": "agent-vault-bridge",
                "port": 18080,
                "no_proxy": "localhost,127.0.0.1,::1,.svc,.cluster.local",
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
        },
    }


def _execution_identity() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.test",
        room_id="!room:example.test",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-1",
        tenant_id="ionq",
    )


def _expected_worker_scoped_proxy_env(worker_key: str, *, service_name_prefix: str) -> dict[str, str]:
    bridge_service_name = worker_id_for_key(worker_key, prefix=service_name_prefix)
    proxy_url = f"http://{bridge_service_name}:18080"
    return {
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "NO_PROXY": "localhost,127.0.0.1,::1,.svc,.cluster.local",
        "no_proxy": "localhost,127.0.0.1,::1,.svc,.cluster.local",
    }


def test_worker_egress_broker_resolves_proxy_env_from_default_and_agent_override() -> None:
    """Worker egress broker should resolve default and per-agent broker env."""
    config = Config.model_validate(_broker_config_payload())

    default_broker = config.get_agent_worker_egress_broker("code")
    override_broker = config.get_agent_worker_egress_broker("local_code")

    assert default_broker is not None
    assert default_broker.execution_env_for_worker_target(None) == _agent_vault_env()
    assert override_broker is not None
    assert override_broker.execution_env_for_worker_target(None)["HTTP_PROXY"] == "http://127.0.0.1:19090"


def test_worker_egress_broker_requires_explicit_kind() -> None:
    """Worker egress brokers should not accept legacy implicit static config."""
    payload = _broker_config_payload()
    brokers = payload["worker_egress_brokers"]
    assert isinstance(brokers, dict)
    local_broker = brokers["local"]
    assert isinstance(local_broker, dict)
    del local_broker["kind"]

    with pytest.raises(ValueError, match=r"worker_egress_brokers\.local\.kind"):
        Config.model_validate(payload)


def test_worker_scoped_proxy_broker_resolves_proxy_env_from_worker_key() -> None:
    """Worker-scoped proxy broker should derive a bridge URL from the worker key."""
    config = Config.model_validate(_worker_scoped_proxy_payload())
    broker = config.get_agent_worker_egress_broker("code")
    identity = _execution_identity()
    worker_target = resolve_worker_target("user_agent", "code", execution_identity=identity)
    assert worker_target.worker_key is not None

    assert broker is not None
    assert broker.execution_env_for_worker_target(worker_target) == (
        _expected_worker_scoped_proxy_env(
            worker_target.worker_key,
            service_name_prefix="agent-vault-bridge",
        )
    )


def test_worker_scoped_proxy_broker_requires_worker_key() -> None:
    """Worker-scoped proxy broker should fail closed when no worker key is available."""
    config = Config.model_validate(_worker_scoped_proxy_payload())
    broker = config.get_agent_worker_egress_broker("code")

    assert broker is not None
    with pytest.raises(ValueError, match="Worker-scoped proxy broker requires a resolved worker key"):
        broker.execution_env_for_worker_target(None)


def test_worker_scoped_proxy_broker_requires_worker_scoped_agent() -> None:
    """Worker-scoped proxy broker bound to an agent without worker scope should fail at config load."""
    payload = _worker_scoped_proxy_payload()
    agents = payload["agents"]
    assert isinstance(agents, dict)
    code_agent = agents["code"]
    assert isinstance(code_agent, dict)
    del code_agent["worker_scope"]

    with pytest.raises(
        ValueError,
        match=r"Agent 'code' uses worker-scoped egress broker 'agent_vault' but has no worker execution scope",
    ):
        Config.model_validate(payload)


def test_static_broker_rejects_worker_scoped_fields() -> None:
    """Static brokers should reject worker-scoped-only fields instead of ignoring them."""
    payload = _broker_config_payload()
    brokers = payload["worker_egress_brokers"]
    assert isinstance(brokers, dict)
    local_broker = brokers["local"]
    assert isinstance(local_broker, dict)
    local_broker["port"] = 18080

    with pytest.raises(ValueError, match="port only applies to worker_scoped_proxy brokers"):
        Config.model_validate(payload)


def test_worker_scoped_proxy_broker_rejects_proxy_url() -> None:
    """Worker-scoped proxy brokers should reject a static proxy_url instead of ignoring it."""
    payload = _worker_scoped_proxy_payload()
    brokers = payload["worker_egress_brokers"]
    assert isinstance(brokers, dict)
    broker = brokers["agent_vault"]
    assert isinstance(broker, dict)
    broker["proxy_url"] = "http://agent-vault-adapter:18080"

    with pytest.raises(ValueError, match="proxy_url does not apply to worker_scoped_proxy brokers"):
        Config.model_validate(payload)


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

    with pytest.raises(
        ValueError,
        match=r"defaults.worker_egress_broker references unknown worker egress broker 'missing'.*agent_vault, local",
    ):
        Config.model_validate(payload)


def test_unknown_agent_worker_egress_broker_reports_config_path() -> None:
    """Unknown agent-level worker egress broker names should fail at config load."""
    payload = _broker_config_payload()
    agents = payload["agents"]
    assert isinstance(agents, dict)
    agent_config = agents["local_code"]
    assert isinstance(agent_config, dict)
    agent_config["worker_egress_broker"] = "missing"

    with pytest.raises(
        ValueError,
        match=r"agents.local_code.worker_egress_broker references unknown worker egress broker 'missing'",
    ):
        Config.model_validate(payload)


def test_worker_egress_broker_rejects_proxy_url_without_scheme() -> None:
    """Worker egress broker should reject proxy URLs without HTTP scheme."""
    payload = _broker_config_payload()
    brokers = payload["worker_egress_brokers"]
    assert isinstance(brokers, dict)
    local_broker = brokers["local"]
    assert isinstance(local_broker, dict)
    local_broker["proxy_url"] = "127.0.0.1:19090"

    with pytest.raises(ValueError, match="proxy_url must include an http:// or https:// scheme"):
        Config.model_validate(payload)


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
        worker_egress_env=broker.execution_env_for_worker_target(None),
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
        runtime_overrides=config.get_agent_tool_runtime_overrides("code", "shell", runtime_paths=runtime_paths),
        execution_identity=None,
    )

    assert toolkit is not None
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    worker_egress_env = kwargs["worker_egress_env"]
    assert isinstance(worker_egress_env, dict)
    for key, value in _agent_vault_env().items():
        assert worker_egress_env[key] == value


def test_build_agent_toolkit_passes_worker_scoped_proxy_egress_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Agent tool construction should pass worker-key-derived proxy env to registered tools."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_KUBERNETES_WORKER_NAME_PREFIX": "mindroom-worker-staging",
        },
    )
    config = Config.model_validate(_worker_scoped_proxy_payload())
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
        runtime_overrides=config.get_agent_tool_runtime_overrides("code", "shell", runtime_paths=runtime_paths),
        execution_identity=_execution_identity(),
    )

    worker_target = resolve_worker_target("user_agent", "code", execution_identity=_execution_identity())
    assert worker_target.worker_key is not None
    assert toolkit is not None
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["worker_egress_env"] == _expected_worker_scoped_proxy_env(
        worker_target.worker_key,
        service_name_prefix="agent-vault-bridge",
    )
