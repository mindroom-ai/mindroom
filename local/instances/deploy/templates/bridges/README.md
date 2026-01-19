# Matrix Bridges for Mindroom

This directory contains Matrix bridge configurations and the automated bridge management tool for Mindroom instances.

## Quick Start with bridge.py

We now have an automated bridge management tool that simplifies the entire process:

```bash
# Add a Telegram bridge to an instance
./bridge.py add telegram --instance default

# Generate registration file
./bridge.py register telegram --instance default

# Start the bridge
./bridge.py start telegram --instance default

# Check bridge status
./bridge.py status --instance default

# View bridge logs
./bridge.py logs telegram --instance default
```

## Available Bridges

### ✅ Telegram Bridge
- **Status**: Fully automated and tested
- **Features**: Bidirectional messaging, media support, user puppeting
- **Setup Time**: ~5 minutes with bridge.py

### 🚧 Slack Bridge (coming soon)
- **Status**: Not yet configured
- **Features**: Workspace bridging, threading support

### 🚧 Email Bridge (coming soon)
- **Status**: Not yet configured
- **Features**: SMTP/IMAP bridging, email to Matrix rooms

## Complete Setup Guide

### Prerequisites
1. A running Mindroom instance with Matrix (Tuwunel or Synapse)
2. Docker installed and running
3. Bridge credentials (API keys, bot tokens, etc.)

### Step 1: Check Your Instances

```bash
# List all Mindroom instances
./deploy.py list
```

Make sure your instance has a Matrix server (shown as T for Tuwunel or S for Synapse).

### Step 2: Add a Bridge

#### For Telegram:

First, get your credentials:
1. **API ID and Hash** from https://my.telegram.org
   - Log in with your phone number
   - Go to "API development tools"
   - Create an app if needed
   - Save your API ID and API Hash

2. **Bot Token** from @BotFather in Telegram:
   - Send `/newbot` to @BotFather
   - Choose a name and username (must end in 'bot')
   - Save the token

Then add the bridge:
```bash
./bridge.py add telegram --instance yourinstance \
  --api-id YOUR_API_ID \
  --api-hash YOUR_API_HASH \
  --bot-token YOUR_BOT_TOKEN
```

### Step 3: Register the Bridge

Generate the registration file:
```bash
./bridge.py register telegram --instance yourinstance
```

#### For Tuwunel/Conduit:
1. Join the admin room: `#admins:m-yourinstance.mindroom.chat`
2. Send: `!admin appservices register`
3. Paste the registration.yaml content (shown in the output)
4. Verify with: `!admin appservices list`

#### For Synapse:
The registration is automatically added to the homeserver.yaml

### Step 4: Start the Bridge

```bash
./bridge.py start telegram --instance yourinstance
```

The bridge will:
- Start the Docker container
- Connect to your Matrix server
- Auto-create the bot user (if needed)
- Connect to Telegram

### Step 5: Use the Bridge

In your Matrix client (Element, etc.):
1. Start a direct message with `@telegrambot:m-yourinstance.mindroom.chat`
2. Send `help` to see available commands
3. Send `login` to connect your Telegram account

## Managing Multiple Instances

You can run bridges on multiple instances with different bots:

```bash
# Add bridge to 'default' instance
./bridge.py add telegram --instance default --bot-token TOKEN1

# Add bridge to 'alt' instance with a different bot
./bridge.py add telegram --instance alt --bot-token TOKEN2

# Start all bridges for an instance
./bridge.py start --all --instance default

# Check status across all instances
./bridge.py list
```

## bridge.py Commands

### Core Commands
- `add <type>` - Add a new bridge to an instance
- `register <type>` - Generate registration for Matrix server
- `start <type>` - Start a bridge
- `stop <type>` - Stop a bridge
- `status` - Show bridge status for an instance
- `list` - List all bridges across all instances
- `remove <type>` - Remove a bridge and its data
- `logs <type>` - Show bridge logs

### Options
- `--instance NAME` - Specify which Mindroom instance (default: "default")
- `--all` - Apply to all bridges for an instance
- `--force` - Skip confirmations
- `--follow` - Follow log output
- `--tail N` - Number of log lines to show

## Technical Details

### How bridge.py Works

1. **Automatic Network Configuration**: Bridges are automatically placed on the same Docker network as their Matrix server
2. **Port Management**: Automatically allocates non-conflicting ports
3. **URL Resolution**: Uses Docker container names for internal communication
4. **Permission Handling**: Sets correct file permissions for databases
5. **Bot User Creation**: Attempts to auto-create bot users when needed

### File Locations

```
/opt/stacks/mindroom/deploy/
├── bridge.py                    # Bridge management tool
├── bridge_instances.json        # Bridge configuration (gitignored)
└── instance_data/
    └── <instance>/
        └── bridges/
            └── <bridge-type>/
                ├── docker-compose.yml
                └── data/
                    ├── config.yaml
                    ├── registration.yaml
                    └── *.db
```

### Network Architecture

```
┌──────────────────────────────────────────────────────┐
│ Docker Network: <instance>_mindroom-network          │
│                                                       │
│  ┌─────────────┐     ┌─────────────┐                │
│  │   Tuwunel   │────▶│  Telegram   │                │
│  │   Matrix    │◀────│   Bridge    │                │
│  └─────────────┘     └─────────────┘                │
│         │                    │                       │
│         └────────┬───────────┘                       │
│                  ▼                                    │
│          ┌─────────────┐                            │
│          │  Mindroom   │                            │
│          │   Backend   │                            │
│          └─────────────┘                            │
└──────────────────────────────────────────────────────┘
                       │
                       ▼
              External Networks
            (Telegram, Slack, etc.)
```

## Troubleshooting

### Common Issues

#### "M_UNKNOWN_TOKEN: Unknown access token"
- The bridge isn't properly registered with the Matrix server
- For Tuwunel: Re-register in the admin room
- Make sure the registration URL uses the container name, not localhost

#### "M_NOT_FOUND: User does not exist"
- The bot user hasn't been created
- The bridge normally auto-creates it, but you may need to restart the bridge

#### "Cannot connect to host"
- Network configuration issue
- The bridge is automatically configured for the correct network
- Check if containers are running: `docker ps`

#### Database permission errors
- The data directory needs proper permissions
- bridge.py sets these automatically
- Manual fix: `chmod 755 instance_data/*/bridges/*/data`

### Checking Logs

```bash
# View recent logs
./bridge.py logs telegram --instance default --tail 50

# Follow logs in real-time
./bridge.py logs telegram --instance default --follow

# Check Docker logs directly
docker logs default-telegram-bridge
```

## Security Notes

⚠️ **Never commit these files:**
- `bridge_instances.json` - Contains API keys and tokens
- `data/config.yaml` - Contains credentials
- `data/registration.yaml` - Contains authentication tokens
- `*.db` - Database files

The `bridge_instances.json` file is automatically added to `.gitignore`.

## Support

- Telegram Bridge: https://docs.mau.fi/bridges/python/telegram/
- Slack Bridge: https://docs.mau.fi/bridges/python/slack/
- General Mautrix: https://docs.mau.fi/bridges/
- Mindroom: https://github.com/mindroom-ai/mindroom
