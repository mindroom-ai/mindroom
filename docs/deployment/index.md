---
icon: lucide/cloud
---

# Deployment

This page helps you choose how to run MindRoom and lists what every deployment needs.
Each option links to the page with its setup steps.

## Deployment Options

| Method | Best For |
|--------|----------|
| [Your computer + MindRoom Chat](hosted-matrix.md) | Recommended and simplest: run only `uvx mindroom run` locally |
| [NixOS LXC (Incus)](../getting-started.md#advanced-agent-managed-nixos-container) | Give a MindRoom agent full freedom over its own persistent NixOS machine while the host controls what it sees |
| [Full Stack (Docker Compose)](../getting-started.md#full-stack-docker-compose) | All-in-one: bundled dashboard + Matrix (Tuwunel) + MindRoom client |
| [Docker (single container)](docker.md) | Single MindRoom runtime when you already have Matrix |
| [Direct install](../getting-started.md#manual-install-with-your-own-matrix-homeserver) | Development and simple setups with your own Matrix homeserver |
| [Kubernetes](kubernetes.md) | Production clusters: a runtime with optional Tuwunel, client, and MatrixRTC charts, or the multi-tenant SaaS platform |
| [Sandbox Proxy Isolation](sandbox-proxy.md) | Run MindRoom while execution tools run in isolated workers |
| [Approved Egress](approved-egress.md) | Require static allowlists or human approval before Kubernetes workers reach external hostnames |
| [Trusted upstream browser auth](trusted-upstream-auth.md) | Hosted multi-user private agents behind an authenticated access layer |

## Required Configuration

The full stack needs only a model provider key in its `.env`; see [Full Stack Docker Compose](../getting-started.md#full-stack-docker-compose).

Direct and single-container deployments need:

1. **Matrix homeserver** - Set `MATRIX_HOMESERVER` and give MindRoom a way to create agent accounts (see [Matrix account provisioning](../getting-started.md#matrix-account-provisioning)).
2. **Model credentials** - Configure credentials for the selected provider, using an API key or a supported CLI login (see [Model Configuration](../configuration/models.md#environment-variables)); containers must mount or provide the matching auth state.
3. **Persistent storage** - Mount the storage directory (`mindroom_data/` by default) so agent state survives restarts (see [Data Storage](storage.md#data-persistence)).

Hosted `mindroom.chat` installs also need the credentials that pairing saves; see [Hosted Matrix](hosted-matrix.md#what-pairing-saves).
Every environment variable is listed in [Configuration — Environment Variables](../configuration/index.md#environment-variables).

## Related Setup

- [Bridges](bridges/index.md) - connect Telegram and other messaging platforms to Matrix.
- [Connect Google Accounts](google-services-user-oauth.md) - Google tools on a paired local install, with no Google Cloud setup.
- [Google OAuth Apps & Scopes](google-services-oauth.md) - your own Google OAuth client for remote, public, or shared deployments.
- [Upgrade Notes](upgrades.md) - required steps when upgrading, including the [Nio 1.0 cutover](upgrades.md#upgrading-to-nio-10).
