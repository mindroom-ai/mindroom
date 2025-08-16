# Mindroom Federation - Simple Multi-Instance Setup

## Overview
This is the simplest possible federation setup for Mindroom. Each instance runs independently with its own ports and data directory. Matrix handles all inter-instance communication.

## Quick Start

### 1. Create an instance
```bash
# Basic instance (no Matrix)
./instance_manager.py create myinstance --domain mydomain.com

# Instance with Tuwunel Matrix server
./instance_manager.py create myinstance --domain mydomain.com --matrix
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
# Production instance
python instance_manager.py create prod prod.mindroom.com
python instance_manager.py start prod

# Development instance
python instance_manager.py create dev dev.mindroom.com
python instance_manager.py start dev

# Personal instance
python instance_manager.py create personal john.mindroom.com
python instance_manager.py start personal
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

## Matrix Integration with Tuwunel

### What is Tuwunel?
Tuwunel is a lightweight, high-performance Matrix homeserver written in Rust. It's perfect for Mindroom federation because:
- Minimal resource usage compared to Synapse
- Easy Docker deployment
- Full Matrix specification support
- Each instance gets its own isolated Matrix server

### Matrix Options for Instances

#### Option 1: With Tuwunel (--matrix flag)
Each instance gets its own Tuwunel Matrix server:
```bash
./instance_manager.py create prod --domain prod.mindroom.com --matrix
```
- Isolated Matrix server per instance
- No shared dependencies
- Perfect for true federation
- First user automatically becomes admin

#### Option 2: Without Matrix (default)
Instance runs without Matrix integration:
```bash
./instance_manager.py create dev --domain dev.mindroom.com
```
- Lighter weight deployment
- Good for instances that don't need chat features

#### Option 3: External Matrix Server
Configure `.env.{instance}` to point to existing Matrix server:
- Set custom `MATRIX_HOMESERVER` URL
- Use existing Synapse or other Matrix server
- Share Matrix infrastructure across instances

### Accessing Your Tuwunel Server
Once started with --matrix, your Tuwunel server is available at:
- Client API: `http://localhost:{MATRIX_PORT}/_matrix/client/`
- Federation (if enabled): `https://{domain}:8448/`

Use any Matrix client (Element, FluffyChat, etc.) to connect:
- Server: `http://localhost:{MATRIX_PORT}` or `https://{domain}`
- First user to register becomes admin automatically

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
