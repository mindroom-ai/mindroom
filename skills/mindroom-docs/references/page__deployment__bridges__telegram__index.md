# Telegram Bridge

Bridge Telegram and Matrix with `mautrix-telegram` in puppet mode.
Each user can authenticate their own Telegram account, and linked Telegram groups become Matrix rooms.

## Prerequisites

Create Telegram API credentials at [my.telegram.org](https://my.telegram.org), and create a bridge bot through [@BotFather](https://t.me/BotFather).
Keep the API hash and bot token secret.

## Deploy

Run the bridge manager from `local/instances/deploy/`:

```bash
./bridge.py add telegram --instance <instance>
./bridge.py register telegram --instance <instance>
./bridge.py start telegram --instance <instance>
./bridge.py status --instance <instance>
./bridge.py logs telegram --instance <instance>
```

`bridge.py add` creates the bridge data directory, bridge configuration, registry entry, and generated `docker-compose.yml` service named `telegram`.
Provide Telegram credentials with `--api-id`, `--api-hash`, and `--bot-token`, export the matching `TELEGRAM_*` variables, or create `local/instances/deploy/.env.telegram` before running the command.
When a credential is still missing, the command prompts for it and writes the resulting values into the generated bridge configuration and bridge registry.

For Synapse, `bridge.py register` updates `homeserver.yaml`, but the local Compose layout does not mount the generated bridge registration into the Synapse container.
Manually expose the generated file at the configured `app_service_config_files` path, then restart Synapse.
For Tuwunel, it generates the registration file and prints the manual admin-room steps; alternatively, run `./bridge.py register-with-matrix telegram --instance <instance>` after generation.

## Configure MindRoom

Telegram ghost users must resolve to an authorized Matrix requester when room access is restrictive.
Register the bridge bot as a bot account when it can originate events, and use room-level thread mode because Telegram does not preserve Matrix thread relations:

```yaml
bot_accounts:
  - "@telegrambot:matrix.example.com"

authorization:
  default_room_access: false
  global_users:
    - "@owner:matrix.example.com"
  aliases:
    "@owner:matrix.example.com":
      - "@telegram_12345:matrix.example.com"
  agent_reply_permissions:
    "*":
      - "@owner:matrix.example.com"

agents:
  assistant:
    thread_mode: room
```

Replace the canonical owner and exact Telegram ghost IDs with values from your deployment.
Aliases use exact Matrix user IDs rather than glob patterns, so add every bridge ghost that should inherit the canonical user's permissions.
Validate the complete configuration before starting MindRoom.

## Authenticate Telegram

Start a Matrix DM with the generated Telegram bridge bot.
Use either:

- `login phone +15551234567` for phone authentication.
- `login qr` for QR authentication.

Telegram normally sends the login code to an already authenticated Telegram client.
Accounts with two-factor authentication receive an additional password prompt.

## Link a Room

1. Create a Telegram group and add the generated Telegram bot.
2. Invite the Matrix bridge bot into the MindRoom-managed Matrix room.
3. Follow the bridge bot's current `help` output to create or link the portal.

Exact portal commands depend on the running `mautrix-telegram` version.
The manager currently deploys the unpinned `latest` image, so use the running bridge's `help` output rather than commands copied from another version.

## Operations

Use the manager for status, logs, start, stop, and registration operations.
Generated configuration, registration, and SQLite state live under the instance's bridge data directory.

Deleting the bridge database removes Telegram login and portal state and requires users to authenticate again.
Back up the generated bridge data directory before any reset.

Telegram-side puppeting is established by Telegram login.
Matrix double puppeting is a separate feature controlled by the generated configuration's current `double_puppet` settings.
Do not assume Telegram login configures Matrix double puppeting automatically.
