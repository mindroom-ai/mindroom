# Docker Deployment

Deploy MindRoom using Docker for simple, containerized deployments.

## Quick Start

MindRoom ships as a single runtime container that serves:

- the bot orchestrator
- the dashboard UI at `http://localhost:8765`
- the dashboard API at `http://localhost:8765/api`
- the OpenAI-compatible API at `http://localhost:8765/v1`

Run it with:

```
docker run -d \
  --name mindroom \
  -p 8765:8765 \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./mindroom_data:/app/mindroom_data \
  --env-file .env \
  ghcr.io/mindroom-ai/mindroom:latest
```

## Docker Compose

Create a `docker-compose.yml`:

```
services:
  mindroom:
    image: ghcr.io/mindroom-ai/mindroom:latest
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

```
docker compose up -d
```

## Environment Variables

Key environment variables (set in `.env` or pass directly):

| Variable                | Description                                                                                                                                                       | Default                                         |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| `MATRIX_HOMESERVER`     | Matrix server URL                                                                                                                                                 | `http://localhost:8008`                         |
| `MATRIX_SSL_VERIFY`     | Verify SSL certificates                                                                                                                                           | `true`                                          |
| `MATRIX_SERVER_NAME`    | Server name for federation (optional)                                                                                                                             | -                                               |
| `MINDROOM_STORAGE_PATH` | Data storage directory                                                                                                                                            | Relative to config file                         |
| `LOG_LEVEL`             | Logging level                                                                                                                                                     | `INFO`                                          |
| `MINDROOM_CONFIG_PATH`  | Path to config.yaml                                                                                                                                               | `./config.yaml`, then `~/.mindroom/config.yaml` |
| `ANTHROPIC_API_KEY`     | Anthropic API key (if using Claude models)                                                                                                                        | -                                               |
| `OPENAI_API_KEY`        | OpenAI API key (if using OpenAI models)                                                                                                                           | -                                               |
| `MINDROOM_PORT`         | Port used by Google OAuth callback URL construction and deployment tooling. Does **not** change the API server bind port — use `mindroom run --api-port` for that | `8765`                                          |
| `MINDROOM_API_KEY`      | API key for dashboard auth (standalone)                                                                                                                           | - (open access)                                 |

To change the API server port or bind address, pass `--api-port` or `--api-host` to the `mindroom run` command. For example, add `command: ["mindroom", "run", "--api-port", "9000"]` to the Docker Compose service.

Streaming responses are configured in `config.yaml` via `defaults.enable_streaming` (default: `true`).

If `MINDROOM_API_KEY` is set, the browser dashboard will prompt for the key via a same-origin login page before loading the UI.

## Building from Source

Build from the repository root:

```
docker build -t mindroom:dev -f local/instances/deploy/Dockerfile.mindroom .
```

The Dockerfile uses a multi-stage build with `uv` for dependency management and runs as a non-root user (UID 1000).

A `Dockerfile.mindroom-minimal` variant is also available, which builds a smaller image without pre-installed tool extras -- useful for sandbox runners.

## With Local Matrix

For development, run MindRoom alongside a local Matrix server:

```
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

If you're running the backend on the host (not in Docker), you can use `mindroom local-stack-setup` to start Synapse + MindRoom Cinny and persist local Matrix env vars automatically:

```
mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix
mindroom run
```

## Health Checks

The container exposes a health endpoint on port 8765:

```
curl http://localhost:8765/api/health
```

## Data Persistence

MindRoom stores data in the `mindroom_data` directory:

- `sessions/` - Per-agent conversation history (SQLite)
- `learning/` - Per-agent Agno Learning state (SQLite, persistent across restarts)
- `chroma/` - ChromaDB vector store for agent/team memories
- `knowledge_db/` - Knowledge base vector stores
- `culture/` - Shared culture state
- `tracking/` - Response tracking to avoid duplicates
- `credentials/` - Synchronized secrets from `.env`
- `logs/` - Application logs
- `matrix_state.yaml` - Matrix connection state
- `encryption_keys/` - Matrix E2EE keys (if enabled)

## Sandbox Proxy Isolation

When configured, `shell`, `file`, and `python` tool calls can be proxied to a separate **sandbox-runner** sidecar container. The sidecar runs the same image but without access to secrets, credentials, or the primary data volume. This provides real process-level isolation for code-execution tools. Without proxy configuration, all tools execute locally in the MindRoom process.

See [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/index.md) for full documentation including Docker Compose examples, Kubernetes shared-sidecar and dedicated-worker modes, host-machine-with-container mode, credential leases, and environment variable reference.

> [!TIP] For production, use a reverse proxy (Traefik, Nginx) in front of the MindRoom container when you want TLS, host routing, or additional auth layers. See `local/instances/deploy/docker-compose.yml` for an example with Traefik labels.
