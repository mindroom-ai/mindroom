# Matrix Bridges for MindRoom Demo

This directory contains Matrix bridge configurations for connecting various platforms to your Matrix server.

## Available Bridges

### ✅ Telegram Bridge (`telegram/`)
- **Status**: Configured and tested
- **Bot**: @mindroom_demo_bot
- **Features**: Bidirectional messaging, media support, user puppeting
- **Setup Time**: ~10 minutes

### 🚧 Slack Bridge (coming soon)
- **Status**: Not yet configured
- **Features**: Workspace bridging, threading support
- **Setup Time**: ~15 minutes

### 🚧 Email Bridge (coming soon)
- **Status**: Not yet configured
- **Features**: SMTP/IMAP bridging, email to Matrix rooms
- **Setup Time**: ~10 minutes

## Quick Start

### Prerequisites
1. Matrix server (Synapse/Dendrite/Conduit/Tuwunel)
2. Docker and Docker Compose
3. Admin access to your Matrix server

### General Setup Pattern

Each bridge follows the same pattern:

1. **Generate config**:
   ```bash
   cd bridge-name
   docker run --rm -v $(pwd)/data:/data:z dock.mau.dev/mautrix/bridge-name:latest
   ```

2. **Configure**: Edit `data/config.yaml` with your credentials

3. **Generate registration**:
   ```bash
   docker compose up  # Ctrl+C after registration.yaml is created
   ```

4. **Register with Matrix server**:

   **For Tuwunel/Conduit servers:**
   - Join `#admins:your-server.com` room
   - Send: `!admin appservices register`
   - Paste the entire `registration.yaml` content
   - Verify with: `!admin appservices list`

   **For Synapse servers:**
   - Copy `registration.yaml` to server
   - Add to `homeserver.yaml` under `app_service_config_files`
   - Restart Synapse

5. **Run**:
   ```bash
   docker compose up -d
   ```

## Network Configuration

All bridges share a common Docker network:

```bash
# Create the shared network (one time)
docker network create matrix-bridges
```

## Security Notes

⚠️ **Never commit these files:**
- `data/config.yaml` - Contains API keys and tokens
- `data/registration.yaml` - Contains authentication tokens
- `.env` - Environment variables with secrets
- `*.db` - Database files

Use the provided `.gitignore` files in each bridge directory.

## For Demo Purposes

For the MindRoom demo, we're focusing on showing:
1. **Cross-platform messaging** - Same conversation in Telegram, Slack, Email
2. **Persistent memory** - Agents remember context across platforms
3. **Federation** - Agents joining from different servers

### Demo Credentials Needed

#### Telegram
1. **Bot Token** (from @BotFather in Telegram):
   - Send `/newbot` to @BotFather
   - Choose bot name and username (must end in `bot`)
   - Save the token you receive

2. **API Credentials** (from https://my.telegram.org):
   - Log in with your phone number
   - Go to "API development tools"
   - Create an app if needed (Platform: Web)
   - Save your API ID (number) and API Hash (string)

#### Slack
- Slack App OAuth Token
- Workspace details
- See: https://api.slack.com/apps

#### Email
- SMTP server credentials
- Domain for receiving emails
- See: https://github.com/etkecc/postmoogle

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Telegram   │────▶│   Bridge    │────▶│   Matrix    │
└─────────────┘     └─────────────┘     └─────────────┘
                            │                    │
┌─────────────┐     ┌─────────────┐            │
│    Slack    │────▶│   Bridge    │────────────┤
└─────────────┘     └─────────────┘            │
                            │                    │
┌─────────────┐     ┌─────────────┐            │
│    Email    │────▶│   Bridge    │────────────┤
└─────────────┘     └─────────────┘            │
                                                 ▼
                                          ┌─────────────┐
                                          │  MindRoom   │
                                          │   Agents    │
                                          └─────────────┘
```

## Troubleshooting

### Bridge can't connect to Matrix
- Check `homeserver` address in config.yaml
- Verify registration.yaml is loaded by Matrix server
- Check firewall/network settings

### "Unknown access token" errors
- Registration not loaded by Matrix server
- Restart Matrix server after adding registration
- Check tokens match between config and registration

### Database errors
- Ensure data directory has write permissions
- For Docker: `chmod 777 data/` (for demo only!)

## Support

For bridge-specific issues:
- Telegram: https://docs.mau.fi/bridges/python/telegram/
- Slack: https://docs.mau.fi/bridges/python/slack/
- Email: https://github.com/etkecc/postmoogle
