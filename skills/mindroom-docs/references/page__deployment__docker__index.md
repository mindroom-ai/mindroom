# Docker Deployment

Deploy MindRoom using Docker for simple, containerized deployments.

## Quick Start

MindRoom consists of two containers:

- **Backend**: The bot orchestrator and API server (port 8765)
- **Frontend**: The dashboard UI (port 8080)

For a minimal setup with just the backend:

```
docker run -d \
  --name mindroom-backend \
  -p 8765:8765 \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./mindroom_data:/app/mindroom_data \
  --env-file .env \
  ghcr.io/basnijholt/mindroom-backend:latest
```

## Docker Compose

Create a `docker-compose.yml`:

```
services:
  mindroom:
    image: ghcr.io/basnijholt/mindroom-backend:latest
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
      - STORAGE_PATH=/app/mindroom_data
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

| Variable                    | Description                                      | Default                 |
| --------------------------- | ------------------------------------------------ | ----------------------- |
| `MATRIX_HOMESERVER`         | Matrix server URL                                | `http://localhost:8008` |
| `MATRIX_SSL_VERIFY`         | Verify SSL certificates                          | `true`                  |
| `MATRIX_SERVER_NAME`        | Server name for federation (optional)            | -                       |
| `STORAGE_PATH`              | Data storage directory                           | `mindroom_data`         |
| `LOG_LEVEL`                 | Logging level                                    | `INFO`                  |
| `MINDROOM_CONFIG_PATH`      | Path to config.yaml (alternative: `CONFIG_PATH`) | Package default         |
| `MINDROOM_ENABLE_STREAMING` | Enable streaming responses                       | `true`                  |
| `ANTHROPIC_API_KEY`         | Anthropic API key (if using Claude models)       | -                       |
| `OPENAI_API_KEY`            | OpenAI API key (if using OpenAI models)          | -                       |

## Building from Source

Build from the repository root:

```
docker build -t mindroom:dev -f local/instances/deploy/Dockerfile.backend .
```

The Dockerfile uses a multi-stage build with `uv` for dependency management and runs as a non-root user (UID 1000).

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

## Health Checks

The container exposes a health endpoint on port 8765:

```
curl http://localhost:8765/api/health
```

## Data Persistence

MindRoom stores data in the `mindroom_data` directory:

- `sessions/` - Per-agent conversation history (SQLite)
- `learning/` - Per-agent Agno Learning state (SQLite, persistent across restarts)
- `memory/` - Vector store for agent/room memories
- `tracking/` - Response tracking to avoid duplicates
- `credentials/` - Synchronized secrets from `.env`
- `logs/` - Application logs
- `matrix_state.yaml` - Matrix connection state
- `encryption_keys/` - Matrix E2EE keys (if enabled)

## Full Stack with Frontend

For a complete deployment including the dashboard:

```
services:
  backend:
    image: ghcr.io/basnijholt/mindroom-backend:latest
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
      - STORAGE_PATH=/app/mindroom_data

  frontend:
    image: ghcr.io/basnijholt/mindroom-frontend:latest
    container_name: mindroom-frontend
    restart: unless-stopped
    ports:
      - "8080:8080"
    depends_on:
      - backend
```

> [!NOTE] The frontend image is built with `VITE_API_URL=""` (empty), meaning it uses relative URLs and expects `/api/*` requests to be proxied to the backend. If you also use the OpenAI-compatible API through the same domain, proxy `/v1/*` to the backend as well. For standalone deployments without a reverse proxy, rebuild the frontend image with `VITE_API_URL=http://localhost:8765`.
>
> [!TIP] For production, use a reverse proxy (Traefik, Nginx) to serve both services under the same domain. See `local/instances/deploy/docker-compose.yml` for an example with Traefik labels.
