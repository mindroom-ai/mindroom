# MindRoom Deployment Guide

## Quick Start

### Prerequisites
- Docker and Docker Compose installed
- Python 3.12+ installed
- API keys for LLM providers (OpenAI, Anthropic, etc.)
- Optional for HTTPS/domain routing: a Traefik container attached to the external Docker network `mynetwork`
- Without Traefik, `./deploy.py start` still exposes localhost ports, but `https://{DOMAIN}`, `https://m-{DOMAIN}`, Authelia, and Matrix `.well-known` routes stay unavailable

### Using the Instance Manager

The `deploy` script manages multiple MindRoom instances with optional Matrix server integration.
All commands below are run from `local/instances/deploy/`.

## Basic Commands

### 1. Create an Instance

```bash
cd local/instances/deploy

# Basic instance (no Matrix server, no auth)
./deploy.py create myapp

# Instance with production-ready authentication (Authelia)
./deploy.py create myapp --auth authelia

# Instance with lightweight Tuwunel Matrix server
./deploy.py create myapp --matrix tuwunel

# Instance with full Synapse Matrix server (PostgreSQL + Redis)
./deploy.py create myapp --matrix synapse

# Instance with custom domain and authentication
./deploy.py create myapp --domain myapp.example.com --auth authelia

# Full setup: Matrix + Authentication
./deploy.py create myapp --domain myapp.example.com --matrix tuwunel --auth authelia
```

### 2. Configure Your Instance

After creating an instance, edit the generated `envs/{instance_name}.env` file:

```bash
# Edit the environment file
nano envs/myapp.env

# Add your API keys:
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
# etc.
```

### 3. Start Your Instance

```bash
./deploy.py start myapp
```

This will start:
- MindRoom on its bundled dashboard/API port (automatically assigned, e.g., 8765)
- Matrix server if enabled (port automatically assigned, e.g., 8448)
- Authelia authentication server if enabled
- PostgreSQL and Redis (if using Synapse)

### 4. Access Your Instance

After starting, these direct host-port endpoints are available immediately:
- **MindRoom**: `http://localhost:{MINDROOM_PORT}` (e.g., `http://localhost:8765`)
- **Matrix Server** (if enabled): `http://localhost:{MATRIX_PORT}` (e.g., `http://localhost:8448`)

If Traefik is already attached to `mynetwork`, these HTTPS/domain routes also work:
- **MindRoom Domain**: `https://{DOMAIN}`
- **Matrix Domain** (if enabled): `https://m-{DOMAIN}`
- **Auth Portal** (if enabled): `https://auth-{DOMAIN}`

To find your ports:
```bash
./deploy.py list
```

### 5. Stop Your Instance

```bash
./deploy.py stop myapp
```

### 6. Remove an Instance

```bash
# Stop and remove containers, but keep data
./deploy.py stop myapp

# Fully remove instance (including data)
./deploy.py remove myapp
```

## Managing Multiple Instances

### List All Instances
```bash
./deploy.py list
```

Output:
```
                              MindRoom Instances
┏━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ Name    ┃  Status   ┃   MindRoom ┃   Matrix ┃ Domain    ┃ Data       ┃
┡━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ prod    │ ● running │         8765 │ 8448 (S) │ prod.com  │ ./instance…│
│ dev     │ ○ stopped │         8766 │ 8449 (T) │ dev.local │ ./instance…│
│ test    │ ● running │         8767 │     none │ test.local│ ./instance…│
└─────────┴───────────┴──────────────┴──────────┴───────────┴────────────┘

(S) = Synapse, (T) = Tuwunel
```

### Running Multiple Instances Simultaneously
```bash
# Create and start production instance with Synapse
./deploy.py create prod --domain prod.mindroom.com --matrix synapse
nano envs/prod.env  # Add API keys
./deploy.py start prod

# Create and start development instance with Tuwunel
./deploy.py create dev --domain dev.mindroom.com --matrix tuwunel
nano envs/dev.env  # Add API keys
./deploy.py start dev

# Create and start test instance without Matrix
./deploy.py create test
nano envs/test.env  # Add API keys
./deploy.py start test

# All three instances now running on different ports
./deploy.py list
```

## Matrix Server Options

### Tuwunel (Lightweight, Rust-based)
- **When to use**: Development, small deployments, resource-constrained environments
- **Resources**: ~100MB RAM
- **Command**: `--matrix tuwunel`
- **Features**: Fast, minimal, perfect for development

### Synapse (Full-featured)
- **When to use**: Production, large deployments, when you need all Matrix features
- **Resources**: ~500MB+ RAM, PostgreSQL, Redis
- **Command**: `--matrix synapse`
- **Features**: Complete Matrix spec implementation, battle-tested

### No Matrix
- **When to use**: When you only need MindRoom without chat features
- **Command**: (default, no flag needed)
- **Features**: Just MindRoom on the bundled dashboard/API port

