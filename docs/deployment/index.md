---
icon: lucide/cloud
---

# Deployment

MindRoom can be deployed in various ways depending on your needs.

Existing deployments moving from the previous Nio integration must follow the [Nio 1.0 cutover guide](upgrades.md#upgrading-to-nio-10).

## Deployment Options

| Method | Best For |
|--------|----------|
| [Hosted Matrix + local MindRoom](hosted-matrix.md) | Recommended and simplest: run only `uvx mindroom run` locally |
| [NixOS LXC (Incus)](https://github.com/mindroom-ai/lxc-nixos) | Give a MindRoom agent full freedom over its own persistent NixOS virtual machine while the host controls what it sees |
| [Sandbox Proxy Isolation](sandbox-proxy.md) | Run MindRoom locally while execution tools run in isolated workers |
| [Approved Egress](approved-egress.md) | Require static allowlists or human approval before Kubernetes workers reach external hostnames |
| Full Stack (Docker Compose) | All-in-one: bundled dashboard + Matrix (Tuwunel) + MindRoom client |
| [Docker (single container)](docker.md) | Single MindRoom runtime or when you already have Matrix |
| [Kubernetes](kubernetes.md) | Production clusters: a runtime with optional Tuwunel, client, and MatrixRTC charts, or the multi-tenant SaaS platform |
| [Trusted upstream browser auth](trusted-upstream-auth.md) | Hosted private agents behind an authenticated access layer |
| Direct | Development, simple setups |

## Bridges

Connect external messaging platforms to Matrix:

- [Bridges overview](bridges/index.md) - available bridges and how they work
- [Telegram bridge](bridges/telegram.md) - bridge Telegram chats via mautrix-telegram

## Google Services (Gmail/Calendar/Drive/Docs/Sheets/Tasks)

Use these guides if you want users to connect Google accounts in the MindRoom frontend:

- [Google Services OAuth (Admin Setup)](google-services-oauth.md) - optional custom setup for public and shared deployments
- [Google Services OAuth (Local Install)](google-services-user-oauth.md) - connect Google locally without Cloud setup

For private personal-agent tools, use the generic [OAuth Framework](../oauth-framework.md) and the Google Drive section in the individual setup guide.
For hosted multi-user private agents, also configure [Trusted Upstream Browser Auth](trusted-upstream-auth.md) so agent-issued OAuth links authenticate as the requester that triggered them.

## Quick Start

See [Install & First Run](../getting-started.md#hosted-matrix-local-mindroom-recommended) and [Docker (single container)](docker.md#docker-single-container).

### Kubernetes

See the [Kubernetes deployment guide](kubernetes.md) for Helm chart configuration.

## Required Configuration

Full stack:

```bash
# .env in the full stack repo
ANTHROPIC_API_KEY=sk-ant-...
# The default stack model uses Anthropic; selecting another provider also requires changing the model config.
```

Direct and single-container deployments:

1. **Matrix homeserver** - Set `MATRIX_HOMESERVER` and configure hosted provisioning, a registration/shared-secret token, or intentionally open registration for managed agent accounts
2. **Model credentials** - Configure credentials that match the selected provider, using an API key or a supported CLI login; containers must mount or provide the corresponding auth state
3. **Persistent storage** - Mount `mindroom_data/` to persist agent state (including `sessions/`, `learning/`, and memory data)

See the [Docker guide](docker.md#environment-variables) for the complete environment variable reference.

Hosted `mindroom.chat` deployments additionally use values from `mindroom connect` (`MINDROOM_LOCAL_CLIENT_ID`, `MINDROOM_LOCAL_CLIENT_SECRET`, and `MINDROOM_NAMESPACE`) to bootstrap agent registrations and avoid collisions on shared homeservers.
