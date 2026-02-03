---
icon: lucide/container
---

# Docker Deployment

Deploy MindRoom using Docker for simple, containerized deployments.

## Quick Start

```bash
docker run -d \
  --name mindroom \
  -v ./config.yaml:/app/config.yaml \
  -v ./mindroom_data:/app/mindroom_data \
  -v ./.env:/app/.env \
  ghcr.io/mindroom-ai/mindroom:latest
```

## Docker Compose

Create a `docker-compose.yml`:

```yaml
services:
  mindroom:
    image: ghcr.io/mindroom-ai/mindroom:latest
    container_name: mindroom
    restart: unless-stopped
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./mindroom_data:/app/mindroom_data
      - ./.env:/app/.env:ro
    environment:
      - MATRIX_HOMESERVER=${MATRIX_HOMESERVER}
      - MATRIX_USER_ID=${MATRIX_USER_ID}
      - MATRIX_ACCESS_TOKEN=${MATRIX_ACCESS_TOKEN}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
```

Run with:

```bash
docker compose up -d
```

## Building from Source

```bash
docker build -t mindroom:dev -f local/instances/deploy/Dockerfile.backend .
```

## With Local Matrix

For development, run MindRoom alongside a local Matrix server:

```bash
# Start Matrix + Postgres
cd local/matrix && docker compose up -d

# Start MindRoom
docker compose up -d
```

## Health Checks

The container exposes a health endpoint:

```bash
curl http://localhost:8765/api/health
```
