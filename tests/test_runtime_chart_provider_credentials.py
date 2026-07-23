"""Runtime chart providerCredentials checks against the runtime's native env contract."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from mindroom.constants import PROVIDER_ENV_KEYS

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CHART_DIR = REPO_ROOT / "cluster" / "k8s" / "runtime"
HELPERS_TPL = RUNTIME_CHART_DIR / "templates" / "_helpers.tpl"
ENV_MAP_DEFINE = '{{- define "mindroom-runtime.providerCredentialEnvMap" -}}'


def _chart_provider_env_map() -> dict[str, str]:
    lines = HELPERS_TPL.read_text(encoding="utf-8").splitlines()
    start = lines.index(ENV_MAP_DEFINE) + 1
    end = lines.index("{{- end -}}", start)
    return yaml.safe_load("\n".join(lines[start:end]))


def test_chart_provider_env_map_matches_provider_env_keys() -> None:
    """The chart's provider map must stay identical to PROVIDER_ENV_KEYS."""
    assert _chart_provider_env_map() == PROVIDER_ENV_KEYS


def test_docs_list_every_supported_provider() -> None:
    """The enumerated provider lists in values.yaml and README.md must stay complete."""
    values_marker = "# Supported providers mirror PROVIDER_ENV_KEYS in src/mindroom/constants.py:"
    values_lines = (RUNTIME_CHART_DIR / "values.yaml").read_text(encoding="utf-8").splitlines()
    values_list = values_lines[values_lines.index(values_marker) + 1]
    readme_lines = (RUNTIME_CHART_DIR / "README.md").read_text(encoding="utf-8").splitlines()
    readme_list = next(line for line in readme_lines if line.startswith("Supported provider names are "))
    for provider in PROVIDER_ENV_KEYS:
        assert provider in values_list, f"values.yaml provider list is missing {provider}"
        assert f"`{provider}`" in readme_list, f"README provider list is missing {provider}"


def _render_runtime_chart(*set_json_args: str) -> list[dict[str, Any]]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for rendered chart checks")
    completed = subprocess.run(
        [
            helm,
            "template",
            "mindroom-demo",
            str(RUNTIME_CHART_DIR),
            *(arg for value in set_json_args for arg in ("--set-json", value)),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(completed.stdout) if isinstance(doc, dict)]


def _runtime_container_env(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deployment = next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "mindroom-demo-mindroom-runtime"
    )
    container = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "mindroom")
    return container["env"]


def test_provider_credentials_render_only_secret_key_refs() -> None:
    """Each entry binds the provider env var to a secretKeyRef with no inline value."""
    docs = _render_runtime_chart(
        'providerCredentials=[{"provider":"openai","existingSecret":"llm-secret","key":"api-key"},'
        '{"provider":"anthropic","existingSecret":"llm-secret","key":"anthropic-api-key"}]',
    )
    env = {entry["name"]: entry for entry in _runtime_container_env(docs)}
    assert env["OPENAI_API_KEY"] == {
        "name": "OPENAI_API_KEY",
        "valueFrom": {"secretKeyRef": {"name": "llm-secret", "key": "api-key"}},
    }
    assert env["ANTHROPIC_API_KEY"] == {
        "name": "ANTHROPIC_API_KEY",
        "valueFrom": {"secretKeyRef": {"name": "llm-secret", "key": "anthropic-api-key"}},
    }


def test_provider_credentials_reject_unknown_provider() -> None:
    """Validation fails fast for providers outside the runtime's native env contract."""
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _render_runtime_chart('providerCredentials=[{"provider":"nope","existingSecret":"s","key":"k"}]')
    assert 'providerCredentials[0].provider "nope" is not supported' in excinfo.value.stderr
