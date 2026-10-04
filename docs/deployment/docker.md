---
icon: lucide/container
---

# Docker Deployment

Run MindRoom as a single container when you already have a Matrix homeserver.
For a hosted homeserver with only MindRoom local, see [Hosted Matrix](hosted-matrix.md); for other options, see [Deployment](index.md).

The `ghcr.io/mindroom-ai/mindroom:latest` image runs `mindroom run` and serves, on port `8765`:

- the bot orchestrator
- the dashboard UI at `http://localhost:8765`
- the dashboard API at `http://localhost:8765/api`
- the OpenAI-compatible API at `http://localhost:8765/v1`

## Docker (single container)

### Prepare the data directory

The container runs as UID/GID `1000:1000`, so the host data directory and everything in it must be writable by that identity.
For a new directory on Linux with rootful Docker and no user-namespace remapping:

```bash
mkdir -p ./mindroom_data
sudo chown 1000:1000 ./mindroom_data
```

For rootless Docker or user-namespace remapping, grant access to the host UID/GID that container UID 1000 maps to, or use suitable ACLs.

See [Data Persistence](storage.md#data-persistence) for what this directory holds and what to back up.

### Run the container

```bash
docker run -d \
  --name mindroom \
  -p 8765:8765 \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./mindroom_data:/app/mindroom_data \
  --env-file .env \
  ghcr.io/mindroom-ai/mindroom:latest
```

Remove `:ro` from the config mount if you want configuration changes made from the dashboard or chat saved to `config.yaml`.
When upgrading a config that still uses retired access fields, follow [Membership Access Migration](upgrades.md#membership-access-migration) on the host before starting the container.

## Docker Compose

Prepare `./mindroom_data` as described [above](#prepare-the-data-directory), then create `docker-compose.yml`:

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
```

Start it with:

```bash
docker compose up -d
```

## Environment Variables

Pass variables with `--env-file .env` or the Compose `env_file:` and `environment:` keys.
For what the container needs, see [Required Configuration](index.md#required-configuration).
Set `MATRIX_SSL_VERIFY=false` for a homeserver with a self-signed certificate, and set `MINDROOM_API_KEY` to require a key for the dashboard instead of leaving it open.
All variables, defaults, and which ones only the process environment can set are listed in [Configuration — Environment Variables](../configuration/index.md#environment-variables).

To change the API port or bind address, pass `--api-port` or `--api-host` to `mindroom run`, for example `command: ["mindroom", "run", "--api-port", "9000"]` in the Compose service, and update the published port to match.
`MINDROOM_PORT` does not change the bind port.

## With Local Matrix

For development, start the repository's local Matrix stack (Synapse on port 8008, with PostgreSQL and Redis) and then MindRoom:

```bash
docker compose -f local/matrix/docker-compose.yml up -d
curl -s http://localhost:8008/_matrix/client/versions
docker compose up -d
```

`MATRIX_HOMESERVER` must resolve from inside the MindRoom container, and the two Compose projects do not share a network automatically.
Attach both services to one named external network and use the Synapse service name, such as `http://mindroom-synapse:8008`, or configure an explicit host-gateway address.
Do not use `http://localhost:8008` inside the MindRoom container, because there `localhost` is the MindRoom container itself.

To run the backend on the host instead of in a container, use [`mindroom local-stack-setup`](../cli.md#local-stack-setup) to start Synapse and MindRoom Chat and save the Matrix variables:

```bash
mindroom local-stack-setup --synapse-dir /path/to/mindroom/local/matrix
mindroom run
```

## Health Checks

```bash
curl http://localhost:8765/api/health
curl http://localhost:8765/api/ready
```

Use `/api/health` for liveness and `/api/ready` for readiness; responses are described in [Health & Readiness](operational-log-events.md#health-readiness).
A hosted container waiting for pairing approval is live but not ready; find the approval link with `docker logs mindroom`.

## Building from Source

Build from the repository root:

```bash
docker build -t mindroom:dev -f local/instances/deploy/Dockerfile.mindroom .
```

`local/instances/deploy/Dockerfile.mindroom-minimal`, published as `ghcr.io/mindroom-ai/mindroom-minimal`, builds a smaller image without pre-installed tool extras, useful for sandbox runners; tools install their dependencies when first used.
To run execution tools in isolated workers, see [Sandbox Proxy Isolation](sandbox-proxy.md).
