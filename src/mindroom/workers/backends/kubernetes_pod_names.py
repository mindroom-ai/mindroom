"""Shared Kubernetes worker pod container and volume names."""

from __future__ import annotations

SANDBOX_RUNNER_CONTAINER_NAME = "sandbox-runner"
AGENT_VAULT_MINT_CONTAINER_NAME = "agent-vault-mint-token"

WORKER_STORAGE_VOLUME_NAME = "worker-storage"
WORKER_CONFIG_VOLUME_NAME = "worker-config"
AGENT_VAULT_TOKEN_VOLUME_NAME = "agent-vault-token"  # noqa: S105
AGENT_VAULT_BOOTSTRAP_VOLUME_NAME = "agent-vault-bootstrap"
AGENT_VAULT_CA_VOLUME_NAME = "agent-vault-ca"

RESERVED_EXTRA_CONTAINER_NAMES = frozenset(
    {
        SANDBOX_RUNNER_CONTAINER_NAME,
        AGENT_VAULT_MINT_CONTAINER_NAME,
    },
)
RESERVED_EXTRA_VOLUME_NAMES = frozenset(
    {
        WORKER_STORAGE_VOLUME_NAME,
        WORKER_CONFIG_VOLUME_NAME,
        AGENT_VAULT_TOKEN_VOLUME_NAME,
        AGENT_VAULT_BOOTSTRAP_VOLUME_NAME,
        AGENT_VAULT_CA_VOLUME_NAME,
    },
)
