# MindRoom Deployment Guide

## Quick Start

### Prerequisites
- Docker and Docker Compose installed
- Python 3.12+ installed
- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed and available in your shell; `deploy.py` and `bridge.py` use its script launcher
- API keys for LLM providers (OpenAI, Anthropic, etc.)
- Optional for HTTPS/domain routing: a Traefik container attached to the external Docker network `mynetwork`
- HTTPS/domain routes only work when Traefik exposes entrypoint names and a certresolver that match the instance labels.
- The defaults are `websecure`, `matrix-fed`, and `porkbun`.
- Override them per instance with `TRAEFIK_WEB_ENTRYPOINT`, `TRAEFIK_MATRIX_ENTRYPOINT`, and `TRAEFIK_CERTRESOLVER` in `envs/{instance_name}.env`.
- Without Traefik, `./deploy.py start` still exposes localhost ports, but `https://{DOMAIN}`, `https://m-{DOMAIN}`, Authelia, and Matrix `.well-known` routes stay unavailable.

### Using the Instance Manager

The `deploy` script manages multiple MindRoom instances with optional Matrix server integration.
All commands below are run from `local/instances/deploy/`.

## Basic Commands

### 1. Create an Instance

```bash
cd local/instances/deploy

# Basic instance (no Matrix server, no auth)
./deploy.py create myapp

# Instance with Authelia (account setup required before starting)
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

#### Authelia accounts (required with `--auth authelia`)

`create --auth authelia` copies an enabled public example `admin` account to `<data-dir>/authelia/users_database.yml` (by default, `instance_data/{instance_name}/authelia/users_database.yml`).
It does not generate a unique login password or prompt for user credentials.
Before running `start` or exposing the instance, configure intended users there: replace the example `admin` password hash and email, or remove/disable that account (`disabled: true`) after adding your own user.
Do not leave any enabled account using the public template credentials.
Full-instance `start` and `restart` refuse to launch while any enabled account still uses the public example hash, even if the account was renamed.
The check also recognizes equivalent Argon2 encodings, including YAML block-scalar newlines and LDAP hash prefixes.
Quote usernames that YAML interprets as non-string values, such as `"on"` or `"yes"`; launch checks reject non-string account keys.
Matrix-only launches (`--only-matrix`) skip this check because they do not start Authelia.

Generate a new password hash with the interactive prompt documented in [Authelia's password guide](https://www.authelia.com/reference/guides/passwords/):

```bash
docker run --rm -it authelia/authelia:latest authelia crypto hash generate argon2

# Edit the user database in the instance's data directory
nano instance_data/myapp/authelia/users_database.yml
```

Copy only the value after `Digest:` into the user's quoted `password` field, and set their `displayname`, `email`, and `groups`.
Use the data directory printed by `create` if you changed the default data location.
Complete this account setup and the required HTTPS/Traefik configuration before production use.

### 3. Start Your Instance

```bash
./deploy.py start myapp
```

This will start:
- MindRoom on its bundled dashboard/API port (automatically assigned, e.g., 8765)
- The sandbox runner used by the default shell, file, and Python tool routing
- A relay that forwards MindRoom's calls to the sandbox runner port
- Matrix server if enabled (port automatically assigned, e.g., 8448)
- Authelia authentication server if enabled
- PostgreSQL and Redis (if using Synapse)

Before starting the sandbox runner, Compose initializes its scratch volume ownership using `UID` and `GID` (both default to `1000`).

The sandbox runner joins only its own `sandbox-network`, so tool code cannot open connections to MindRoom, PostgreSQL, Redis, Authelia, or the homeserver over Docker networking.
The `sandbox-relay` container joins both networks, forwards only the runner port, caps connections per address, and does not route other traffic.
The runner keeps outbound internet access for package installs and web requests, so like any other client it can reach public routes and every port that any instance or other host service publishes on the host's interfaces.
`MINDROOM_API_KEY` protects the MindRoom API on those paths, and PostgreSQL and Redis publish no host ports.

### 4. Access Your Instance

After starting, these direct host-port endpoints are exposed on the host:
- **MindRoom**: `http://localhost:{MINDROOM_PORT}` (e.g., `http://localhost:8765`)
- **Matrix Server** (if enabled): `http://localhost:{MATRIX_PORT}` (e.g., `http://localhost:8448`)

