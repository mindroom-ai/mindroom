"""Validation and Helm render checks for the multi-user runtime preset examples."""

from __future__ import annotations

from pathlib import Path

from mindroom.config.main import load_config
from mindroom.constants import resolve_runtime_paths
from tests.test_helm_instance_worker_isolation import (
    _container,
    _env_by_name,
    _render_chart,
    _resource,
    _volumes_by_name,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CHART_DIR = REPO_ROOT / "cluster" / "k8s" / "runtime"
VALUES_EXAMPLE = RUNTIME_CHART_DIR / "values-multi-user.example.yaml"
CONFIG_EXAMPLE = RUNTIME_CHART_DIR / "config-multi-user.example.yaml"


def test_multi_user_values_example_renders_runtime_chart() -> None:
    """The multi-user values example renders the runtime chart with expected worker and broker settings."""
    docs = _render_chart(
        RUNTIME_CHART_DIR,
        release_name="mindroom",
        values_files=(VALUES_EXAMPLE,),
    )
    deployment = _resource(docs, "Deployment", "mindroom-mindroom-runtime")
    container = _container(deployment, "mindroom")
    env = _env_by_name(container)

    assert env["MINDROOM_WORKER_BACKEND"]["value"] == "kubernetes"
    assert env["MINDROOM_SANDBOX_PROXY_TOOLS"]["value"] == "shell,file,coding,browser"
    assert env["MINDROOM_EGRESS_BROKER_PORT"]["value"] == "8768"

    config_mount = next(mount for mount in container["volumeMounts"] if mount["name"] == "config")
    assert config_mount["readOnly"] is True
    assert _volumes_by_name(deployment)["config"]["configMap"]["name"] == "mindroom-config"


def test_multi_user_config_example_validates(tmp_path: Path) -> None:
    """The companion multi-user config validates through MindRoom's runtime loader, including personal_agents."""
    runtime_paths = resolve_runtime_paths(config_path=CONFIG_EXAMPLE, storage_path=tmp_path)
    config = load_config(runtime_paths)

    assert config.event_journal.backend == "postgres"
    assert config.event_journal.database_url_env == "MINDROOM_EVENT_CACHE_DATABASE_URL"
    assert config.personal_rooms is not None
    assert config.personal_rooms.agent == "mind"
    assert config.personal_rooms.onboarding_rooms == ["lobby"]
    assert "mind" in config.personal_agents

    mind = config.personal_agents["mind"]
    assert mind.workspace_template is None
    assert mind.agent.worker_scope == "user_agent"
    assert mind.agent.dedicated_subagent_workers is True
    assert mind.agent.named_workers == 4
    assert mind.agent.knowledge_graph is True
    assert mind.agent.skill_learning.enabled is True
    assert "update_awareness" not in mind.agent.tool_names
    assert [(rule.match, rule.arguments) for rule in config.tool_approval.rules] == [
        ("thread_resolution", {"action": "resolve"}),
        ("matrix_user_write", None),
    ]
