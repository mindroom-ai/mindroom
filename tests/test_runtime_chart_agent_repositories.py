"""Runtime chart checks for constrained agent repository broker wiring."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CHART_DIR = REPO_ROOT / "cluster" / "k8s" / "runtime"
BROKER_MOUNT_DIRECTORY = "/etc/agent-repository-broker"


def _render_runtime_chart(*set_args: str) -> list[dict[str, Any]]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for rendered chart checks")
    completed = subprocess.run(
        [
            helm,
            "template",
            "mindroom-demo",
            str(RUNTIME_CHART_DIR),
            *(argument for value in set_args for argument in ("--set", value)),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(completed.stdout) if isinstance(doc, dict)]


def _deployment(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "mindroom-demo-mindroom-runtime"
    )


def _enabled_values() -> tuple[str, ...]:
    return (
        "agentRepositories.enabled=true",
        "agentRepositories.organization=example-org",
        "agentRepositories.prefix=MindRoom",
        "agentRepositories.brokerUrl=http://agent-vault:14321",
        "agentRepositories.brokerTokenSecret.name=agent-vault-mindroom-repository-broker",
        "agentRepositories.brokerTokenSecret.key=token",
        "workers.backend=kubernetes",
        "workers.kubernetes.agentVault.enabled=true",
        "workers.kubernetes.agentVault.cliImage=agent-vault:test",
        "workers.kubernetes.agentVault.ownerEmail=owner@example.test",
    )


def test_disabled_agent_repositories_render_no_broker_material() -> None:
    """Disabled defaults must not add broker env, mounts, or Secret references."""
    pod_spec = _deployment(_render_runtime_chart())["spec"]["template"]["spec"]

    assert all(
        not entry["name"].startswith("MINDROOM_AGENT_REPOSITORY_")
        for container in pod_spec["containers"]
        for entry in container.get("env", [])
    )
    assert "agent-repository-broker" not in {volume["name"] for volume in pod_spec.get("volumes", [])}


def test_enabled_agent_repositories_render_only_to_control_plane() -> None:
    """Broker capability should reach only MindRoom through a mounted Secret file."""
    pod_spec = _deployment(_render_runtime_chart(*_enabled_values()))["spec"]["template"]["spec"]
    containers = {container["name"]: container for container in pod_spec["containers"]}
    mindroom = containers["mindroom"]
    broker_env = {
        entry["name"]: entry for entry in mindroom["env"] if entry["name"].startswith("MINDROOM_AGENT_REPOSITORY_")
    }

    assert broker_env == {
        "MINDROOM_AGENT_REPOSITORY_BROKER_URL": {
            "name": "MINDROOM_AGENT_REPOSITORY_BROKER_URL",
            "value": "http://agent-vault:14321",
        },
        "MINDROOM_AGENT_REPOSITORY_BROKER_TOKEN_FILE": {
            "name": "MINDROOM_AGENT_REPOSITORY_BROKER_TOKEN_FILE",
            "value": f"{BROKER_MOUNT_DIRECTORY}/token",
        },
    }
    assert {
        "name": "agent-repository-broker",
        "mountPath": BROKER_MOUNT_DIRECTORY,
        "readOnly": True,
    } in mindroom["volumeMounts"]
    assert {
        "name": "agent-repository-broker",
        "secret": {
            "secretName": "agent-vault-mindroom-repository-broker",
            "items": [{"key": "token", "path": "token"}],
        },
    } in pod_spec["volumes"]

    for name, container in containers.items():
        if name == "mindroom":
            continue
        assert all(not entry["name"].startswith("MINDROOM_AGENT_REPOSITORY_") for entry in container.get("env", []))
        assert "agent-repository-broker" not in {mount["name"] for mount in container.get("volumeMounts", [])}
    for container in pod_spec.get("initContainers", []):
        assert all(not entry["name"].startswith("MINDROOM_AGENT_REPOSITORY_") for entry in container.get("env", []))
        assert "agent-repository-broker" not in {mount["name"] for mount in container.get("volumeMounts", [])}


def test_agent_repositories_validation_rejects_partial_or_unsafe_policy() -> None:
    """Enabled chart wiring requires the complete fixed policy and Secret reference."""
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for rendered chart checks")

    for values, expected_error in (
        (("agentRepositories.enabled=true",), "agentRepositories.organization is required"),
        (
            (
                "agentRepositories.enabled=true",
                "agentRepositories.organization=example-org",
            ),
            "agentRepositories.brokerTokenSecret.name is required",
        ),
        (
            (*_enabled_values(), "agentRepositories.prefix=Other"),
            "agentRepositories.prefix must be MindRoom",
        ),
        (
            (*_enabled_values(), "agentRepositories.brokerUrl=http://user:pass@agent-vault:14321"),
            "agentRepositories.brokerUrl must be an uncredentialed HTTP(S) base URL",
        ),
        (
            (*_enabled_values(), "agentRepositories.brokerUrl=http://agent-vault:99999"),
            "agentRepositories.brokerUrl port must be between 1 and 65535",
        ),
        (
            (*_enabled_values(), "workers.backend=static_runner"),
            "agentRepositories.enabled requires workers.backend=kubernetes",
        ),
        (
            (*_enabled_values(), "workers.kubernetes.agentVault.enabled=false"),
            "agentRepositories.enabled requires workers.kubernetes.agentVault.enabled=true",
        ),
    ):
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            _render_runtime_chart(*values)
        assert expected_error in excinfo.value.stderr


def test_values_schema_locks_agent_repository_field_names() -> None:
    """Infra should consume one exact chart-values contract without guessed aliases."""
    schema = json.loads((RUNTIME_CHART_DIR / "values.schema.json").read_text(encoding="utf-8"))
    policy = schema["properties"]["agentRepositories"]

    assert policy["additionalProperties"] is False
    assert set(policy["properties"]) == {
        "enabled",
        "organization",
        "prefix",
        "brokerUrl",
        "brokerTokenSecret",
    }
    assert policy["properties"]["brokerTokenSecret"]["additionalProperties"] is False
    assert set(policy["properties"]["brokerTokenSecret"]["properties"]) == {"name", "key"}
