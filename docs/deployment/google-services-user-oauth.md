---
icon: lucide/user-round
---

# Google Services OAuth For Individuals

This guide is for one person running MindRoom with their own Google OAuth client.
MindRoom uses per-service generic OAuth providers instead of a legacy all-Google route.

## Choose Providers

Enable only the APIs your agents need.
Add the matching tool to the agent config.

```yaml
agents:
  personal:
    display_name: Personal
    role: Help with my Google workspace
    worker_scope: user_agent
    tools:
      - google_drive
      - google_calendar
      - google_sheets
      - gmail
```

## Create OAuth Credentials

Open Google Cloud Console and create an OAuth client.
Add one redirect URI per provider you use.

```text
http://localhost:8765/api/oauth/google_drive/callback
http://localhost:8765/api/oauth/google_calendar/callback
http://localhost:8765/api/oauth/google_sheets/callback
http://localhost:8765/api/oauth/google_gmail/callback
```

## Configure MindRoom

For a single personal OAuth client, store shared Google OAuth app client config under `google_oauth_client` through the dashboard credentials API or UI:

```json
{
  "client_id": "your-client-id.apps.googleusercontent.com",
  "client_secret": "your-client-secret"
}
```

For provider-specific client config, use `google_drive_oauth_client`, `google_calendar_oauth_client`, `google_sheets_oauth_client`, or `google_gmail_oauth_client`.
Provider-specific client config wins over the shared `google_oauth_client` service.
MindRoom stores OAuth app client config separately from user OAuth tokens and never mirrors it into worker containers.

Environment variables remain available as bootstrap and fallback.
For a single personal OAuth client, set the shared fallback client variables:

```bash
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
```

When using standalone dashboard API-key auth, also set `MINDROOM_OWNER_USER_ID` to your Matrix user ID, such as `@alice:matrix.example.com`.

For explicit service-specific redirect URIs, set:

```bash
GOOGLE_DRIVE_REDIRECT_URI=http://localhost:8765/api/oauth/google_drive/callback
GOOGLE_CALENDAR_REDIRECT_URI=http://localhost:8765/api/oauth/google_calendar/callback
GOOGLE_SHEETS_REDIRECT_URI=http://localhost:8765/api/oauth/google_sheets/callback
GOOGLE_GMAIL_REDIRECT_URI=http://localhost:8765/api/oauth/google_gmail/callback
```

You may instead set `GOOGLE_DRIVE_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_SHEETS_CLIENT_ID`, or `GOOGLE_GMAIL_CLIENT_ID` with matching secrets.

## Connect

Open the MindRoom dashboard and connect the integration required by each tool.
If an agent tries a Google tool before it is connected, the tool result includes a MindRoom connect URL for that exact provider and agent scope.
After the browser OAuth flow completes, retry the original request.

OAuth tokens are stored under provider token services such as `google_drive_oauth`.
Editable tool settings are stored separately under services such as `google_drive`, `google_calendar`, `google_sheets`, and `gmail`.
OAuth app client config is stored separately under services such as `google_oauth_client` or `google_drive_oauth_client`.
