# Docker Deployment

Deploy MindRoom using Docker for simple, containerized deployments.

## Quick Start

MindRoom ships as a single runtime container that serves:

- the bot orchestrator
- the dashboard UI at `http://localhost:8765`
- the dashboard API at `http://localhost:8765/api`
- the OpenAI-compatible API at `http://localhost:8765/v1`

MindRoom may atomically rewrite `config.yaml` at startup when an automatic configuration migration is required.
Before starting an upgraded container with a pre-membership access config, run `mindroom config migrate --path ./config.yaml` on the host because a single-file bind mount cannot be replaced atomically.
Current configs can remain read-only as shown below.

Create the host data directory before starting either container recipe below.
The image runs as UID/GID `1000:1000`, so the bind-mounted directory and any existing contents must be writable by that container identity.
For a new data directory on Linux with rootful Docker and no user-namespace remapping:

```bash
mkdir -p ./mindroom_data
sudo chown 1000:1000 ./mindroom_data
```

For rootless Docker or user-namespace remapping, grant access using the corresponding host UID/GID mapping or suitable ACLs.
A bind mount hides the image directory's ownership, so the image's preconfigured permissions do not make an unwritable host directory usable.

Run it with:

```bash
docker run -d \
  --name mindroom \
  -p 8765:8765 \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./mindroom_data:/app/mindroom_data \
  --env-file .env \
  ghcr.io/mindroom-ai/mindroom:latest
```

### Docker (single container)

Create `./mindroom_data` and grant write access to the container's UID/GID `1000:1000` before running this command; follow the [Docker guide's storage preparation](https://docs.mindroom.chat/deployment/docker/#quick-start) for your Docker user mapping.

```bash
docker run -d \
  --name mindroom \
  -p 8765:8765 \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./mindroom_data:/app/mindroom_data \
  --env-file .env \
  ghcr.io/mindroom-ai/mindroom:latest
```

Before using this read-only single-file mount with a pre-membership access config, run `mindroom config migrate --path ./config.yaml` on the host.

See the [Docker deployment guide](https://docs.mindroom.chat/deployment/docker/) for the full single-container setup.

## Docker Compose

Prepare the writable `mindroom_data` directory as described above before starting this Compose service.
Create a `docker-compose.yml`:

```yaml
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

```bash
docker compose up -d
```

## Environment Variables

Key environment variables (set in `.env` or pass directly):

| Variable | Description | Default |
|----------|-------------|---------|
| `MATRIX_HOMESERVER` | Matrix server URL | `http://localhost:8008` |
| `MATRIX_SSL_VERIFY` | Verify the TLS certificate of `MATRIX_HOMESERVER`; provisioning service requests and other homeservers are always verified | `true` |
| `MATRIX_SERVER_NAME` | Server name for federation (optional) | - |
| `MINDROOM_STORAGE_PATH` | Data storage directory | Relative to config file |
| `MINDROOM_SESSION_STORAGE_PATH` | Dedicated root for agent and team session SQLite databases. Mount this path separately when sessions need their own persistent volume | `MINDROOM_STORAGE_PATH` |
| `LOG_LEVEL` | Logging level | `INFO` |
| `MINDROOM_LOGGER_LEVELS` | Optional per-logger overrides, for example `mindroom:DEBUG,httpx:WARNING,httpcore:WARNING,anthropic:INFO,nio:WARNING`; set `nio.crypto:WARNING` to inspect Matrix crypto decrypt warnings | - |
| `MINDROOM_CONFIG_PATH` | Path to config.yaml | `./config.yaml`, then `~/.mindroom/config.yaml` |
| `ANTHROPIC_API_KEY` | Anthropic API key (if using Claude models) | - |
| `OPENAI_API_KEY` | OpenAI API key (if using OpenAI models) | - |
| `MINDROOM_PORT` | Port used by Google OAuth callback URL construction and deployment tooling. Does **not** change the API server bind port — use `mindroom run --api-port` for that | `8765` |
| `MINDROOM_API_KEY` | API key for dashboard auth (standalone) | - (open access) |

To change the API server port or bind address, pass `--api-port` or `--api-host` to the `mindroom run` command.
For example, add `command: ["mindroom", "run", "--api-port", "9000"]` to the Docker Compose service.

Streaming responses are configured in `config.yaml` via `defaults.enable_streaming` (default: `true`).

If `MINDROOM_API_KEY` is set, the browser dashboard will prompt for the key via a same-origin login page before loading the UI.

## Building from Source

Build from the repository root:

```bash
docker build -t mindroom:dev -f local/instances/deploy/Dockerfile.mindroom .
```

The Dockerfile uses a multi-stage build with `uv` for dependency management and runs as a non-root user (UID 1000).

A `Dockerfile.mindroom-minimal` variant is also available, which builds a smaller image without pre-installed tool extras -- useful for sandbox runners.

## With Local Matrix

For development, run MindRoom alongside a local Matrix server:

```bash
# Start Matrix (Synapse + Postgres + Redis) without changing directories
docker compose -f local/matrix/docker-compose.yml up -d

# Verify Matrix is running
curl -s http://localhost:8008/_matrix/client/versions

# Start MindRoom using the docker-compose.yml you created above.
# Its MATRIX_HOMESERVER must resolve from inside the MindRoom container.
docker compose up -d
```

The two Compose projects do not share a network automatically.
Either attach both services to one named external network and use the Synapse service name, such as `http://mindroom-synapse:8008`, or configure an explicit host-gateway address.
Do not use `http://localhost:8008` inside the MindRoom container; there, `localhost` means the MindRoom container itself.

The local Matrix stack includes:

- **Synapse**: Matrix homeserver on port 8008
- **PostgreSQL**: Database backend
- **Redis**: Caching layer

For a host-installed backend, use `mindroom local-stack-setup` with the core MindRoom checkout's `local/matrix` directory to start Synapse + MindRoom Chat and persist local Matrix env vars automatically:

```bash
mindroom local-stack-setup --synapse-dir /path/to/mindroom/local/matrix
mindroom run
```

## Health Checks

The container exposes a health endpoint on port 8765:

```bash
curl http://localhost:8765/api/health
```

An unpaired hosted container answers `/api/health` with `200` while it waits for pairing approval, so a health check does not restart it and replace its pairing code.
During that wait, `/api/ready` returns `503` with `"detail": "Waiting for local pairing approval"`.
After approval, the port is briefly closed while the full API server takes it over, as at any startup, and `/api/ready` then reports normal startup progress until MindRoom is ready.
Find the approval link in the container logs.

See [Data Persistence](https://docs.mindroom.chat/deployment/storage/#data-persistence) and [Sandbox Proxy Isolation](https://docs.mindroom.chat/deployment/sandbox-proxy/#sandbox-proxy-isolation_1).
