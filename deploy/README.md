# Mindroom Deployment Guide

## Quick Start

### Prerequisites
- Docker and Docker Compose installed
- Python 3.10+ installed
- API keys for LLM providers (OpenAI, Anthropic, etc.)

### Using the Instance Manager

The `deploy` script manages multiple Mindroom instances with optional Matrix server integration.

## Basic Commands

### 1. Create an Instance

```bash
cd deploy

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

After creating an instance, edit the generated `.env.{instance_name}` file:

```bash
# Edit the environment file
nano .env.myapp

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
- Mindroom backend (port automatically assigned, e.g., 8765)
- Mindroom frontend (port automatically assigned, e.g., 3003)
- Matrix server if enabled (port automatically assigned, e.g., 8448)
- Authelia authentication server if enabled (with Redis for sessions)
- PostgreSQL and Redis (if using Synapse)

### 4. Access Your Instance

After starting, your instance will be available at:
- **Frontend**: `http://localhost:{FRONTEND_PORT}` (e.g., `http://localhost:3005`)
- **Backend API**: `http://localhost:{BACKEND_PORT}` (e.g., `http://localhost:8765`)
- **Matrix Server** (if enabled): `http://localhost:{MATRIX_PORT}` (e.g., `http://localhost:8448`)
- **Auth Portal** (if enabled): `https://auth-{DOMAIN}` (e.g., `https://auth-myapp.example.com`)

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
                              Mindroom Instances
┏━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ Name    ┃  Status   ┃ Backend ┃ Frontend ┃   Matrix ┃ Domain    ┃ Data       ┃
┡━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ prod    │ ● running │    8765 │     3005 │ 8448 (S) │ prod.com  │ ./instance…│
│ dev     │ ○ stopped │    8766 │     3006 │ 8449 (T) │ dev.local │ ./instance…│
│ test    │ ● running │    8767 │     3007 │     none │ test.local│ ./instance…│
└─────────┴───────────┴─────────┴──────────┴──────────┴───────────┴────────────┘

(S) = Synapse, (T) = Tuwunel
```

### Running Multiple Instances Simultaneously
```bash
# Create and start production instance with Synapse
./deploy.py create prod --domain prod.mindroom.com --matrix synapse
nano .env.prod  # Add API keys
./deploy.py start prod

# Create and start development instance with Tuwunel
./deploy.py create dev --domain dev.mindroom.com --matrix tuwunel
nano .env.dev  # Add API keys
./deploy.py start dev

# Create and start test instance without Matrix
./deploy.py create test
nano .env.test  # Add API keys
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
- **When to use**: When you only need Mindroom without chat features
- **Command**: (default, no flag needed)
- **Features**: Just Mindroom backend and frontend

## Testing Your Matrix Server

After starting an instance with Matrix:

```bash
# Test the Matrix server (requires requests library)
cd ..  # Go to project root
source .venv/bin/activate
python deploy/test_matrix.py <MATRIX_PORT> <SERVER_TYPE>

# Examples:
python deploy/test_matrix.py 8448 Tuwunel
python deploy/test_matrix.py 8450 Synapse
```

## Port Management

Ports are automatically assigned and tracked:
- **Backend**: Starts at 8765, increments for each instance
- **Frontend**: Starts at 3005, increments for each instance (internal port always 3003)
- **Matrix**: Starts at 8448, increments for each instance

The instance manager ensures no port conflicts.

## Data Storage

Each instance has its own data directory:
```
deploy/instance_data/
├── myapp/
│   ├── config/       # Mindroom configuration
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
2. Check logs: `docker logs {instance_name}-backend`
3. Ensure `.env.{instance_name}` has valid API keys
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
# Stop all instances
./deploy.py reset

# Remove all Docker resources
docker system prune -a
```

### Matrix Server Issues

#### Synapse Permission Issues
If Synapse fails with permission errors:
```bash
# The entrypoint.sh script should handle this automatically
# If not, files might need proper ownership
ls -la instance_data/{instance_name}/synapse/
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
- Port allocation starts at: Backend (8765), Frontend (3005), Matrix (8448)

### Docker Compose Structure
The system uses parameterized Docker Compose files:
- `docker-compose.yml` - Base Mindroom services (backend, frontend)
- `docker-compose.tuwunel.yml` - Adds Tuwunel Matrix server
- `docker-compose.synapse.yml` - Adds Synapse with PostgreSQL and Redis

Container names use `${INSTANCE_NAME}` prefix to avoid conflicts.

### Direct Docker Compose Usage
You can also use Docker Compose directly:
```bash
# From project root
docker compose --env-file deploy/.env.myapp \
  -f deploy/docker-compose.yml \
  -f deploy/docker-compose.tuwunel.yml \
  -p myapp up -d
```

## Environment Variables

Each `.env.{instance_name}` file contains:

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
BACKEND_PORT=8765
FRONTEND_PORT=3005
DATA_DIR=/absolute/path/to/instance_data/myapp
INSTANCE_DOMAIN=myapp.localhost

# Matrix configuration (if enabled)
MATRIX_PORT=8448
MATRIX_SERVER_NAME=myapp.localhost
```

## Examples

### Development Setup
```bash
# Create a dev instance with all features
./deploy.py create dev --matrix tuwunel
echo "OPENAI_API_KEY=sk-..." >> .env.dev
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env.dev
./deploy.py start dev

# Access at http://localhost:3004
```

### Production Setup
```bash
# Create production instance with Synapse
./deploy.py create prod \
  --domain mindroom.example.com \
  --matrix synapse

# Configure with production API keys
nano .env.prod

# Start the instance
./deploy.py start prod

# Set up reverse proxy (nginx, etc.) to ports shown in:
./deploy.py list
```

### Testing Setup
```bash
# Quick test instance without Matrix
./deploy.py create test
cp .env.template .env.test  # Use template
./deploy.py start test
# Run tests...
./deploy.py remove test  # Clean up
```