The dashboard asks for the `MINDROOM_API_KEY` stored in `envs/{instance_name}.env`, including after an Authelia login, and API clients send it as a bearer token.
Containers on the shared `mynetwork` can reach the runtime by container name, so this key is what protects the dashboard from other instances on the same host.

The dashboard accepts browser changes only from its public origin, `https://{DOMAIN}` by default.
Without Traefik, set `MINDROOM_PUBLIC_URL=http://localhost:{MINDROOM_PORT}` in `envs/{instance_name}.env` and restart before editing through that port.

Some services, especially Synapse, can take a moment before they answer requests on those ports.

When your Traefik config matches the instance's `TRAEFIK_*` settings, these HTTPS/domain routes are published:
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

`remove` requests Docker Compose teardown with `down -v`, including named-volume removal, before deleting the instance data directory and environment file.
`stop` omits `-v` and keeps persistent data.

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

Core MindRoom and Matrix bind mounts use each instance's data directory (`DATA_DIR`):
```
local/instances/deploy/instance_data/
├── myapp/
│   ├── config/         # config.yaml mounted at /app/config.yaml
│   ├── mindroom_data/  # Persistent MindRoom state mounted at /app/mindroom_data
│   ├── logs/           # Mounted at /app/logs
│   ├── synapse/        # Synapse config and media mounted at /data (if enabled)
│   └── tuwunel/        # Tuwunel data mounted at /var/lib/tuwunel (if enabled)
└── another-instance/
    └── ...
```

Synapse's PostgreSQL and Redis data live in Docker named volumes, outside this directory:

| Docker volume | Container mount |
|---------------|-----------------|
| `<instance>-postgres-data` | `/var/lib/postgresql/data` |
| `<instance>-redis-data` | `/data` |

The setup helper also creates `postgres/` and `redis/` host directories, but these are not mounted into those services.
Backing up only the instance directory therefore omits the PostgreSQL and Redis volumes.
The shared sandbox also stores its workspace in the Compose-managed `sandbox-workspace` named volume.

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
ZAI_API_KEY=
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

# Random per-instance secrets
MINDROOM_API_KEY=...
MINDROOM_SANDBOX_PROXY_TOKEN=...
# Synapse only
POSTGRES_PASSWORD=...
REDIS_PASSWORD=...
```

`create` generates these values, and Synapse's `homeserver.yaml` receives the same PostgreSQL and Redis passwords.
Without `MINDROOM_SANDBOX_PROXY_TOKEN` the sandbox runner rejects every tool call, and without `MINDROOM_API_KEY` the MindRoom API accepts unauthenticated requests.

### Upgrading Instances Created by Older Versions

No manual steps are required.
`start` and `restart` add a random `MINDROOM_API_KEY` and `MINDROOM_SANDBOX_PROXY_TOKEN` to an env file that lacks them and print the file path, after which the dashboard asks for that key.
Compose then creates `sandbox-network` and the relay, recreates the runner on the new network, and reuses the existing `mindroom-network`, so attached bridges stay connected.
Synapse instances keep their existing PostgreSQL password and unauthenticated Redis, which the runner can no longer reach.
To enable Redis authentication on such an instance anyway, set one new value as `REDIS_PASSWORD` in the env file and as `redis.password` in `{DATA_DIR}/synapse/homeserver.yaml`, then restart it.
When you run Docker Compose directly with an older env file, run `./deploy.py start <name>` once or add random values for both `MINDROOM_API_KEY` and `MINDROOM_SANDBOX_PROXY_TOKEN` yourself.
Without the token the runner rejects every tool call, and without the API key tool code can call the MindRoom API through its published host port.

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
# Match its entrypoint and certresolver names to the instance's TRAEFIK_* settings.
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
