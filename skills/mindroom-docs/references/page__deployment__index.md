# Deployment

MindRoom can be deployed in various ways depending on your needs.

## Deployment Options

| Method                                                                                         | Best For                                                                                    |
| ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| [Hosted Matrix + local MindRoom](https://docs.mindroom.chat/deployment/hosted-matrix/index.md) | Simplest setup: run only `uvx mindroom run` locally                                         |
| [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md)        | Run MindRoom locally while `shell`, `file`, and `python` execute in isolated Docker workers |
| Full Stack (Docker Compose)                                                                    | All-in-one: bundled dashboard + Matrix (Tuwunel) + MindRoom client                          |
| [Docker (single container)](https://docs.mindroom.chat/deployment/docker/index.md)             | Single MindRoom runtime or when you already have Matrix                                     |
| [Kubernetes](https://docs.mindroom.chat/deployment/kubernetes/index.md)                        | Multi-tenant SaaS, production                                                               |
| Direct                                                                                         | Development, simple setups                                                                  |

## Bridges

Connect external messaging platforms to Matrix:

- [Bridges overview](https://docs.mindroom.chat/deployment/bridges/index.md) - available bridges and how they work
- [Telegram bridge](https://docs.mindroom.chat/deployment/bridges/telegram/index.md) - bridge Telegram chats via mautrix-telegram

## Google Services (Gmail/Calendar/Drive/Sheets)

Use these guides if you want users to connect Google accounts in the MindRoom frontend:

- [Google Services OAuth (Admin Setup)](https://docs.mindroom.chat/deployment/google-services-oauth/index.md) - one-time setup for shared/team deployments
- [Google Services OAuth (Individual Setup)](https://docs.mindroom.chat/deployment/google-services-user-oauth/index.md) - single-user bring-your-own OAuth app setup

## Quick Start

### Hosted Matrix + local MindRoom (simplest)

```
# Creates ~/.mindroom/config.yaml and ~/.mindroom/.env by default
uvx mindroom config init --profile public
$EDITOR ~/.mindroom/.env
uvx mindroom connect --pair-code ABCD-EFGH
uvx mindroom run
```

Generate the pair code in `https://chat.mindroom.chat` under: `Settings -> Local MindRoom`.

See [Hosted Matrix deployment](https://docs.mindroom.chat/deployment/hosted-matrix/index.md) for the full walkthrough. If you want `shell`, `file`, or `python` to run in dedicated Docker workers on the same machine, see [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md).

### Full Stack (recommended)

```
git clone https://github.com/mindroom-ai/mindroom-stack
cd mindroom-stack
cp .env.example .env
$EDITOR .env  # add at least one AI provider key

docker compose up -d
```

The stack exposes MindRoom at `http://localhost:8765`, the MindRoom client at `http://localhost:8080`, and Matrix at `http://localhost:8008`. The stack uses published `mindroom`, `mindroom-cinny`, and `mindroom-tuwunel` images by default. If you access it from another device, set `CLIENT_HOMESERVER_URL=http://<host-ip>:8008` in `.env` before starting it.

### Direct (Development)

```
mindroom run --storage-path ./mindroom_data
```

The config file path is set via `MINDROOM_CONFIG_PATH` and otherwise defaults to `./config.yaml`, then `~/.mindroom/config.yaml`.

If you want local Matrix + Cinny with a host-installed MindRoom runtime (Linux/macOS), use:

```
mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix
mindroom run --storage-path ./mindroom_data
```

### Docker (single container)

```
docker run -d \
  --name mindroom \
  -p 8765:8765 \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./mindroom_data:/app/mindroom_data \
  --env-file .env \
  ghcr.io/mindroom-ai/mindroom:latest
```

See the [Docker deployment guide](https://docs.mindroom.chat/deployment/docker/index.md) for the full single-container setup.

### Kubernetes

See the [Kubernetes deployment guide](https://docs.mindroom.chat/deployment/kubernetes/index.md) for Helm chart configuration.

## Required Configuration

Full stack:

```
# .env in the full stack repo
OPENAI_API_KEY=sk-...
# Add other providers as needed
```

Direct and single-container deployments:

1. **Matrix homeserver** - Set `MATRIX_HOMESERVER` (must allow open registration for agent accounts)
1. **AI provider keys** - At least one of `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, etc.
1. **Persistent storage** - Mount `mindroom_data/` to persist agent state (including `sessions/`, `learning/`, and memory data)

See the [Docker guide](https://docs.mindroom.chat/deployment/docker/#environment-variables) for the complete environment variable reference.

Hosted `mindroom.chat` deployments additionally use values from `mindroom connect` (`MINDROOM_LOCAL_CLIENT_ID`, `MINDROOM_LOCAL_CLIENT_SECRET`, and `MINDROOM_NAMESPACE`) to bootstrap agent registrations and avoid collisions on shared homeservers.
