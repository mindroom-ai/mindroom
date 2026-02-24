# Deployment

MindRoom can be deployed in various ways depending on your needs.

## Deployment Options

| Method                                                                             | Best For                                                    |
| ---------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Full Stack (Docker Compose)                                                        | All-in-one: backend + frontend + Matrix (Synapse) + Element |
| [Docker (single container)](https://docs.mindroom.chat/deployment/docker/index.md) | Backend-only or when you already have Matrix                |
| [Kubernetes](https://docs.mindroom.chat/deployment/kubernetes/index.md)            | Multi-tenant SaaS, production                               |
| Direct                                                                             | Development, simple setups                                  |

## Bridges

Connect external messaging platforms to Matrix:

- [Bridges overview](https://docs.mindroom.chat/deployment/bridges/index.md) - available bridges and how they work
- [Telegram bridge](https://docs.mindroom.chat/deployment/bridges/telegram/index.md) - bridge Telegram chats via mautrix-telegram

## Google Services (Gmail/Calendar/Drive/Sheets)

Use these guides if you want users to connect Google accounts in the MindRoom frontend:

- [Google Services OAuth (Admin Setup)](https://docs.mindroom.chat/deployment/google-services-oauth/index.md) - one-time setup for shared/team deployments
- [Google Services OAuth (Individual Setup)](https://docs.mindroom.chat/deployment/google-services-user-oauth/index.md) - single-user bring-your-own OAuth app setup

## Quick Start

### Full Stack (recommended)

```
git clone https://github.com/mindroom-ai/mindroom-stack
cd mindroom-stack
cp .env.example .env
$EDITOR .env  # add at least one AI provider key

docker compose up -d
```

### Direct (Development)

```
mindroom run --storage-path ./mindroom_data
```

The config file path is set via `MINDROOM_CONFIG_PATH` (defaults to `./config.yaml`).

If you want local Matrix + Cinny with a host-installed backend (Linux/macOS), use:

```
mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix
mindroom run --storage-path ./mindroom_data
```

### Docker (single container)

```
docker run -d \
  --name mindroom-backend \
  -p 8765:8765 \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./mindroom_data:/app/mindroom_data \
  --env-file .env \
  ghcr.io/mindroom-ai/mindroom-backend:latest
```

See the [Docker deployment guide](https://docs.mindroom.chat/deployment/docker/index.md) for full setup including the frontend.

### Kubernetes

See the [Kubernetes deployment guide](https://docs.mindroom.chat/deployment/kubernetes/index.md) for Helm chart configuration.

## Required Configuration

Full stack:

```
# .env in the full stack repo
ANTHROPIC_API_KEY=sk-ant-...
# Add other providers as needed
```

Direct and single-container deployments:

1. **Matrix homeserver** - Set `MATRIX_HOMESERVER` (must allow open registration for agent accounts)
1. **AI provider keys** - At least one of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.
1. **Persistent storage** - Mount `mindroom_data/` to persist agent state (including `sessions/`, `learning/`, and memory data)

See the [Docker guide](https://docs.mindroom.chat/deployment/docker/#environment-variables) for the complete environment variable reference.
