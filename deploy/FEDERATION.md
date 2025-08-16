# Mindroom Federation - Simple Multi-Instance Setup

## Overview
This is the simplest possible federation setup for Mindroom. Each instance runs independently with its own ports and data directory. Matrix handles all inter-instance communication.

## Quick Start

### 1. Create an instance
```bash
# Basic instance (no Matrix)
./instance_manager.py create myinstance --domain mydomain.com

# Instance with Tuwunel Matrix server (lightweight, Rust-based)
./instance_manager.py create myinstance --domain mydomain.com --matrix tuwunel

# Instance with Synapse Matrix server (full-featured, with PostgreSQL + Redis)
./instance_manager.py create myinstance --domain mydomain.com --matrix synapse
```

### 2. Configure the instance
Edit `.env.myinstance` and add your API keys.

### 3. Start the instance
```bash
./instance_manager.py start myinstance
```

### 4. List all instances
```bash
./instance_manager.py list
```

### 5. Stop an instance
```bash
./instance_manager.py stop myinstance
```

### Get help
```bash
./instance_manager.py --help
./instance_manager.py create --help
```

## How It Works

### Instance Registry
- `instances.json` - Tracks all instances, ports, and configuration
- Automatically manages port allocation (no conflicts!)
- Simple JSON file, easy to edit manually if needed

### Environment Variables
Each instance gets its own `.env.{name}` file with:
- `INSTANCE_NAME` - Unique instance identifier
- `BACKEND_PORT` - Backend API port
- `FRONTEND_PORT` - Frontend web UI port
- `DATA_DIR` - Instance data directory
- `INSTANCE_DOMAIN` - Domain for Traefik routing
- `MATRIX_PORT` - Tuwunel Matrix server port (if --matrix enabled)
- `MATRIX_SERVER_NAME` - Matrix server name (usually same as domain)

### Docker Compose
The `docker-compose.yml` is fully parameterized:
- Container names: `${INSTANCE_NAME}-backend`, `${INSTANCE_NAME}-frontend`
- Ports: `${BACKEND_PORT}`, `${FRONTEND_PORT}`
- Volumes: `${DATA_DIR}/config`, `${DATA_DIR}/tmp`, `${DATA_DIR}/logs`
- Traefik labels: Uses `${INSTANCE_NAME}` and `${INSTANCE_DOMAIN}`

### Data Isolation
Each instance has its own:
- Configuration directory
- Temporary files directory
- Logs directory
- Database/storage

## Examples

### Run multiple instances on one server
```bash
# Production instance with Synapse (full Matrix)
./instance_manager.py create prod --domain prod.mindroom.com --matrix synapse
./instance_manager.py start prod

# Development instance with Tuwunel (lightweight Matrix)
./instance_manager.py create dev --domain dev.mindroom.com --matrix tuwunel
./instance_manager.py start dev

# Personal instance without Matrix
./instance_manager.py create personal --domain john.mindroom.com
./instance_manager.py start personal
```

### Check what's running
```bash
python instance_manager.py list
docker ps | grep mindroom
```

## Port Allocation
- Backend ports start at 8765 and increment
- Frontend ports start at 3003 and increment
- Matrix ports start at 8448 and increment (if enabled)
- Automatically tracked in `instances.json`
- No manual port management needed!

## Matrix Server Options

### Choose Your Matrix Server

#### Option 1: Tuwunel (--matrix tuwunel)
Lightweight, high-performance Matrix homeserver written in Rust:
```bash
./instance_manager.py create prod --domain prod.mindroom.com --matrix tuwunel
```
- **Pros**: Minimal resource usage, fast, simple setup
- **Cons**: Newer, less ecosystem support
- **Best for**: Small to medium deployments, resource-constrained environments
- **Resources**: ~100MB RAM, minimal CPU

#### Option 2: Synapse (--matrix synapse)
Full-featured, production-ready Matrix homeserver with PostgreSQL and Redis:
```bash
./instance_manager.py create prod --domain prod.mindroom.com --matrix synapse
```
- **Pros**: Battle-tested, full Matrix spec, extensive ecosystem
- **Cons**: Higher resource usage, more complex
- **Best for**: Large deployments, production environments
- **Resources**: ~500MB+ RAM, PostgreSQL + Redis

#### Option 3: No Matrix (default)
Instance runs without Matrix integration:
```bash
./instance_manager.py create dev --domain dev.mindroom.com
```
- **Best for**: Instances that don't need chat features
- **Lighter weight**: No Matrix overhead

#### Option 4: External Matrix Server
Configure `.env.{instance}` to point to existing Matrix server:
- Set custom `MATRIX_HOMESERVER` URL in env file
- Share Matrix infrastructure across instances

### Accessing Your Matrix Server
Once started, your Matrix server is available at:
- Client API: `http://localhost:{MATRIX_PORT}/_matrix/client/`
- Federation (if enabled): `https://{domain}:8448/`

Use any Matrix client (Element, FluffyChat, etc.) to connect:
- Server: `http://localhost:{MATRIX_PORT}` or `https://{domain}`
- Tuwunel: First user to register becomes admin automatically
- Synapse: Configure admin users via homeserver.yaml

## Inter-Instance Communication
With Tuwunel, instances can communicate through:
- Direct Matrix federation (if MATRIX_ALLOW_FEDERATION=true)
- Shared rooms on same server
- Bridge connections between servers

## Future Enhancements (kept simple!)
- [ ] Instance health checks
- [ ] Backup/restore commands
- [ ] Instance migration between servers
- [ ] Shared Redis for certain data
- [ ] Direct instance-to-instance API

## Troubleshooting

### Port already in use
The instance manager tracks ports, but if you manually used a port:
1. Edit `instances.json`
2. Add the port to `allocated_ports`
3. Create new instance (will skip that port)

### Instance won't start
Check:
1. `.env.{name}` file exists and has API keys
2. Docker is running
3. No port conflicts with other services
4. Data directory permissions

### Clean up an instance
```bash
python instance_manager.py stop myinstance
rm -rf /mnt/data/myinstance
rm .env.myinstance
# Then edit instances.json to remove the instance
```
