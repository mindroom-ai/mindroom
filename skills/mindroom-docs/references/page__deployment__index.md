# Deployment

This page helps you choose how to run MindRoom and lists what every deployment needs.
Each option links to the page with its setup steps.

## Deployment Options

| Method | Best For |
|--------|----------|
| [Hosted Matrix + local MindRoom](https://docs.mindroom.chat/deployment/hosted-matrix/) | Recommended and simplest: run only `uvx mindroom run` locally |
| [NixOS LXC (Incus)](https://docs.mindroom.chat/getting-started/#preferred-alternative-nixos-lxc-container-agent-controlled-machine) | Give a MindRoom agent full freedom over its own persistent NixOS machine while the host controls what it sees |
| [Full Stack (Docker Compose)](https://docs.mindroom.chat/getting-started/#alternative-full-stack-docker-compose) | All-in-one: bundled dashboard + Matrix (Tuwunel) + MindRoom client |
| [Docker (single container)](https://docs.mindroom.chat/deployment/docker/) | Single MindRoom runtime when you already have Matrix |
| [Direct install](https://docs.mindroom.chat/getting-started/#manual-install-with-your-own-matrix-homeserver) | Development and simple setups with your own Matrix homeserver |
| [Kubernetes](https://docs.mindroom.chat/deployment/kubernetes/) | Production clusters: a runtime with optional Tuwunel, client, and MatrixRTC charts, or the multi-tenant SaaS platform |
| [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/) | Run MindRoom while execution tools run in isolated workers |
| [Approved Egress](https://docs.mindroom.chat/deployment/approved-egress/) | Require static allowlists or human approval before Kubernetes workers reach external hostnames |
| [Trusted upstream browser auth](https://docs.mindroom.chat/deployment/trusted-upstream-auth/) | Hosted multi-user private agents behind an authenticated access layer |

## Required Configuration

The full stack needs only a model provider key in its `.env`; see [Full Stack Docker Compose](https://docs.mindroom.chat/getting-started/#alternative-full-stack-docker-compose).

Direct and single-container deployments need:

1. **Matrix homeserver** - Set `MATRIX_HOMESERVER` and give MindRoom a way to create agent accounts (see [Matrix account provisioning](https://docs.mindroom.chat/getting-started/#matrix-account-provisioning)).
2. **Model credentials** - Configure credentials for the selected provider, using an API key or a supported CLI login (see [Model Configuration](https://docs.mindroom.chat/configuration/models/#environment-variables)); containers must mount or provide the matching auth state.
3. **Persistent storage** - Mount the storage directory (`mindroom_data/` by default) so agent state survives restarts (see [Data Storage](https://docs.mindroom.chat/deployment/storage/#data-persistence)).

Hosted `mindroom.chat` installs also need the credentials that pairing saves; see [Hosted Matrix](https://docs.mindroom.chat/deployment/hosted-matrix/#what-pairing-saves).
Every environment variable is listed in [Configuration — Environment Variables](https://docs.mindroom.chat/configuration/#environment-variables).

## Related Setup

- [Bridges](https://docs.mindroom.chat/deployment/bridges/) - connect Telegram and other messaging platforms to Matrix.
- [Connect Google Accounts](https://docs.mindroom.chat/deployment/google-services-user-oauth/) - Google tools on a paired local install, with no Google Cloud setup.
- [Google OAuth Apps & Scopes](https://docs.mindroom.chat/deployment/google-services-oauth/) - your own Google OAuth client for remote, public, or shared deployments.
- [Upgrade Notes](https://docs.mindroom.chat/deployment/upgrades/) - required steps when upgrading, including the [Nio 1.0 cutover](https://docs.mindroom.chat/deployment/upgrades/#upgrading-to-nio-10).
