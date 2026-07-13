# Deployment

MindRoom can be deployed in various ways depending on your needs.

## Deployment Options

| Method | Best For |
|--------|----------|
| [Hosted Matrix + local MindRoom](https://docs.mindroom.chat/deployment/hosted-matrix/) | Recommended and simplest: run only `uvx mindroom run` locally |
| [NixOS LXC (Incus)](https://github.com/mindroom-ai/lxc-nixos) | Give a MindRoom agent full freedom over its own persistent NixOS virtual machine while the host controls what it sees |
| [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/) | Run MindRoom locally while execution tools run in isolated workers |
| [Approved Egress](https://docs.mindroom.chat/deployment/approved-egress/) | Require static allowlists or human approval before Kubernetes workers reach external hostnames |
| Full Stack (Docker Compose) | All-in-one: bundled dashboard + Matrix (Tuwunel) + MindRoom client |
| [Docker (single container)](https://docs.mindroom.chat/deployment/docker/) | Single MindRoom runtime or when you already have Matrix |
| [Kubernetes](https://docs.mindroom.chat/deployment/kubernetes/) | Multi-tenant SaaS, production |
| [Trusted upstream browser auth](https://docs.mindroom.chat/deployment/trusted-upstream-auth/) | Hosted private agents behind an authenticated access layer |
| Direct | Development, simple setups |

## Bridges

Connect external messaging platforms to Matrix:

- [Bridges overview](https://docs.mindroom.chat/deployment/bridges/) - available bridges and how they work
- [Telegram bridge](https://docs.mindroom.chat/deployment/bridges/telegram/) - bridge Telegram chats via mautrix-telegram

## Google Services (Gmail/Calendar/Drive/Sheets)

Use these guides if you want users to connect Google accounts in the MindRoom frontend:

- [Google Services OAuth (Admin Setup)](https://docs.mindroom.chat/deployment/google-services-oauth/) - one-time setup for shared/team deployments
- [Google Services OAuth (Individual Setup)](https://docs.mindroom.chat/deployment/google-services-user-oauth/) - single-user bring-your-own OAuth app setup

For private personal-agent tools, use the generic [OAuth Framework](https://docs.mindroom.chat/oauth-framework/) and the Google Drive section in the individual setup guide.
For hosted multi-user private agents, also configure [Trusted Upstream Browser Auth](https://docs.mindroom.chat/deployment/trusted-upstream-auth/) so agent-issued OAuth links authenticate as the requester that triggered them.

## Quick Start

### Hosted Matrix + local MindRoom (recommended)

```bash
# Creates ~/.mindroom/config.yaml and ~/.mindroom/.env by default
uvx mindroom config init
$EDITOR ~/.mindroom/.env
uvx mindroom connect --pair-code ABCD-EFGH
uvx mindroom run
```

Generate the pair code in `https://chat.mindroom.chat` under:
`Settings -> Local MindRoom`.

See [Hosted Matrix deployment](https://docs.mindroom.chat/deployment/hosted-matrix/) for the full walkthrough.
If you want worker-routed execution tools like `coding`, `docker`, `file`, `python`, and `shell` to run in dedicated Docker workers on the same machine, see [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/).

### NixOS LXC container (preferred alternative, agent-controlled machine)

Use this when you want to give a MindRoom agent full freedom over its own virtual machine while you, from the host, control precisely what it can see.
The [mindroom-ai/lxc-nixos](https://github.com/mindroom-ai/lxc-nixos) flake provisions the virtual machine — an Incus LXC system container running NixOS — with the full MindRoom stack (MindRoom, Tuwunel Matrix homeserver, MindRoom Chat, Element, Caddy) plus Docker and `ragenix`-based secrets wiring, so the agent can rebuild and manage the persistent system it runs on — unlike the mostly stateless Docker Compose stack below — without ever touching the host.
It is slightly harder to set up by hand, but asking a coding agent such as Codex or Claude Code to do it is trivial: the repo ships machine-oriented instructions in `AGENTS.md`.
It requires a Linux host running [Incus](https://linuxcontainers.org/incus/docs/main/installing/); see the repo README for the full setup.

```bash
git clone https://github.com/mindroom-ai/lxc-nixos.git
cd lxc-nixos
incus launch images:nixos/unstable mindroom -c security.nesting=true
incus config device add mindroom repo disk source="$PWD" path=/mnt/repo shift=true
```

### Full Stack Docker Compose (all-local alternative)

```bash
git clone https://github.com/mindroom-ai/mindroom-stack
cd mindroom-stack
cp .env.example .env
$EDITOR .env  # add at least one AI provider key

docker compose up -d
```

The stack exposes MindRoom at `http://localhost:8765`, the MindRoom client at `http://localhost:8080`, and Matrix at `http://localhost:8008`.
The stack uses published `mindroom`, `mindroom-chat`, and `mindroom-tuwunel` images by default.
If you access it from another device, set `CLIENT_HOMESERVER_URL=http://<host-ip>:8008` in `.env` before starting it.

### Direct (Development)

```bash
mindroom run --storage-path ./mindroom_data
```

The config file path is set via `MINDROOM_CONFIG_PATH` and otherwise defaults to `./config.yaml`, then `~/.mindroom/config.yaml`.

If you want local Matrix + MindRoom Chat with a host-installed MindRoom runtime (Linux/macOS), use:

```bash
mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix
mindroom run --storage-path ./mindroom_data
```

### Docker (single container)

```bash
docker run -d \
  --name mindroom \
  -p 8765:8765 \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./mindroom_data:/app/mindroom_data \
  --env-file .env \
  ghcr.io/mindroom-ai/mindroom:latest
```

See the [Docker deployment guide](https://docs.mindroom.chat/deployment/docker/) for the full single-container setup.

### Kubernetes

See the [Kubernetes deployment guide](https://docs.mindroom.chat/deployment/kubernetes/) for Helm chart configuration.

## Required Configuration

Full stack:

```bash
# .env in the full stack repo
OPENAI_API_KEY=sk-...
# Add other providers as needed
```

Direct and single-container deployments:

1. **Matrix homeserver** - Set `MATRIX_HOMESERVER` (must allow open registration for agent accounts)
2. **AI provider keys** - At least one of `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, etc.
3. **Persistent storage** - Mount `mindroom_data/` to persist agent state (including `sessions/`, `learning/`, and memory data)

See the [Docker guide](https://docs.mindroom.chat/deployment/docker/#environment-variables) for the complete environment variable reference.

Hosted `mindroom.chat` deployments additionally use values from `mindroom connect` (`MINDROOM_LOCAL_CLIENT_ID`, `MINDROOM_LOCAL_CLIENT_SECRET`, and `MINDROOM_NAMESPACE`) to bootstrap agent registrations and avoid collisions on shared homeservers.
