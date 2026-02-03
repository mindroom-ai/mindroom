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

Note: The config file path is set via the `MINDROOM_CONFIG_PATH` environment variable (defaults to `./config.yaml`).

### Docker

MindRoom consists of two containers: backend (bot + API) and frontend (dashboard). For quick local testing with just the backend:

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

## Environment Variables

All deployments require these environment variables:

```bash
# Matrix homeserver (must allow open registration for agent accounts)
MATRIX_HOMESERVER=https://matrix.example.com

# Optional: for self-signed certificates (development only)
# MATRIX_SSL_VERIFY=false

# Optional: for federation setups where hostname differs from server_name
# MATRIX_SERVER_NAME=example.com

# AI provider API keys (set the ones you use in config.yaml)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
# GOOGLE_API_KEY=...
# OPENROUTER_API_KEY=...
# DEEPSEEK_API_KEY=...

# Ollama (for local models)
# OLLAMA_HOST=http://localhost:11434

# Optional settings
# ENABLE_AI_CACHE=true           # Enable AI response caching (default: true)
# MINDROOM_ENABLE_STREAMING=true # Enable streaming responses (default: true)
# BACKEND_PORT=8765              # Port for the API server
```

!!! note "Matrix Registration"
    MindRoom automatically creates Matrix user accounts for each agent.
    Your Matrix homeserver must allow open registration.

## Persistent Storage

MindRoom stores data in the following structure:

```
mindroom_data/
├── sessions/          # Agent conversation history (SQLite)
├── memory/            # Vector store for memories (ChromaDB)
├── tracking/          # Response tracking to avoid duplicates
├── credentials/       # Synced credentials from .env
├── logs/              # Application logs
├── matrix_state.yaml  # Matrix connection state
└── encryption_keys/   # Matrix E2EE keys (if enabled)
```

Ensure this directory is persisted across restarts. The `STORAGE_PATH` environment variable controls the location (defaults to `mindroom_data`).
