---
icon: lucide/cloud
---

# Deployment

MindRoom can be deployed in various ways depending on your needs.

## Deployment Options

| Method | Best For |
|--------|----------|
| [Hosted Matrix + local backend](hosted-matrix.md) | Simplest setup: run only `uvx mindroom run` locally |
| Full Stack (Docker Compose) | All-in-one: backend + frontend + Matrix (Synapse) + Element |
| [Docker (single container)](docker.md) | Backend-only or when you already have Matrix |
| [Kubernetes](kubernetes.md) | Multi-tenant SaaS, production |
| Direct | Development, simple setups |

## Bridges

Connect external messaging platforms to Matrix:

- [Bridges overview](bridges/index.md) - available bridges and how they work
- [Telegram bridge](bridges/telegram.md) - bridge Telegram chats via mautrix-telegram

## Google Services (Gmail/Calendar/Drive/Sheets)

Use these guides if you want users to connect Google accounts in the MindRoom frontend:

- [Google Services OAuth (Admin Setup)](google-services-oauth.md) - one-time setup for shared/team deployments
- [Google Services OAuth (Individual Setup)](google-services-user-oauth.md) - single-user bring-your-own OAuth app setup

## Quick Start

### Hosted Matrix + local backend (simplest)

```bash
mkdir -p ~/mindroom-local
cd ~/mindroom-local
uvx mindroom config init --profile public
$EDITOR .env
uvx mindroom connect --pair-code ABCD-EFGH
uvx mindroom run
```

Generate the pair code in `https://chat.mindroom.chat` under:
`Settings -> Local MindRoom`.

See [Hosted Matrix deployment](hosted-matrix.md) for the full walkthrough.

### Full Stack (recommended)

```bash
git clone https://github.com/mindroom-ai/mindroom-stack
cd mindroom-stack
cp .env.example .env
$EDITOR .env  # add at least one AI provider key

docker compose up -d
```

### Direct (Development)

```bash
mindroom run --storage-path ./mindroom_data
```

The config file path is set via `MINDROOM_CONFIG_PATH` (defaults to `./config.yaml`).

If you want local Matrix + Cinny with a host-installed backend (Linux/macOS), use:

```bash
mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix
mindroom run --storage-path ./mindroom_data
```

### Docker (single container)

```bash
docker run -d \
  --name mindroom-backend \
  -p 8765:8765 \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./mindroom_data:/app/mindroom_data \
  --env-file .env \
  ghcr.io/mindroom-ai/mindroom-backend:latest
```

See the [Docker deployment guide](docker.md) for full setup including the frontend.

### Kubernetes

See the [Kubernetes deployment guide](kubernetes.md) for Helm chart configuration.

## Required Configuration

Full stack:

```bash
# .env in the full stack repo
ANTHROPIC_API_KEY=sk-ant-...
# Add other providers as needed
```

Direct and single-container deployments:

1. **Matrix homeserver** - Set `MATRIX_HOMESERVER` (must allow open registration for agent accounts)
2. **AI provider keys** - At least one of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.
3. **Persistent storage** - Mount `mindroom_data/` to persist agent state (including `sessions/`, `learning/`, and memory data)

See the [Docker guide](docker.md#environment-variables) for the complete environment variable reference.

Hosted `mindroom.chat` deployments additionally use local provisioning credentials from `mindroom connect` (`MINDROOM_LOCAL_CLIENT_ID` and `MINDROOM_LOCAL_CLIENT_SECRET`) to bootstrap agent registrations.
