"""Runtime chart values that merge across several Helm values files."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from mindroom.workers.backends._config_helpers import read_json_mapping_env
from tests.test_helm_instance_worker_isolation import (
    _container,
    _env_by_name,
    _render_chart,
    _resource,
    _run_helm_template,
    _volumes_by_name,
)

RUNTIME_CHART = Path("cluster/k8s/runtime")
RUNTIME_DEPLOYMENT = "mindroom-runtime"
KUBERNETES_WORKERS = (
    "workers.backend=kubernetes",
    "workers.sandbox.proxyToken.value=test-token",
    "eventCache.postgres.auth.password=test-password",
)
AGENT_VAULT_SERVER = (
    "workers.kubernetes.agentVault.server.enabled=true",
    "workers.kubernetes.agentVault.server.image=example.test/agent-vault:test",
)

SHARED_VALUES = """
matrix:
  serverName: chat.example.com
env:
  extra:
    PUBLIC_URL: "https://{{ .Values.matrix.serverName }}"
    UPLOAD_LIMIT: "104857600"
    AUDIENCE:
      value: shared-audience
    API_TOKEN:
      valueFrom:
        secretKeyRef:
          name: shared-secrets
          key: API_TOKEN
    RETIRED: retired
  envFrom:
    10-secrets:
      secretRef:
        name: shared-secrets
    20-config:
      configMapRef:
        name: shared-config
extraVolumes:
  session-state:
    persistentVolumeClaim:
      claimName: session-state
  knowledge-db:
    persistentVolumeClaim:
      claimName: knowledge-db
extraVolumeMounts:
  session-state:
    mountPath: /app/session_state
  session-tracking:
    name: session-state
    mountPath: /app/tracking
    subPath: tracking
  knowledge-db:
    mountPath: /app/knowledge_db
"""

ENVIRONMENT_VALUES = """
matrix:
  serverName: staging.example.com
env:
  extra:
    AUDIENCE:
      value: null
      valueFrom:
        secretKeyRef:
          name: staging-secrets
          key: AUDIENCE
    API_TOKEN:
      valueFrom:
        secretKeyRef:
          name: staging-secrets
    RETIRED: null
    STAGING_ONLY: "true"
  envFrom:
    10-secrets:
      secretRef:
        name: staging-secrets
extraVolumes:
  knowledge-db: null
extraVolumeMounts:
  knowledge-db: null
