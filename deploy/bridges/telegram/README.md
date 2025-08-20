# Telegram Bridge Setup

This directory contains the mautrix-telegram bridge configuration for connecting Telegram to Matrix.

## Prerequisites

1. **Telegram Bot Token**: Create a bot via @BotFather on Telegram
2. **Telegram API Credentials**: Get from https://my.telegram.org
3. **Matrix Server**: Access to your Matrix homeserver

## Quick Setup

### 1. Generate Configuration

```bash
# Generate default config
docker run --rm -v $(pwd)/data:/data:z dock.mau.dev/mautrix/telegram:latest

# This creates data/config.yaml
```

### 2. Configure the Bridge

Edit `data/config.yaml` with your settings:

```yaml
# Homeserver details
homeserver:
    address: https://your-matrix-server.com  # Your Matrix server
    domain: your-matrix-server.com           # Your Matrix domain

# Database (for simple setup, using SQLite)
appservice:
    database: sqlite:///data/mautrix-telegram.db

# Telegram settings
telegram:
    api_id: YOUR_API_ID          # From my.telegram.org
    api_hash: YOUR_API_HASH      # From my.telegram.org
    bot_token: YOUR_BOT_TOKEN    # From @BotFather

# Permissions
bridge:
    permissions:
        "*": "relaybot"
        "your-domain.com": "full"
        "@admin:your-domain.com": "admin"
```

### 3. Start the Bridge

```bash
# First run generates registration.yaml
docker compose up

# After registration is generated, stop with Ctrl+C
```

### 4. Register with Matrix

1. Copy `data/registration.yaml` to your Matrix server
2. Add to Synapse's `homeserver.yaml`:
   ```yaml
   app_service_config_files:
     - /path/to/registration.yaml
   ```
3. Restart Synapse

### 5. Run the Bridge

```bash
docker compose up -d
```

## Testing

In Matrix/Element:
1. Start a DM with `@telegrambot:your-domain.com`
2. Type `help` to see available commands
3. Type `login` to connect your Telegram account

## Credentials Required

Create a `.env` file (don't commit this!):
```env
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_BOT_TOKEN=your_bot_token
MATRIX_DOMAIN=your-matrix-domain.com
```

## Files

- `data/config.yaml` - Bridge configuration (contains secrets, don't commit!)
- `data/registration.yaml` - Appservice registration for Matrix
- `data/mautrix-telegram.db` - SQLite database
- `docker-compose.yml` - Docker setup
