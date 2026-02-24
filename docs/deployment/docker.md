---
icon: lucide/container
---

# Docker Deployment

Deploy MindRoom using Docker for simple, containerized deployments.

## Quick Start

MindRoom consists of two containers:

- **Backend**: The bot orchestrator and API server (port 8765)
- **Frontend**: The dashboard UI (port 8080)

For a minimal setup with just the backend:

```bash
docker run -d \
  --name mindroom-backend \
  -p 8765:8765 \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./mindroom_data:/app/mindroom_data \
  --env-file .env \
  ghcr.io/mindroom-ai/mindroom-backend:latest
```

## Docker Compose

Create a `docker-compose.yml`:

```yaml
services:
  mindroom:
    image: ghcr.io/mindroom-ai/mindroom-backend:latest
    container_name: mindroom
    restart: unless-stopped
    ports:
      - "8765:8765"
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./mindroom_data:/app/mindroom_data
    env_file:
      - .env
    environment:
      - MINDROOM_STORAGE_PATH=/app/mindroom_data
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
      - MATRIX_HOMESERVER=${MATRIX_HOMESERVER}
      # Optional: for self-signed certificates
      # - MATRIX_SSL_VERIFY=false
      # Optional: override server name for federation
      # - MATRIX_SERVER_NAME=example.com
```

Run with:

```bash
docker compose up -d
```

## Environment Variables

Key environment variables (set in `.env` or pass directly):

| Variable | Description | Default |
|----------|-------------|---------|
| `MATRIX_HOMESERVER` | Matrix server URL | `http://localhost:8008` |
| `MATRIX_SSL_VERIFY` | Verify SSL certificates | `true` |
| `MATRIX_SERVER_NAME` | Server name for federation (optional) | - |
| `MINDROOM_STORAGE_PATH` | Data storage directory | Relative to config file |
| `LOG_LEVEL` | Logging level | `INFO` |
| `MINDROOM_CONFIG_PATH` | Path to config.yaml | `./config.yaml`, then `~/.mindroom/config.yaml` |
| `ANTHROPIC_API_KEY` | Anthropic API key (if using Claude models) | - |
| `OPENAI_API_KEY` | OpenAI API key (if using OpenAI models) | - |
| `MINDROOM_API_KEY` | API key for dashboard auth (standalone) | - (open access) |

Streaming responses are configured in `config.yaml` via `defaults.enable_streaming` (default: `true`).

## Building from Source

Build from the repository root:

```bash
docker build -t mindroom:dev -f local/instances/deploy/Dockerfile.backend .
```

The Dockerfile uses a multi-stage build with `uv` for dependency management and runs as a non-root user (UID 1000).

A `Dockerfile.backend-minimal` variant is also available, which builds a smaller image without pre-installed tool extras -- useful for sandbox runners.

## With Local Matrix

For development, run MindRoom alongside a local Matrix server:

```bash
# Start Matrix (Synapse + Postgres + Redis)
cd local/matrix && docker compose up -d

# Verify Matrix is running
curl -s http://localhost:8008/_matrix/client/versions

# Start MindRoom using the docker-compose.yml you created above
docker compose up -d
```

The local Matrix stack includes:

- **Synapse**: Matrix homeserver on port 8008
- **PostgreSQL**: Database backend
- **Redis**: Caching layer

If you're running the backend on the host (not in Docker), you can use
`mindroom local-stack-setup` to start Synapse + MindRoom Cinny and persist local Matrix
env vars automatically:

```bash
mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix
mindroom run
```

## Health Checks

The container exposes a health endpoint on port 8765:

```bash
curl http://localhost:8765/api/health
```

## Data Persistence

MindRoom stores data in the `mindroom_data` directory:

- `sessions/` - Per-agent conversation history (SQLite)
- `learning/` - Per-agent Agno Learning state (SQLite, persistent across restarts)
- `chroma/` - ChromaDB vector store for agent/room memories
- `knowledge_db/` - Knowledge base vector stores
- `culture/` - Shared culture state
- `tracking/` - Response tracking to avoid duplicates
- `credentials/` - Synchronized secrets from `.env`
- `logs/` - Application logs
- `matrix_state.yaml` - Matrix connection state
- `encryption_keys/` - Matrix E2EE keys (if enabled)

## Sandbox Proxy Isolation

When configured, `shell`, `file`, and `python` tool calls can be proxied to a separate **sandbox-runner** sidecar container. The sidecar runs the same image but without access to secrets, credentials, or the primary data volume. This provides real process-level isolation for code-execution tools. Without proxy configuration, all tools execute locally in the backend process.

See [Sandbox Proxy Isolation](sandbox-proxy.md) for full documentation including Docker Compose examples, Kubernetes sidecar setup, host-machine-with-container mode, credential leases, and environment variable reference.

## Full Stack with Frontend

For a complete deployment including the dashboard:

```yaml
services:
  backend:
    image: ghcr.io/mindroom-ai/mindroom-backend:latest
    container_name: mindroom-backend
    restart: unless-stopped
    ports:
      - "8765:8765"
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./mindroom_data:/app/mindroom_data
    env_file:
      - .env
    environment:
      - MINDROOM_STORAGE_PATH=/app/mindroom_data

  frontend:
    image: ghcr.io/mindroom-ai/mindroom-frontend:latest
    container_name: mindroom-frontend
    restart: unless-stopped
    ports:
      - "8080:8080"
    depends_on:
      - backend
```

> [!NOTE]
> The frontend image is built with `VITE_API_URL=""` (empty), meaning it uses relative URLs and expects `/api/*` requests to be proxied to the backend. If you also use the OpenAI-compatible API through the same domain, proxy `/v1/*` to the backend as well. For standalone deployments without a reverse proxy, rebuild the frontend image with `VITE_API_URL=http://localhost:8765`.

> [!TIP]
> For production, use a reverse proxy (Traefik, Nginx) to serve both services under the same domain. See `local/instances/deploy/docker-compose.yml` for an example with Traefik labels.
