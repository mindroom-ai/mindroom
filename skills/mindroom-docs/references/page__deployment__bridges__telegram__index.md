# Telegram Bridge

Bridge Telegram and Matrix using [mautrix-telegram](https://docs.mau.fi/bridges/python/telegram/) in **puppet mode**. Each user logs in with their own Telegram account, so messages appear as the real user on both sides.

## What Can You Do With This?

The bridge enables two main use cases:

1. **Talk to MindRoom agents from Telegram** -- Link a Telegram group to a Matrix room (like Lobby) so you can chat with AI agents directly from the Telegram app, without opening Element.
1. **Access Telegram chats from Matrix** -- Your existing Telegram conversations appear as Matrix rooms in Element, so you can use one client for everything.

Most users want use case 1. See [Bridging Matrix Rooms to Telegram](#bridging-matrix-rooms-to-telegram) after setup.

## Architecture

```
Telegram Cloud <--> mautrix-telegram <--> Synapse <--> Element
                    (bridge bot)         (homeserver)   (client)
```

- **mautrix-telegram** runs locally and connects outbound to Telegram's API -- your Matrix server does NOT need to be publicly accessible
- Each Matrix user can log into their own Telegram account (puppeting)
- Messages flow bidirectionally in real time

## Prerequisites

### 1. Telegram API Credentials

1. Go to [my.telegram.org](https://my.telegram.org) and log in
1. Click "API development tools"
1. Create an app (title: "MindRoom Bridge", short name: "mindroom")
1. Note the **api_id** (numeric) and **api_hash** (string)

### 2. Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
1. Send `/newbot`, choose a name and username
1. Note the **bot token** (format: `123456789:ABCdefGHI...`)

## Setup

### 1. Add credentials to config

Edit `telegram-bridge/config.yaml` and replace the placeholders in the `telegram:` section:

```
telegram:
    api_id: 12345678          # Your numeric api_id
    api_hash: abcdef123456    # Your api_hash string
    bot_token: 123456:ABC...  # Your bot token from BotFather
```

Also update the same values in your `.env`:

```
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef123456
TELEGRAM_BOT_TOKEN=123456:ABC...
```

### 2. Recreate Synapse and start the bridge

Synapse needs a new volume mount for the bridge registration file, so it must be **recreated** (not just restarted):

```
# Recreate Synapse to pick up the new volume mount and bridge registration
docker compose up -d synapse

# Wait for Synapse to become healthy
docker compose ps synapse

# Start the bridge
docker compose up -d telegram-bridge
```

> **Note:** `docker compose restart synapse` will NOT work here because the `registration.yaml` volume mount is new in `compose.yaml`. A restart reuses the existing container; `up -d` recreates it with the updated mounts.

### 3. Verify

```
# Check bridge logs
docker compose logs telegram-bridge --tail 20

# Look for "Startup actions complete"
```

## Usage

### Step 1: Log in to Telegram via the bridge

Before you can bridge anything, you must link your Telegram account:

1. Open Element at your Element URL
1. Start a DM with `@telegrambot:your.matrix.domain`
1. Send `login`
1. Enter your phone number in international format (e.g., `+1234567890`)
1. Enter the verification code sent to your Telegram app
1. Your existing Telegram chats will appear as Matrix rooms

### Step 2: Bridge Matrix rooms to Telegram

This is the primary use case -- talking to MindRoom agents from Telegram.

The bridge connects a **Telegram group** to a **Matrix room**. You need a Telegram group on the Telegram side because that's what you'll open in the Telegram app to send and receive messages.

**For each Matrix room you want to access from Telegram** (e.g., Lobby):

1. **Create a Telegram group** in the Telegram app (e.g., name it "MindRoom Lobby")
1. **Add your bridge bot** (e.g., `@your_bridge_bot`) to that Telegram group
1. **In Element**, go to the Matrix room you want to bridge (e.g., Lobby)
1. **Invite the bridge bot**: invite `@telegrambot:your.matrix.domain` to the room
1. **Link the rooms**: in the Matrix room, send `!tg bridge` -- the bot will list your Telegram groups and let you pick which one to link

Once linked:

- Messages you send in the **Telegram group** appear in the **Matrix room** -- MindRoom agents will see and respond to them
- Agent responses in the **Matrix room** appear in the **Telegram group**
- You can chat with MindRoom agents entirely from the Telegram app

Repeat for any other Matrix rooms you want accessible from Telegram.

> **Why can't I just invite the bot directly?** The bridge bot (`@telegrambot`) is Matrix-side infrastructure -- it manages the bridge but isn't a Telegram chat. To use Telegram as your client, there must be a Telegram group for the Telegram app to display. The bridge connects that group to the Matrix room bidirectionally.

### Accessing Telegram chats from Matrix

After logging in (step 1), your Telegram chats automatically appear as Matrix rooms in Element. This lets you use Element as a unified client for both Matrix and Telegram conversations.

- **Private chats**: Automatically bridged as Matrix DMs
- **Groups**: Automatically bridged if within `sync_create_limit` (default: 30)
- **Additional groups**: Use `search <query>` in the bridge bot DM to find and bridge more

### Bot Commands Reference

Send these to `@telegrambot:your.matrix.domain` in a DM, or in a bridged room:

| Command          | Description                                                     |
| ---------------- | --------------------------------------------------------------- |
| `login`          | Link your Telegram account                                      |
| `logout`         | Unlink your Telegram account                                    |
| `ping`           | Check bridge connection status                                  |
| `search <query>` | Search your Telegram chats                                      |
| `!tg bridge`     | Link current Matrix room to a Telegram group (send in the room) |
| `unbridge`       | Unlink current room from Telegram                               |
| `sync`           | Re-sync Telegram chat list                                      |
| `help`           | Show all commands                                               |

## Configuration Reference

Key settings in `telegram-bridge/config.yaml`:

| Setting                       | Default              | Description                                 |
| ----------------------------- | -------------------- | ------------------------------------------- |
| `bridge.username_template`    | `telegram_{userid}`  | Matrix username pattern for Telegram ghosts |
| `bridge.displayname_template` | `{displayname} (TG)` | Display name pattern for Telegram users     |
| `bridge.sync_create_limit`    | `30`                 | Max chats to auto-create on first sync      |
| `bridge.sync_direct_chats`    | `true`               | Auto-bridge private chats                   |
| `bridge.encryption.allow`     | `true`               | Allow E2EE in bridged rooms                 |
| `bridge.permissions`          | See config           | Who can use the bridge and at what level    |

### Permission Levels

Set in `bridge.permissions`:

- `relaybot` - Messages relayed through the bot (not puppeted)
- `user` - Can use the bridge but not log in
- `puppeting` - Can log in with their Telegram account
- `full` - Full access including creating portals
- `admin` - Bridge administration

Default config gives `full` to all users on your homeserver domain.

## Troubleshooting

### Bridge won't start

- Check credentials: `api_id` must be numeric, `api_hash` must be a hex string, `bot_token` must be a valid BotFather token
- Check logs: `docker compose logs telegram-bridge --tail 50`
- Verify Synapse is healthy: `docker compose ps`

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
1. If messages still show as the bot, check `bridge.sync_with_custom_puppets` in config

### Database issues

The bridge uses SQLite stored in the `telegram-bridge` data volume. To reset:

```
docker compose stop telegram-bridge
rm <data-dir>/telegram-bridge/mautrix-telegram.db
docker compose up -d telegram-bridge
```

Note: This will require re-logging into Telegram.

### Registration out of sync

If Synapse reports appservice errors, regenerate the registration:

```
docker compose stop telegram-bridge
rm telegram-bridge/registration.yaml
# Temporarily set valid api_id in config.yaml, then:
docker compose run --rm --no-deps --entrypoint \
  "python -m mautrix_telegram -g -c /data/config.yaml -r /data/registration.yaml" \
  telegram-bridge
docker compose restart synapse
docker compose up -d telegram-bridge
```

## Maintenance

### Updating

```
docker compose pull telegram-bridge
docker compose up -d telegram-bridge
```

### Backup

Important data locations:

- `telegram-bridge/config.yaml` - Bridge configuration
- `telegram-bridge/registration.yaml` - Appservice registration
- Telegram bridge data volume - SQLite database with session data