"""


def _values_file(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _runtime(docs: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    deployment = _resource(docs, "Deployment", RUNTIME_DEPLOYMENT)
    return deployment, _container(deployment, "mindroom")


def _render_layered(tmp_path: Path, *contents: str, set_args: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    values_files = tuple(
        _values_file(tmp_path, f"values-{index}.yaml", content) for index, content in enumerate(contents)
    )
    return _render_chart(
        RUNTIME_CHART,
        *set_args,
        release_name="mindroom-runtime",
        namespace="mindroom-staging",
        values_files=values_files,
    )


def test_list_forms_render_unchanged_in_their_given_order(tmp_path: Path) -> None:
    """Existing list values keep their order and content."""
    docs = _render_layered(
        tmp_path,
        """
        env:
          extra:
            - name: ZETA
              value: z
            - name: ALPHA
              valueFrom:
                secretKeyRef:
                  name: app-secrets
                  key: ALPHA
          envFrom:
            - configMapRef:
                name: zeta-config
            - secretRef:
                name: alpha-secrets
        extraVolumes:
          - name: zeta
            emptyDir: {}
          - name: alpha
            emptyDir: {}
        extraVolumeMounts:
          - name: zeta
            mountPath: /zeta
          - name: alpha
            mountPath: /alpha
        """,
    )
    deployment, container = _runtime(docs)

    assert [entry for entry in container["env"] if entry["name"] in {"ZETA", "ALPHA"}] == [
        {"name": "ZETA", "value": "z"},
        {"name": "ALPHA", "valueFrom": {"secretKeyRef": {"name": "app-secrets", "key": "ALPHA"}}},
    ]
    assert container["envFrom"][1:] == [
        {"configMapRef": {"name": "zeta-config"}},
        {"secretRef": {"name": "alpha-secrets"}},
    ]
    assert [volume["name"] for volume in deployment["spec"]["template"]["spec"]["volumes"]][-2:] == ["zeta", "alpha"]
    assert container["volumeMounts"][-2:] == [
        {"name": "zeta", "mountPath": "/zeta"},
        {"name": "alpha", "mountPath": "/alpha"},
    ]


def test_map_forms_merge_entry_by_entry_across_values_files(tmp_path: Path) -> None:
    """An environment file overrides, adds, and removes single entries without restating shared lists."""
    deployment, container = _runtime(_render_layered(tmp_path, SHARED_VALUES, ENVIRONMENT_VALUES))
    env = _env_by_name(container)

    assert env["AUDIENCE"] == {
        "name": "AUDIENCE",
        "valueFrom": {"secretKeyRef": {"name": "staging-secrets", "key": "AUDIENCE"}},
    }
    assert env["API_TOKEN"] == {
        "name": "API_TOKEN",
        "valueFrom": {"secretKeyRef": {"name": "staging-secrets", "key": "API_TOKEN"}},
    }
    assert env["UPLOAD_LIMIT"] == {"name": "UPLOAD_LIMIT", "value": "104857600"}
    assert env["STAGING_ONLY"] == {"name": "STAGING_ONLY", "value": "true"}
    assert "RETIRED" not in env
    assert container["envFrom"][1:] == [
        {"secretRef": {"name": "staging-secrets"}},
        {"configMapRef": {"name": "shared-config"}},
    ]
    volumes = _volumes_by_name(deployment)
    assert volumes["session-state"] == {
        "name": "session-state",
        "persistentVolumeClaim": {"claimName": "session-state"},
    }
    assert "knowledge-db" not in volumes
    assert [mount for mount in container["volumeMounts"] if mount["name"] in {"session-state", "knowledge-db"}] == [
        {"name": "session-state", "mountPath": "/app/session_state"},
        {"name": "session-state", "mountPath": "/app/tracking", "subPath": "tracking"},
    ]


def test_map_forms_render_in_key_order_for_deterministic_manifests(tmp_path: Path) -> None:
    """Map entries render sorted by key regardless of how values files list them."""
    _, container = _runtime(_render_layered(tmp_path, SHARED_VALUES, ENVIRONMENT_VALUES))
    extra_names = {"API_TOKEN", "AUDIENCE", "PUBLIC_URL", "STAGING_ONLY", "UPLOAD_LIMIT"}

    assert [entry["name"] for entry in container["env"] if entry["name"] in extra_names] == sorted(extra_names)


def test_string_env_values_render_templates(tmp_path: Path) -> None:
    """Shared env values can reference the release namespace and environment-specific values."""
    docs = _render_layered(
        tmp_path,
        """
        matrix:
          serverName: chat.example.com
        env:
          extra:
            - name: API_URL
              value: "http://api.{{ .Release.Namespace }}.svc.cluster.local:8080"
            - name: LITERAL_BRACES
              value: '{{ "{{" }} literal }}'
        workers:
          kubernetes:
            extraEnv:
              WORKER_API_URL: "http://api.{{ .Release.Namespace }}.svc.cluster.local:8080"
              all_proxy: null
            agentVault:
              server:
                extraEnv:
                  AGENT_VAULT_ADDR: "https://{{ .Values.matrix.serverName }}/agent-vault"
        egressProxy:
          enabled: true
          networkPolicy:
            proxyPodSelector:
              matchLabels:
                app: egress-proxy
          noProxy:
            - localhost
            - "api.{{ .Release.Namespace }}.svc.cluster.local"
        """,
        set_args=(*KUBERNETES_WORKERS, *AGENT_VAULT_SERVER),
    )
    _, container = _runtime(docs)
    env = _env_by_name(container)
    env_json = {"MINDROOM_KUBERNETES_WORKER_ENV_JSON": env["MINDROOM_KUBERNETES_WORKER_ENV_JSON"]["value"]}
    worker_env = json.loads(env_json["MINDROOM_KUBERNETES_WORKER_ENV_JSON"])
    vault_env = _env_by_name(_container(_resource(docs, "Deployment", "agent-vault"), "agent-vault"))

    assert env["API_URL"]["value"] == "http://api.mindroom-staging.svc.cluster.local:8080"
    assert env["LITERAL_BRACES"]["value"] == "{{ literal }}"
    assert worker_env["WORKER_API_URL"] == "http://api.mindroom-staging.svc.cluster.local:8080"
    # The runtime drops null worker env entries, which lets values remove a chart-injected proxy variable.
    assert worker_env["all_proxy"] is None
    assert "all_proxy" not in read_json_mapping_env(env_json, "MINDROOM_KUBERNETES_WORKER_ENV_JSON")
    assert worker_env["ALL_PROXY"].startswith("http://")
    assert worker_env["NO_PROXY"] == "localhost,api.mindroom-staging.svc.cluster.local"
    assert vault_env["AGENT_VAULT_ADDR"]["value"] == "https://chat.example.com/agent-vault"


def test_agent_vault_server_env_lists_accept_maps(tmp_path: Path) -> None:
    """The Agent Vault server env lists layer like the runtime container's."""
    docs = _render_layered(
        tmp_path,
        """
        workers:
          kubernetes:
            agentVault:
              server:
                extraEnv:
                  AGENT_VAULT_UI_BASE_PATH: /agent-vault
                  AGENT_VAULT_OAUTH_CLIENT_SECRET:
                    valueFrom:
                      secretKeyRef:
                        name: shared-oauth
                        key: client-secret
                envFrom:
                  settings:
                    configMapRef:
                      name: vault-settings
        """,
        """
        workers:
          kubernetes:
            agentVault:
              server:
                extraEnv:
                  AGENT_VAULT_OAUTH_CLIENT_SECRET:
                    valueFrom:
                      secretKeyRef:
                        name: staging-oauth
        """,
        set_args=AGENT_VAULT_SERVER,
    )
    vault = _container(_resource(docs, "Deployment", "agent-vault"), "agent-vault")
    env = _env_by_name(vault)

    assert env["AGENT_VAULT_UI_BASE_PATH"]["value"] == "/agent-vault"
    assert env["AGENT_VAULT_OAUTH_CLIENT_SECRET"]["valueFrom"] == {
        "secretKeyRef": {"name": "staging-oauth", "key": "client-secret"},
    }
    assert vault["envFrom"] == [{"configMapRef": {"name": "vault-settings"}}]


