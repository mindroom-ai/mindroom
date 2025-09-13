# Bridge Manager for Mindroom Instances

The `bridge.py` script provides a unified interface for managing Matrix bridges across Mindroom instances. It follows the same design patterns as `deploy.py` for consistency.

## Prerequisites

1. **Mindroom Instance with Matrix**: Create an instance with Matrix support first:
   ```bash
   ./deploy.py create my-instance --matrix tuwunel
   ./deploy.py start my-instance
   ```

2. **Bridge Credentials**: Obtain necessary credentials for each bridge type (see below)

## Quick Start

### Add a Telegram Bridge

```bash
# Add and configure Telegram bridge
./bridge.py add telegram --instance my-instance \
  --api-id YOUR_API_ID \
  --api-hash YOUR_API_HASH \
  --bot-token YOUR_BOT_TOKEN

# Generate registration file
./bridge.py register telegram --instance my-instance

# For Tuwunel: Follow the manual registration steps shown
# For Synapse: Restart Matrix server after registration

# Start the bridge
./bridge.py start telegram --instance my-instance

# Check status
./bridge.py status --instance my-instance
```

## Commands

### `add` - Add and Configure a Bridge
```bash
./bridge.py add BRIDGE_TYPE [OPTIONS]
```
- **BRIDGE_TYPE**: `telegram`, `slack`, or `email`
- **Options**:
  - `--instance`: Target Mindroom instance (default: "default")
  - Bridge-specific credentials (e.g., `--api-id`, `--bot-token`)

### `register` - Register with Matrix Server
```bash
./bridge.py register BRIDGE_TYPE --instance INSTANCE_NAME
```
Generates registration file and provides instructions for Matrix server registration.

### `start` - Start Bridge(s)
```bash
# Start specific bridge
./bridge.py start telegram --instance my-instance

# Start all bridges for instance
./bridge.py start --all --instance my-instance
```

### `stop` - Stop Bridge(s)
```bash
# Stop specific bridge
./bridge.py stop telegram --instance my-instance

# Stop all bridges
./bridge.py stop --all --instance my-instance
```

### `status` - Check Bridge Status
```bash
./bridge.py status --instance my-instance
```
Shows all bridges for an instance with their current status.

### `list` - List All Bridges
```bash
./bridge.py list
```
Shows all configured bridges across all instances.

### `logs` - View Bridge Logs
```bash
# View last 100 lines
./bridge.py logs telegram --instance my-instance

# Follow logs in real-time
./bridge.py logs telegram --instance my-instance --follow
```

### `remove` - Remove Bridge(s)
```bash
# Remove specific bridge
./bridge.py remove telegram --instance my-instance

# Remove all bridges for instance
./bridge.py remove --all --instance my-instance --force
```

## Bridge Types

### Telegram
**Required Credentials**:
- **API ID & Hash**: From https://my.telegram.org
- **Bot Token**: From @BotFather in Telegram

**Setup Steps**:
1. Create a bot with @BotFather
2. Get API credentials from my.telegram.org
3. Add bridge with credentials
4. Register and start

### Slack
**Required Credentials**:
- **App Token**: Socket Mode token (xapp-...) from Basic Information > App-Level Tokens
- **Bot Token**: Bot User OAuth Token (xoxb-...) from OAuth & Permissions
- **Team ID**: Workspace ID (T...) from your Slack URL or workspace settings

**Setup Steps**:
1. Create a new Slack App at https://api.slack.com/apps
2. Enable Socket Mode and generate an App-Level Token with connections:write scope
3. Add OAuth scopes: channels:history, channels:read, chat:write, users:read
4. Install app to your workspace
5. Get the Bot User OAuth Token
6. Find your Team ID in the Slack URL or workspace settings
7. Add bridge with credentials:
   ```bash
   ./bridge.py add slack --instance my-instance \
     --app-token xapp-... \
     --bot-token xoxb-... \
     --team-id T...
   ```

### Email (Coming Soon)
**Required Credentials**:
- **Domain**: Your email domain
- **SMTP Settings**: Host, port, credentials

## Architecture

```
instance_data/
└── {instance_name}/
    ├── bridges/
    │   ├── telegram/
    │   │   ├── docker-compose.yml
    │   │   └── data/
    │   │       ├── config.yaml
    │   │       ├── registration.yaml
    │   │       └── mautrix-telegram.db
    │   ├── slack/
    │   └── email/
    └── ... (other instance data)
```

## Bridge Registration

### For Tuwunel/Conduit
1. The script generates `registration.yaml`
2. Join admin room: `#admins:your-server.com`
3. Send: `!admin appservices register`
4. Paste the registration content
5. Verify: `!admin appservices list`

### For Synapse
1. The script adds registration to `homeserver.yaml`
2. Restart Synapse: `./deploy.py restart --only-matrix`
3. Bridge is automatically registered on startup

## Port Allocation

Bridges use dedicated port ranges to avoid conflicts:
- **Telegram**: 29317-29399
- **Slack**: 29400-29499
- **Email**: 29500-29599

Ports are automatically allocated and tracked to prevent conflicts.

## Docker Network

All bridges join the `matrix-bridges` network for communication:
```bash
docker network create matrix-bridges
```
This is created automatically when starting bridges.

## Troubleshooting

### Bridge Won't Start
- Check logs: `./bridge.py logs telegram --instance my-instance`
- Verify registration: Check that bridge is registered with Matrix server
- Check ports: Ensure allocated port is not in use

### Registration Issues
- **Tuwunel**: Ensure you're in the admin room and have admin privileges
- **Synapse**: Check that homeserver.yaml has correct registration path

### Connection Issues
- Verify Matrix server is running: `./deploy.py status`
- Check network: `docker network ls | grep matrix-bridges`
- Ensure firewall allows bridge ports

## Integration with deploy.py

The bridge manager integrates seamlessly with Mindroom instances:

```bash
# Complete workflow
./deploy.py create demo --matrix tuwunel    # Create instance with Matrix
./deploy.py start demo                       # Start instance
./bridge.py add telegram --instance demo     # Add Telegram bridge
./bridge.py register telegram --instance demo # Register with Matrix
./bridge.py start telegram --instance demo   # Start bridge
```

## Data Persistence

- Bridge configurations are stored in the instance's data directory
- Registry tracked in `bridge_instances.json`
- All data removed with `./bridge.py remove --force`

## Security Notes

- Never commit credential files or registration files
- Use environment variables for production deployments
- Regularly rotate bridge tokens and credentials
- Monitor bridge logs for security events
