# Mindroom Federation - Simple Multi-Instance Setup

## Overview
This is the simplest possible federation setup for Mindroom. Each instance runs independently with its own ports and data directory. Matrix handles all inter-instance communication.

## Quick Start

### 1. Create an instance
```bash
python instance_manager.py create myinstance mydomain.com
```

### 2. Configure the instance
Edit `.env.myinstance` and add your API keys.

### 3. Start the instance
```bash
python instance_manager.py start myinstance
```

### 4. List all instances
```bash
python instance_manager.py list
```

### 5. Stop an instance
```bash
python instance_manager.py stop myinstance
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
- Automatically tracked in `instances.json`
- No manual port management needed!

## Inter-Instance Communication
Currently handled entirely through Matrix federation. Each instance can:
- Join the same Matrix rooms
- Communicate through Matrix messages
- Share data through Matrix state events

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