@pytest.mark.parametrize(
    ("values", "set_args", "error"),
    [
        (
            """
            workers:
              kubernetes:
                agentVault:
                  server:
                    extraEnv:
                      AGENT_VAULT_MASTER_PASSWORD: override
            """,
            AGENT_VAULT_SERVER,
            "workers.kubernetes.agentVault.server.extraEnv cannot override chart-managed AGENT_VAULT_MASTER_PASSWORD",
        ),
        (
            """
            providerCredentials:
              - provider: openai
                existingSecret: llm-secret
                key: api-key
            env:
              extra:
                OPENAI_API_KEY: duplicate
            """,
            (),
            "providerCredentials[0] and env.extra both set OPENAI_API_KEY",
        ),
        (
            """
            config:
              source: file
              path: /app/agent_data/runtime-config/config.yaml
              bootstrapBundlePath: /bundle
            workers:
              backend: kubernetes
            extraVolumeMounts:
              runtime-config:
                mountPath: /app/agent_data/runtime-config
            """,
            (),
            "config.path directory overlaps a mounted volume",
        ),
        (
            """
            extraVolumes:
              broken: not-a-volume
            """,
            (),
            "extraVolumes.broken must be a map or null",
        ),
        (
            """
            env:
              extra:
                UPLOAD_LIMIT: 104857600
            """,
            (),
            "env.extra.UPLOAD_LIMIT must be a string, a map, or null; quote numbers and booleans",
        ),
        (
            """
            env:
              envFrom: not-a-list
            """,
            (),
            "env.envFrom must be a list or a map",
        ),
        (
            """
            sessionStorage:
              enabled: true
            env:
              extra:
                MINDROOM_SESSION_STORAGE_PATH: /elsewhere
            """,
            (),
            "env.extra must not set MINDROOM_SESSION_STORAGE_PATH when sessionStorage.enabled=true",
        ),
        (
            """
            sessionStorage:
              enabled: true
            env:
              extra:
                MINDROOM_SESSION_STORAGE_PATH:
                  value: /elsewhere
            """,
            (),
            "env.extra must not set MINDROOM_SESSION_STORAGE_PATH when sessionStorage.enabled=true",
        ),
        (
            """
            knowledgeStorage:
              enabled: true
            extraVolumeMounts:
              kdb:
                mountPath: /app/agent_data/knowledge_db
            """,
            (),
            "/app/agent_data/knowledge_db, where knowledgeStorage is mounted, must differ from "
            "extraVolumeMounts.kdb.mountPath",
        ),
    ],
    ids=[
        "vault-managed-env",
        "provider-credential-env",
        "config-mount-overlap",
        "scalar-volume",
        "unquoted-number-env",
        "scalar-env-from",
        "session-path-env-shorthand",
        "session-path-env-entry",
        "knowledge-mount-overlap",
    ],
)
def test_map_forms_keep_chart_validation(
    tmp_path: Path,
    values: str,
    set_args: tuple[str, ...],
    error: str,
) -> None:
    """Validation sees map-form entries the same way it sees list entries."""
    completed = _run_helm_template(
        RUNTIME_CHART,
        *set_args,
        release_name="mindroom-runtime",
        values_files=(_values_file(tmp_path, "values.yaml", values),),
    )

    assert completed.returncode != 0
    assert error in completed.stderr


def test_default_and_layered_renders_emit_no_coalesce_warnings(tmp_path: Path) -> None:
    """Neither list nor map values trip Helm's table/non-table coalescing warnings."""
    list_values = _values_file(
        tmp_path,
        "list.yaml",
        """
        env:
          extra:
            - name: A
              value: a
        extraVolumes:
          - name: scratch
            emptyDir: {}
        """,
    )
    map_values = _values_file(tmp_path, "map.yaml", SHARED_VALUES)

    for values_file in (list_values, map_values):
        completed = _run_helm_template(RUNTIME_CHART, release_name="mindroom-runtime", values_files=(values_file,))
        completed.check_returncode()
        assert "warning" not in completed.stderr
        assert [doc for doc in yaml.safe_load_all(completed.stdout) if isinstance(doc, dict)]
