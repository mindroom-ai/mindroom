# Bridges

MindRoom uses [mautrix](https://docs.mau.fi/bridges/) bridges to connect external messaging platforms to Matrix.
Bridges run as appservices alongside a Matrix homeserver such as Synapse or Tuwunel, create ghost users for external contacts, and relay messages bidirectionally.

## Available Bridges

| Bridge | Platform | Mode | Status |
|--------|----------|------|--------|
| [Telegram](https://docs.mindroom.chat/deployment/bridges/telegram/) | Telegram | Puppet (login as yourself) | Available |
| Slack | Slack | - | Planned |
| Email | IMAP/SMTP | - | Planned |

## How Bridges Work

Each bridge registers as a Matrix [Application Service](https://spec.matrix.org/latest/application-service-api/) with the homeserver.
The bridge:

1. Creates ghost users on Matrix for external contacts
2. Creates Matrix rooms for external chats
3. Relays messages between the external platform and Matrix in real time

In **puppet mode**, you log into your real account on the external platform. Your messages appear as coming from you on both sides, not from a bot.

## Bridge Manager

Bridge deployments are managed from `local/instances/deploy/`.
Run `./bridge.py --help` there for the exact supported commands.

The normal workflow is:

1. Add the bridge with `bridge.py add <type> --instance <name>`.
2. Generate and install its appservice registration with `bridge.py register <type> --instance <name>`.
3. Start it with `bridge.py start <type> --instance <name>`.
4. Inspect it with `bridge.py status` and `bridge.py logs`.

Generated bridge data lives beneath the selected instance data directory.
Synapse registration is wired automatically by the manager; Tuwunel uses its appservice registration flow.

Adding a new bridge type requires a template under `local/instances/deploy/templates/bridges/` plus corresponding manager support.
