# Telegram Bridge Setup for Tuwunel

Since you're using Tuwunel, the registration process is different from Synapse.

## Files Created

- `data/config.yaml` - Bridge configuration with your Telegram credentials
- `data/registration.yaml` - Standard Matrix appservice registration

## Registering with Tuwunel

### Using the Admin Room

1. **Join the admin room** on your Tuwunel server
   - The admin room is named `#admins` (not #admin)
   - The first person that registered on the homeserver automatically joins it
   - Full room ID: `#admins:m-test.mindroom.chat`

2. **Copy the entire contents** of `data/registration.yaml`

3. **Send this message** in the admin room:
   ```
   !admin appservices register
   ```
   paste
   the
   contents
   of
   the
   yaml
   registration
   here
   ```
   ```

4. **Verify registration** by sending:
   ```
   !admin appservices list
   ```
   The server bot should answer with: `Appservices (1): telegram`

## Important Notes

1. The `url` in registration.yaml should point to where your bridge is accessible:
   - If bridge runs on same server as Tuwunel: `http://localhost:29318`
   - If bridge runs on different server: `http://your-bridge-host:29318`
   - If using Docker network: `http://mautrix-telegram:29317`

2. The registration will take effect immediately without restarting the server

## Starting the Bridge

After registration is complete:

```bash
cd deploy/bridges/telegram
docker compose up -d
```

## Testing

1. In Element/Matrix, start a DM with `@telegrambot:m-test.mindroom.chat`
2. Type `help` to see available commands
3. Type `login` to connect your Telegram account

## Troubleshooting

- **Check bridge logs**: `docker compose logs -f`
- **List registered appservices**: Send `!admin appservices list` in #admins room
- **Remove an appservice**: Send `!admin appservices unregister telegram` in #admins room
- **Restart note**: You don't need to restart Tuwunel after registration, but if it doesn't work, restarting while the appservice is running could help
- **Ensure the bridge can reach Tuwunel** at the configured address
- **Verify tokens match** between config.yaml and registration.yaml

## Getting Help

If you run into any problems:
- Ask in [#tuwunel:tuwunel.chat](https://matrix.to/#/#tuwunel:tuwunel.chat)
- [Open an issue on GitHub](https://github.com/matrix-construct/tuwunel/issues/new)
