# Bridges

Bridges connect external messaging platforms to Matrix, so users can talk to MindRoom agents from those platforms.
MindRoom uses [mautrix](https://docs.mau.fi/bridges/) bridges, which run as Matrix [application services](https://spec.matrix.org/latest/application-service-api/) beside the homeserver (Synapse or Tuwunel).
A bridge creates Matrix rooms for external chats, creates ghost Matrix users for external contacts, and relays messages both ways in real time.
In **puppet mode**, you log in with your own account on the external platform, so your messages appear as coming from you on both sides rather than from a bot.

## Available Bridges

| Bridge | Platform | Mode | Status |
|--------|----------|------|--------|
| [Telegram](https://docs.mindroom.chat/deployment/bridges/telegram/) | Telegram | Puppet (login as yourself) | Available |
| Slack | Slack | - | Planned |
| Email | IMAP/SMTP | - | Planned |

## Bridge Manager

Deploy bridges with `./bridge.py` in `local/instances/deploy/`, for an instance created with a Matrix server by `./deploy.py create <name> --matrix tuwunel` or `--matrix synapse`.
Run `./bridge.py --help` there for every command and option.

1. Add the bridge with `./bridge.py add <type> --instance <name>`.
2. Generate its appservice registration with `./bridge.py register <type> --instance <name>`.
3. Register the bridge with the homeserver:
   - **Synapse:** `register` adds `/data/bridges/<type>/registration.yaml` to `app_service_config_files` in Synapse's `homeserver.yaml`, but you must place the generated registration file at that path in the Synapse container yourself.
     Make that copy readable only by the Synapse container's user (UID 1000), then restart Synapse with `./deploy.py restart <name> --only-matrix`.
   - **Tuwunel:** follow the admin-room steps that `register` prints, or run `./bridge.py register-with-matrix <type> --instance <name>`.
4. Start the bridge with `./bridge.py start <type> --instance <name>`.
5. Check it with `./bridge.py status --instance <name>` and `./bridge.py logs <type> --instance <name>`.

Use `./bridge.py stop` to stop bridges, `./bridge.py list` to see bridges across all instances, and `./bridge.py remove` to remove a bridge together with its data.
Each bridge's configuration, registration file, and data live in `bridges/<type>/` under the instance data directory.
