---
icon: lucide/message-circle
---

# Telegram Bridge

Bridge Telegram chats to Matrix using [mautrix-telegram](https://docs.mau.fi/bridges/python/telegram/) in **puppet mode**. Each user logs in with their own Telegram account, so messages appear as the real user on both sides.

## Architecture

```
Telegram Cloud <--> mautrix-telegram <--> Synapse <--> Element
                    (bridge bot)         (homeserver)   (client)
```

- **mautrix-telegram** connects to both Telegram and Synapse
- Each Matrix user can log into their own Telegram account (puppeting)
- Telegram chats appear as Matrix rooms with ghost users for Telegram contacts
- Messages flow bidirectionally in real time

## Prerequisites

### 1. Telegram API Credentials

1. Go to [my.telegram.org](https://my.telegram.org) and log in
2. Click "API development tools"
3. Create an app (title: "MindRoom Bridge", short name: "mindroom")
4. Note the **api_id** (numeric) and **api_hash** (string)

### 2. Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, choose a name and username
3. Note the **bot token** (format: `123456789:ABCdefGHI...`)

## Setup

### 1. Add credentials to config

Edit `/opt/stacks/mindroom/telegram-bridge/config.yaml` and replace the placeholders in the `telegram:` section:

```yaml
telegram:
    api_id: 12345678          # Your numeric api_id
    api_hash: abcdef123456    # Your api_hash string
    bot_token: 123456:ABC...  # Your bot token from BotFather
```

Also update the same values in `/opt/stacks/mindroom/.env`:

```bash
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef123456
TELEGRAM_BOT_TOKEN=123456:ABC...
```

### 2. Restart Synapse and start the bridge

```bash
# Restart Synapse to pick up the bridge registration
cf compose mindroom restart synapse

# Start the bridge
cf compose mindroom up -d telegram-bridge
```

### 3. Verify

```bash
# Check bridge logs
cf compose mindroom logs telegram-bridge --tail 20

# Look for "Bridge initialization complete"
```

## Usage: Puppet Mode Login

1. Open Element at `element.lab.nijho.lt`
2. Start a DM with `@telegrambot:matrix.lab.nijho.lt`
3. Send `login` to start the Telegram authentication flow
4. Enter your phone number when prompted
5. Enter the verification code sent to your Telegram app
6. Your Telegram chats will appear as Matrix rooms

### Bridging Groups and Channels

After logging in:

- **Private chats**: Automatically bridged as Matrix DMs
- **Groups**: Send `bridge <chat_id>` to the bridge bot, or use `search <query>` to find groups
- **Channels**: Use `bridge <channel_username>` to bridge public channels

### Useful Bot Commands

| Command | Description |
|---------|-------------|
| `login` | Start Telegram login |
| `logout` | Disconnect Telegram account |
| `ping` | Check bridge connection status |
| `search <query>` | Search your Telegram chats |
| `bridge <id>` | Bridge a specific chat |
| `unbridge` | Unbridge current room |
| `sync` | Re-sync chat list |
| `help` | Show all commands |

## Configuration Reference

Key settings in `telegram-bridge/config.yaml`:

| Setting | Default | Description |
|---------|---------|-------------|
| `bridge.username_template` | `telegram_{userid}` | Matrix username pattern for Telegram ghosts |
| `bridge.displayname_template` | `{displayname} (TG)` | Display name pattern for Telegram users |
| `bridge.sync_create_limit` | `30` | Max chats to auto-create on first sync |
| `bridge.sync_direct_chats` | `true` | Auto-bridge private chats |
| `bridge.encryption.allow` | `true` | Allow E2EE in bridged rooms |
| `bridge.permissions` | See config | Who can use the bridge and at what level |

### Permission Levels

Set in `bridge.permissions`:

- `relaybot` - Messages relayed through the bot (not puppeted)
- `user` - Can use the bridge but not log in
- `puppeting` - Can log in with their Telegram account
- `full` - Full access including creating portals
- `admin` - Bridge administration

Default config gives `full` to all `matrix.lab.nijho.lt` users.

## Troubleshooting

### Bridge won't start

- Check credentials: `api_id` must be numeric, `api_hash` must be a hex string, `bot_token` must be a valid BotFather token
- Check logs: `cf compose mindroom logs telegram-bridge --tail 50`
- Verify Synapse is healthy: `cf compose mindroom ps`

### Login fails

- Ensure `api_id` and `api_hash` are from the same Telegram app
- The bot token must be from a bot you own (not revoked)
- If you get "FLOOD_WAIT", wait the indicated time before retrying

### Messages not bridging

- Check the bridge is connected: DM the bot and send `ping`
- Verify Synapse has the registration: check `app_service_config_files` in `homeserver.yaml`
- Check bridge permissions in `config.yaml` - your user domain must have `full` or `puppeting`

### Double puppeting

To make your messages from Matrix appear as your real Telegram account (not the bridge bot):

1. This is automatic when you log in via `login` - puppet mode is the default
2. If messages still show as the bot, check `bridge.sync_with_custom_puppets` in config

### Database issues

The bridge uses SQLite at `/mnt/data/mindroom/telegram-bridge/mautrix-telegram.db`. To reset:

```bash
cf compose mindroom stop telegram-bridge
rm /mnt/data/mindroom/telegram-bridge/mautrix-telegram.db
cf compose mindroom up -d telegram-bridge
```

Note: This will require re-logging into Telegram.

### Registration out of sync

If Synapse reports appservice errors, regenerate the registration:

```bash
cf compose mindroom stop telegram-bridge
rm /opt/stacks/mindroom/telegram-bridge/registration.yaml
# Temporarily set valid api_id in config.yaml, then:
cf compose mindroom run --rm --no-deps --entrypoint \
  "python -m mautrix_telegram -g -c /data/config.yaml -r /data/registration.yaml" \
  telegram-bridge
cf compose mindroom restart synapse
cf compose mindroom up -d telegram-bridge
```

## Maintenance

### Updating

```bash
cf update mindroom
# Or just the bridge:
cf compose mindroom pull telegram-bridge
cf compose mindroom up -d telegram-bridge
```

### Backup

Important data locations:

- `/opt/stacks/mindroom/telegram-bridge/config.yaml` - Bridge configuration
- `/opt/stacks/mindroom/telegram-bridge/registration.yaml` - Appservice registration
- `/mnt/data/mindroom/telegram-bridge/` - SQLite database with session data
