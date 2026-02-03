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
mindroom run --config config.yaml --storage-path ./mindroom_data
```

### Docker

```bash
docker run -v ./config.yaml:/app/config.yaml \
           -v ./mindroom_data:/app/mindroom_data \
           ghcr.io/mindroom-ai/mindroom:latest
```

### Kubernetes

See the [Kubernetes deployment guide](kubernetes.md) for Helm chart configuration.

## Environment Variables

All deployments require these environment variables:

```bash
# Matrix credentials
MATRIX_HOMESERVER=https://matrix.example.com
MATRIX_USER_ID=@mindroom:example.com
MATRIX_ACCESS_TOKEN=your_token

# AI provider API keys
ANTHROPIC_API_KEY=sk-ant-...
# Add other providers as needed
```

## Persistent Storage

MindRoom stores data in the following structure:

```
mindroom_data/
├── sessions/      # Agent conversation history (SQLite)
├── memory/        # Vector store for memories (ChromaDB)
├── tracking/      # Response tracking
└── credentials/   # Synced credentials from .env
```

Ensure this directory is persisted across restarts.
