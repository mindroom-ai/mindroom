# Slack Bridge for Matrix

This directory contains the configuration for the mautrix-slack bridge, which connects Slack workspaces to Matrix.

## Prerequisites

1. **Slack App Setup**:
   - Go to https://api.slack.com/apps
   - Click "Create New App" → "From scratch"
   - Give your app a name and select your workspace

2. **Enable Socket Mode**:
   - Go to "Socket Mode" in the left sidebar
   - Enable Socket Mode
   - Generate an App-Level Token with `connections:write` scope
   - Save the token (starts with `xapp-`)

3. **Configure OAuth & Permissions**:
   - Go to "OAuth & Permissions" in the left sidebar
   - Add these Bot Token Scopes:
     - `channels:history` - View messages in public channels
     - `channels:read` - View basic channel info
     - `chat:write` - Send messages as the bot
     - `users:read` - View users and their profiles
     - `groups:history` - View messages in private channels
     - `groups:read` - View basic private channel info
     - `im:history` - View direct messages
     - `im:read` - View basic DM info
     - `mpim:history` - View group DMs
     - `mpim:read` - View basic group DM info
   - Install the app to your workspace
   - Save the Bot User OAuth Token (starts with `xoxb-`)

4. **Get your Team ID**:
   - Can be found in your Slack URL: `https://app.slack.com/client/T1234567/...`
   - Or in Slack: Click workspace name → Settings & administration → Workspace settings
   - The Team ID starts with `T`

## Quick Start

```bash
# Add the Slack bridge
./bridge.py add slack --instance my-instance \
  --app-token xapp-1-... \
  --bot-token xoxb-... \
  --team-id T1234567

# Register with Matrix server
./bridge.py register slack --instance my-instance

# Start the bridge
./bridge.py start slack --instance my-instance
```

## Configuration

The bridge configuration is automatically generated at:
```
instance_data/{instance_name}/bridges/slack/data/config.yaml
```

Key configuration sections:
- `homeserver`: Matrix server connection details
- `appservice`: Bridge service configuration
- `slack`: Slack-specific settings including tokens
- `bridge`: Permissions and user mapping

## Usage

### Bridging Channels

After the bridge is running:

1. **Invite the bot to a Slack channel**:
   - In Slack: `/invite @YourBotName` in the channel

2. **Bridge to Matrix**:
   - The bridge will automatically create a Matrix room
   - Or manually: `!slack bridge #channel-name`

### Managing Connections

- **List bridged channels**: `!slack list`
- **Unbridge a channel**: `!slack unbridge #channel-name`
- **Get help**: `!slack help`

## Troubleshooting

### Bridge won't start
- Check logs: `./bridge.py logs slack --instance my-instance`
- Verify tokens are correct
- Ensure Socket Mode is enabled in your Slack app

### Messages not bridging
- Verify the bot is in the Slack channel
- Check that the bot has necessary permissions
- Review the permission scopes in your Slack app

### Authentication errors
- Regenerate tokens if they've expired
- Ensure the app is installed to your workspace
- Check that Socket Mode is enabled

## Files

- `config.yaml`: Bridge configuration (auto-generated)
- `registration.yaml`: Matrix appservice registration
- `mautrix-slack.db`: SQLite database for bridge state

## Security Notes

- Keep your tokens secure and never commit them
- Use environment variables in production
- Regularly rotate tokens
- Monitor bridge logs for security events

## References

- mautrix-slack documentation: https://docs.mau.fi/bridges/python/slack/
- Slack API documentation: https://api.slack.com/
