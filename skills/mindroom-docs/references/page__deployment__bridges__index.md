# Bridges

MindRoom uses [mautrix](https://docs.mau.fi/bridges/) bridges to connect external messaging platforms to Matrix. Bridges run as appservices alongside Synapse, creating ghost users for external contacts and relaying messages bidirectionally.

## Available Bridges

| Bridge                                                                      | Platform  | Mode                       | Status    |
| --------------------------------------------------------------------------- | --------- | -------------------------- | --------- |
| [Telegram](https://docs.mindroom.chat/deployment/bridges/telegram/index.md) | Telegram  | Puppet (login as yourself) | Available |
| Slack                                                                       | Slack     | -                          | Planned   |
| Email                                                                       | IMAP/SMTP | -                          | Planned   |

## How Bridges Work

Each bridge registers as a Matrix [Application Service](https://spec.matrix.org/latest/application-service-api/) with Synapse. The bridge:

1. Creates ghost users on Matrix for external contacts
1. Creates Matrix rooms for external chats
1. Relays messages between the external platform and Matrix in real time

In **puppet mode**, you log into your real account on the external platform. Your messages appear as coming from you on both sides, not from a bot.

## Adding a New Bridge

1. Create a config directory: `telegram-bridge/`, `slack-bridge/`, etc.
1. Add the bridge service to `compose.yaml`
1. Generate a registration file and mount it into Synapse
1. Add the registration path to `homeserver.yaml` under `app_service_config_files`
1. Restart Synapse and start the bridge