## Testing Your Matrix Server

After starting an instance with Matrix:

```bash
# Basic Matrix client API smoke test
curl -fsS http://localhost:<MATRIX_PORT>/_matrix/client/versions
```

## Port Management

Ports are automatically assigned and tracked:
- **MindRoom**: Starts at 8765, increments for each instance
- **Matrix**: Starts at 8448, increments for each instance

The instance manager ensures no port conflicts.

## Data Storage

Each instance has its own data directory:
```
local/instances/deploy/instance_data/
├── myapp/
│   ├── config/       # MindRoom configuration
│   ├── tmp/          # Temporary files
│   ├── logs/         # Application logs
│   ├── synapse/      # Synapse data (if using Synapse)
│   ├── tuwunel/      # Tuwunel data (if using Tuwunel)
│   ├── postgres/     # PostgreSQL data (if using Synapse)
│   └── redis/        # Redis data (if using Synapse)
└── another-instance/
    └── ...
```

## Troubleshooting

### Instance Won't Start
1. Check if ports are already in use: `docker ps`
2. Check logs: `docker logs {instance_name}-mindroom`
3. Ensure `envs/{instance_name}.env` has valid API keys
4. Try stopping and starting again

### Port Conflicts
```bash
# Check what's using a port
lsof -i :8765

# Force stop all containers
docker stop $(docker ps -q)
```

### Clean Up Everything
```bash
# Stop and remove all managed instances
./deploy.py remove --all --force

# Remove all Docker resources
docker system prune -a
```

### Matrix Server Issues

#### Synapse Permission Issues
If Synapse fails with permission errors:
```bash
# If not, files might need proper ownership
ls -la local/instances/deploy/instance_data/{instance_name}/synapse/
```

#### Tuwunel Connection Issues
Tuwunel should work out of the box. Check:
```bash
docker logs {instance_name}-tuwunel
```

## How It Works

### Instance Registry
- `instances.json` - Tracks all instances, ports, and configuration
- Automatically manages port allocation (no conflicts!)
- Port allocation starts at: MindRoom (8765), Matrix (8448)

### Docker Compose Structure
The system uses parameterized Docker Compose files:
- `docker-compose.yml` - Base MindRoom services (runtime + bundled dashboard/API)
- `docker-compose.tuwunel.yml` - Adds the MindRoom Tuwunel fork (`ghcr.io/mindroom-ai/mindroom-tuwunel:latest`)
- `docker-compose.synapse.yml` - Adds the MindRoom Synapse fork (`ghcr.io/mindroom-ai/mindroom-synapse:develop`) with PostgreSQL and Redis

Container names use `${INSTANCE_NAME}` prefix to avoid conflicts.

### Direct Docker Compose Usage
You can also use Docker Compose directly:
```bash
# From project root
docker compose --env-file local/instances/deploy/envs/myapp.env \
  -f local/instances/deploy/docker-compose.yml \
  -f local/instances/deploy/docker-compose.tuwunel.yml \
  -f local/instances/deploy/docker-compose.wellknown.yml \
  -p myapp up -d
```

## Environment Variables

Each `envs/{instance_name}.env` file contains:

### Required (add these yourself)
```env
# LLM API Keys
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
GROQ_API_KEY=...

# Optional services
DEEPSEEK_API_KEY=
OPENROUTER_API_KEY=
OLLAMA_HOST=
```

### Auto-generated (set by deploy.py)
```env
# Instance configuration
INSTANCE_NAME=myapp
MINDROOM_PORT=8765
DATA_DIR=/absolute/path/to/instance_data/myapp
INSTANCE_DOMAIN=myapp.localhost

# Matrix configuration (if enabled)
MATRIX_PORT=8448
MATRIX_SERVER_NAME=m-myapp.localhost
```

## Examples

### Development Setup
```bash
# Create a dev instance with all features
./deploy.py create dev --matrix tuwunel
echo "OPENAI_API_KEY=sk-..." >> envs/dev.env
echo "ANTHROPIC_API_KEY=sk-ant-..." >> envs/dev.env
./deploy.py start dev

# Access at the MindRoom port shown by ./deploy.py list
```

### Production Setup
```bash
# Create production instance with Synapse
./deploy.py create prod \
  --domain mindroom.example.com \
  --matrix synapse

# Configure with production API keys
nano envs/prod.env

# Start the instance
./deploy.py start prod

# Attach Traefik to mynetwork before relying on the HTTPS/domain routes above.
# The provided compose files use Traefik labels, not nginx configuration.
./deploy.py list
```

### Testing Setup
```bash
# Quick test instance without Matrix
./deploy.py create test
nano envs/test.env
./deploy.py start test
# Run tests...
./deploy.py remove test  # Clean up
```
