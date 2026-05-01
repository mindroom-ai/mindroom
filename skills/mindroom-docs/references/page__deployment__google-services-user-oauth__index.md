# Google Services OAuth For Individuals

This guide is for one person running MindRoom with their own Google OAuth client.
MindRoom uses per-service generic OAuth providers instead of a legacy all-Google route.

## Choose Providers

Enable only the APIs your agents need.
Add the matching tool to the agent config.

```
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

```
http://localhost:8765/api/oauth/google_drive/callback
http://localhost:8765/api/oauth/google_calendar/callback
http://localhost:8765/api/oauth/google_sheets/callback
http://localhost:8765/api/oauth/google_gmail/callback
```

## Configure MindRoom

For a single personal OAuth client, store shared Google OAuth app client config under `google_oauth_client` through the dashboard credentials API or raw credentials editor:

```
{
  "client_id": "your-client-id.apps.googleusercontent.com",
  "client_secret": "your-client-secret"
}
```

For provider-specific client config, use `google_drive_oauth_client`, `google_calendar_oauth_client`, `google_sheets_oauth_client`, or `google_gmail_oauth_client`.
Provider-specific client config wins over the shared `google_oauth_client` service.
The shared `google_oauth_client` service supplies only the shared client ID and secret.
MindRoom derives each provider's redirect URI from `MINDROOM_PUBLIC_URL` or the local default origin.
MindRoom stores OAuth app client config separately from user OAuth tokens and never mirrors it into worker containers.
First-time dashboard client setup requires `client_id` and `client_secret`; later edits may leave either field blank to keep the stored value.

When using standalone dashboard API-key auth, also set `MINDROOM_OWNER_USER_ID` to your Matrix user ID, such as `@alice:matrix.example.com`.
Do not use `MINDROOM_OWNER_USER_ID` as the identity model for hosted multi-user private agents. Use [Trusted Upstream Browser Auth](https://docs.mindroom.chat/deployment/trusted-upstream-auth/index.md) for those deployments.

## Connect

Open the MindRoom dashboard and connect the integration required by each tool.
If an agent tries a Google tool before it is connected, the tool result includes a MindRoom connect URL for that exact provider and agent scope.
After the browser OAuth flow completes, retry the original request.

OAuth tokens are stored under provider token services such as `google_drive_oauth`.
Editable tool settings are stored separately under services such as `google_drive`, `google_calendar`, `google_sheets`, and `gmail`.
OAuth app client config is stored separately under services such as `google_oauth_client` or `google_drive_oauth_client`.
