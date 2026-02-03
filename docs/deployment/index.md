---
icon: lucide/cloud
---

# Deployment

MindRoom can be deployed in various ways depending on your needs.

## Deployment Options

| Method | Best For |
|--------|----------|
| [Docker](docker.md) | Single-instance deployments, development |
| [Kubernetes](kubernetes.md) | Multi-tenant SaaS, production |
| Direct | Development, simple setups |

## Quick Start

### Direct (Development)

```bash
mindroom run --storage-path ./mindroom_data
```

The config file path is set via `MINDROOM_CONFIG_PATH` (defaults to `./config.yaml`).

### Docker

```bash
docker run -d \
  --name mindroom-backend \
  -p 8765:8765 \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./mindroom_data:/app/mindroom_data \
  --env-file .env \
  ghcr.io/basnijholt/mindroom-backend:latest
```

See the [Docker deployment guide](docker.md) for full setup including the frontend.

### Kubernetes

See the [Kubernetes deployment guide](kubernetes.md) for Helm chart configuration.

## Required Configuration

All deployments need:

1. **Matrix homeserver** - Set `MATRIX_HOMESERVER` (must allow open registration for agent accounts)
2. **AI provider keys** - At least one of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.
3. **Persistent storage** - Mount `mindroom_data/` to persist agent state

See the [Docker guide](docker.md#environment-variables) for the complete environment variable reference.
