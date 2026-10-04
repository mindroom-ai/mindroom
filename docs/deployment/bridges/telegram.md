---
icon: lucide/message-circle
---

# Telegram Bridge

Bridge Telegram and Matrix with `mautrix-telegram` in puppet mode, so MindRoom agents can talk in Telegram groups.
Linked Telegram groups become Matrix rooms, and permitted users can log in with their own Telegram account.

## Prerequisites

Create Telegram API credentials (API ID and API hash) at [my.telegram.org](https://my.telegram.org), and create a bridge bot through [@BotFather](https://t.me/BotFather).
Keep the API hash and bot token secret.

## Deploy

Run the [bridge manager](index.md#bridge-manager) from `local/instances/deploy/`:

```bash
./bridge.py add telegram --instance <instance> --admin @<you>:m-<instance-domain>
./bridge.py register telegram --instance <instance>
./bridge.py start telegram --instance <instance>
./bridge.py status --instance <instance>
./bridge.py logs telegram --instance <instance>
```

Provide the Telegram credentials to `bridge.py add` with `--api-id`, `--api-hash`, and `--bot-token`, through the `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_BOT_TOKEN` environment variables, or in `local/instances/deploy/.env.telegram`.
The command prompts for any credential still missing, and for the admin Matrix user ID when `--admin` is omitted.
The manager pins `mautrix-telegram` `v0.15.3`, the legacy Python bridge.

Between `register` and `start`, register the bridge with the homeserver as described in the [bridge manager](index.md#bridge-manager) steps.
On Synapse, place the registration at `/data/bridges/telegram/registration.yaml` in the Synapse container, readable only by the container's user (UID 1000).

## Bridge permissions

The generated bridge configuration grants `admin` only to the `--admin` user and `relaybot` to everyone else, so accounts registered on an open homeserver cannot control the bridge.
To let more users log in with their own Telegram accounts, add their Matrix user IDs with the `puppeting` (or `full`) level under `bridge.permissions` in the generated configuration, then restart the bridge.

Bridges created by older manager versions grant `user` to the whole homeserver domain and `admin` to `@admin:<domain>`.
Fix one by setting `bridge.permissions` to `relaybot` for `*` and `admin` for your own Matrix user ID, then restarting it, or recreate it with `bridge.py remove` and `bridge.py add --admin`, which deletes its data.

## Configure MindRoom

When room access is restrictive, Telegram ghost users must satisfy [responder access](../../authorization.md#responder-access) like any other sender.
Add a [bridge alias](../../authorization.md#bridge-aliases) when a ghost should act as an existing Matrix user with the same identity and permissions.
List the bridge bot in [`bot_accounts`](../../authorization.md#bot-accounts) when it can send messages, and use room-level [thread mode](../../configuration/threads.md) because Telegram does not preserve Matrix threads:

```yaml
bot_accounts:
  - "@telegrambot:matrix.example.com"

authorization:
  aliases:
    "@owner:matrix.example.com":
      - "@telegram_12345:matrix.example.com"

agents:
  assistant:
    thread_mode: room
```

Replace the owner and Telegram ghost IDs with the ones from your deployment.
Aliases are exact Matrix user IDs, not glob patterns, so list every ghost that should act as that user.

## Log in to Telegram

Start a Matrix DM with the Telegram bridge bot and send `login` for phone authentication or `login-qr` for QR authentication.
Telegram usually sends the login code to an already signed-in Telegram app, and accounts with two-factor authentication are also asked for their password.

Telegram login does not set up Matrix double puppeting; configure that separately with the `double_puppet` settings in the generated bridge configuration.

## Link a room

1. Create a Telegram group and add your Telegram bot to it.
2. Invite the Matrix bridge bot into the MindRoom-managed Matrix room.
3. Create or link the portal with the commands from the bridge bot's `help` output.

Portal commands differ between `mautrix-telegram` versions, so use the running bridge's `help` output rather than commands from other documentation.

## Data and backups

The generated configuration (`data/config.yaml`), registration, and SQLite database live in the bridge data directory under the instance data directory.
The configuration and registration contain the Telegram credentials and appservice tokens.
Deleting the bridge database removes Telegram logins and portal links, and users must log in again, so back up the bridge data directory before any reset.
