# Telegram Bridge for Mindroom

Connects Telegram chats to Matrix rooms using mautrix-telegram.

## Automated Setup with bridge.py

The Telegram bridge is now fully automated using the `bridge.py` tool.

### Quick Setup

```bash
# From the deploy directory
cd /opt/stacks/mindroom/deploy

# Add Telegram bridge to your instance
./bridge.py add telegram --instance default \
  --api-id YOUR_API_ID \
  --api-hash YOUR_API_HASH \
  --bot-token YOUR_BOT_TOKEN

# Generate and register
./bridge.py register telegram --instance default

# Start the bridge
./bridge.py start telegram --instance default
```

## Getting Telegram Credentials

### 1. API ID and Hash (Required)

Get these from https://my.telegram.org:

1. Log in with your phone number
2. Click "API development tools"
3. Create an app if you haven't already:
   - App title: Your choice (e.g., "Mindroom Bridge")
   - Short name: Your choice (e.g., "mindroom")
   - Platform: Other
   - Description: Optional
4. Save your:
   - **API ID**: A number like `26756732`
   - **API Hash**: A string like `74f8d547c03e3b68bacb65f3e8943159`

### 2. Bot Token (Required)

Get this from @BotFather in Telegram:

1. Start a chat with @BotFather
2. Send `/newbot`
3. Choose a display name (e.g., "Mindroom Bridge")
4. Choose a username (must end in 'bot', e.g., `mindroom_bridge_bot`)
5. Save the token (looks like `8221446002:AAHtp1yxBI5kFv022lkje_VSqJGbmmrL9Ro`)

### Important: Use Different Bots for Different Instances

If you're running multiple Mindroom instances, create a separate bot for each:
- Default instance: `@mindroom_default_bot`
- Alt instance: `@mindroom_alt_bot`
- Test instance: `@mindroom_test_bot`

This prevents conflicts and message duplication.

## Registration with Matrix Server

### For Tuwunel/Conduit

After running `./bridge.py register telegram --instance yourinstance`:

1. Join the admin room: `#admins:m-yourinstance.mindroom.chat`
2. Send: `!admin appservices register`
3. Paste the entire registration.yaml content (shown in the command output)
4. Verify with: `!admin appservices list`

### For Synapse

The registration is automatically added to homeserver.yaml. Just restart Synapse:
```bash
./deploy.py restart yourinstance --only-matrix
```

## Using the Bridge

### First Time Setup

1. In your Matrix client (Element, etc.), start a DM with `@telegrambot:m-yourinstance.mindroom.chat`
2. Send `help` to see available commands
3. Send `login` to connect your personal Telegram account

### Available Commands

- `help` - Show all commands
- `login` - Start Telegram login process
- `logout` - Disconnect from Telegram
- `ping` - Check if bridge is alive
- `list` - Show your Telegram chats
- `open <name>` - Bridge a Telegram chat to Matrix
- `pm <username>` - Start private chat with Telegram user
- `sync` - Synchronize your chats
- `sync --create-all` - Create rooms for all Telegram chats

### Login Process

When you send `login`:
1. The bot will ask for your phone number
2. Enter it in international format (e.g., `+1234567890`)
3. Telegram will send you a code
4. Enter the code
5. If you have 2FA, enter your password
6. You're connected!

## Managing Bridges

### Check Status
```bash
# Status for one instance
./bridge.py status --instance default

# List all bridges
./bridge.py list
```

### View Logs
```bash
# Recent logs
./bridge.py logs telegram --instance default --tail 50

# Follow logs
./bridge.py logs telegram --instance default --follow
```

### Stop/Start
```bash
# Stop
./bridge.py stop telegram --instance default

# Start
./bridge.py start telegram --instance default

# Restart
./bridge.py stop telegram --instance default && \
./bridge.py start telegram --instance default
```

### Remove Bridge
```bash
# Remove bridge and all data
./bridge.py remove telegram --instance default --force
```

## Features

### What Works
✅ Text messages (both directions)
✅ Images, videos, files
✅ Stickers and GIFs
✅ Replies and edits
✅ Typing notifications
✅ Read receipts
✅ Group chats and channels
✅ Contact sharing
✅ Location sharing

### Limitations
- Telegram calls aren't bridged
- Some Telegram-specific features (polls, games) have limited support
- Large files may be slow

## Troubleshooting

### Bridge Won't Start

Check the logs:
```bash
./bridge.py logs telegram --instance default --tail 100
```

Common issues:
- **Database errors**: Fixed automatically by bridge.py
- **Network issues**: bridge.py configures the correct network
- **Bot user missing**: Bridge auto-creates it on start

### Can't Login to Telegram

- Check your phone number format (include country code)
- Make sure 2FA is handled correctly
- Try `logout` then `login` again
- Check if Telegram is blocking your IP (use VPN if needed)

### Messages Not Bridging

1. Check bridge is running: `./bridge.py status --instance default`
2. Check registration: In admin room, send `!admin appservices list`
3. Verify bot user exists: Search for `@telegrambot:m-yourinstance.mindroom.chat`
4. Check logs for errors: `./bridge.py logs telegram --instance default --follow`

### "Unknown access token" Errors

The bridge isn't properly registered:
1. Re-run: `./bridge.py register telegram --instance yourinstance`
2. Re-register in the admin room with the new registration.yaml
3. Restart the bridge

## Configuration Files

The bridge.py tool manages all configuration automatically:

```
instance_data/
└── yourinstance/
    └── bridges/
        └── telegram/
            ├── docker-compose.yml  # Docker configuration
            └── data/
                ├── config.yaml      # Bridge configuration
                ├── registration.yaml # Matrix registration
                └── *.db            # SQLite databases
```

### Manual Configuration (Advanced)

If you need to modify settings, edit:
`instance_data/yourinstance/bridges/telegram/data/config.yaml`

Then restart the bridge:
```bash
./bridge.py stop telegram --instance yourinstance
./bridge.py start telegram --instance yourinstance
```

## Security Notes

- API credentials are stored in `bridge_instances.json` (gitignored)
- Each instance should use its own bot token
- Database files contain message history - keep secure
- Use strong passwords for Telegram 2FA

## Manual Setup (Legacy)

<details>
<summary>Click to see manual setup instructions</summary>

### Prerequisites

1. **Telegram Bot Token** from @BotFather
2. **API Credentials** from https://my.telegram.org
3. **Matrix Server** with admin access

### Manual Steps

1. Generate config:
```bash
docker run --rm -v $(pwd)/data:/data:z dock.mau.dev/mautrix/telegram:latest
```

2. Edit `data/config.yaml` with your credentials

3. Generate registration:
```bash
docker compose up  # Ctrl+C after registration.yaml appears
```

4. Register with Matrix server (see above)

5. Run:
```bash
docker compose up -d
```

</details>

## Resources

- Upstream docs: https://docs.mau.fi/bridges/python/telegram/
- Telegram Bot API: https://core.telegram.org/bots/api
- Matrix Spec: https://spec.matrix.org/
