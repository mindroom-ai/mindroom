# Google Services OAuth (Individual Setup)

This guide is for one person running MindRoom and creating their own Google OAuth app for the legacy Google Services dashboard integration.
For personal-agent Google Drive access through the generic OAuth framework, use the Google Drive section below.

For team/shared deployments, use [Google Services OAuth (Admin Setup)](https://docs.mindroom.chat/deployment/google-services-oauth/index.md).

## What You Need Before Starting

- A Google account
- Access to Google Cloud Console
- A running MindRoom instance with the bundled dashboard (default URL: `http://localhost:8765`)

The callback path is always:

```
/api/google/callback
```

So the default full callback URL is:

```
http://localhost:8765/api/google/callback
```

## Step 1: Create Google Cloud Project

1. Open [Google Cloud Console](https://console.cloud.google.com/).
1. Create or select a project.
1. Save the project ID for `GOOGLE_PROJECT_ID`.

## Step 2: Enable APIs

1. Go to **APIs & Services → Library**.
1. Enable:
1. Gmail API
1. Google Calendar API
1. Google Drive API
1. Google Sheets API

## Step 3: Configure OAuth Consent Screen

1. Go to **APIs & Services → OAuth consent screen**.
1. Choose `External`.
1. Fill required fields and save.
1. Add your own email as a test user.
1. Add scopes:
1. `https://www.googleapis.com/auth/gmail.readonly`
1. `https://www.googleapis.com/auth/gmail.modify`
1. `https://www.googleapis.com/auth/gmail.compose`
1. `https://www.googleapis.com/auth/calendar`
1. `https://www.googleapis.com/auth/spreadsheets`
1. `https://www.googleapis.com/auth/drive.file`
1. `openid`
1. `https://www.googleapis.com/auth/userinfo.email`
1. `https://www.googleapis.com/auth/userinfo.profile`

## Step 4: Create OAuth Client ID

1. Go to **APIs & Services → Credentials**.
1. Click **Create Credentials → OAuth client ID**.
1. Choose **Web application**.
1. Add redirect URI:
1. `http://localhost:8765/api/google/callback`
1. Copy client ID and client secret.

## Step 5: Configure MindRoom

Add this to `.env` (or export in your shell):

```
MINDROOM_PORT=8765
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
GOOGLE_PROJECT_ID=your-project-id
GOOGLE_REDIRECT_URI=http://localhost:8765/api/google/callback
```

Restart MindRoom.

## Step 6: Verify MindRoom Reads Credentials

Run:

```
curl -s http://localhost:8765/api/google/status
```

Expected:

- `"has_credentials": true`

## Step 7: Connect in Frontend

1. Open **Integrations → Google Services**.
1. Click **Login with Google**.
1. Sign in and approve requested scopes.
1. You should see **Connected** and your available services.

## Step 8: Enable Google Tools in `config.yaml`

After OAuth is connected, add Google tools to your agent config:

```
agents:
  email_assistant:
    display_name: Email Assistant
    role: Help manage and respond to emails
    tools:
      - gmail
      - google_calendar
      - google_sheets
    instructions:
      - Search important unread emails first.
      - Draft replies and ask for confirmation before sending.
```

Gmail tool capabilities include:

- `gmail_search`: Search emails with Gmail query syntax (for example `is:unread` or `from:boss@company.com`)
- `gmail_latest`: Read latest inbox emails
- `gmail_unread`: Read unread emails only

After editing `config.yaml`, restart MindRoom to reload configuration.

## Disconnect Later (Optional)

1. In MindRoom frontend, click **Disconnect Google Account**.
1. Optional: also revoke app access in [Google Account Permissions](https://myaccount.google.com/permissions).

## Google Drive for Personal Agents

<<<<<<< HEAD
The generic Google Drive OAuth provider is separate from the legacy `/api/google/*` Google Services integration.
It stores credentials under the scoped `google_drive` service for the authenticated requester and selected agent.
Use it when a private personal agent needs to search or read a user's Drive files without sharing that token with other users or agents.
=======
The generic Google Drive OAuth provider is separate from the legacy `/api/google/*` Google Services integration. It stores OAuth tokens under the scoped `google_drive_oauth` service for the authenticated requester and selected agent. It stores editable tool settings, such as capability toggles and file-size limits, under the separate `google_drive` service. Use it when a private personal agent needs to search or read a user's Drive files without sharing that token with other users or agents.
>>>>>>> e18a7ccdd (split oauth tokens from tool settings)

The generic callback path is:

```
/api/oauth/google_drive/callback
```

The default local callback URL is:

```
http://localhost:8765/api/oauth/google_drive/callback
```

Enable only the Google Drive API when the agent only needs Drive file search and read access.
The built-in provider requests Drive read scopes plus OpenID email/profile scopes for identity validation.
It does not request Gmail, Calendar, or Sheets scopes.

Configure MindRoom with these environment variables:

```
MINDROOM_PORT=8765
GOOGLE_DRIVE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_DRIVE_CLIENT_SECRET=your-client-secret
GOOGLE_DRIVE_REDIRECT_URI=http://localhost:8765/api/oauth/google_drive/callback
```

Optional deployment restrictions can be configured without changing MindRoom core:

```
GOOGLE_DRIVE_ALLOWED_EMAIL_DOMAINS=example.com,example.org
GOOGLE_DRIVE_ALLOWED_HOSTED_DOMAINS=example.com
```

Add the tool to a private agent:

```
agents:
  drive_assistant:
    display_name: Drive Assistant
    role: Search and read my Drive files
    worker_scope: user_agent
    tools:
      - google_drive
```

<<<<<<< HEAD
If credentials are missing, the tool returns a MindRoom connect URL for the selected agent.
That URL contains an opaque connect token for the current worker credential target instead of exposing the Matrix requester in the URL.
MindRoom verifies that the authenticated dashboard user resolves to the same requester before it stores credentials.
For standalone personal deployments, pairing normally sets `MINDROOM_OWNER_USER_ID`; set it manually if agent-issued links need to resolve to the owner Matrix user.
The user opens that URL, completes Google OAuth, and retries the original request.
Tokens are stored in MindRoom credential storage for the resolved requester and agent scope, not in `config.yaml`.
=======
If credentials are missing, the tool returns a MindRoom connect URL for the selected agent. That URL contains an opaque connect token for the current worker credential target instead of exposing the Matrix requester in the URL. MindRoom verifies that the authenticated dashboard user resolves to the same requester before it stores credentials. For standalone personal deployments, pairing normally sets `MINDROOM_OWNER_USER_ID`; set it manually if agent-issued links need to resolve to the owner Matrix user. The user opens that URL, completes Google OAuth, and retries the original request. Tokens are stored in MindRoom credential storage for the resolved requester and agent scope, not in `config.yaml`. Dashboard tool settings are stored separately so changing `google_drive` options cannot overwrite or expose OAuth token fields.
>>>>>>> e18a7ccdd (split oauth tokens from tool settings)

## Troubleshooting

### "Admin Setup Required" shown in frontend

MindRoom does not have valid Google OAuth env vars yet.

### "Failed to complete OAuth flow"

Check redirect URI exact match between Google Cloud Console and MindRoom.

### Access blocked by Google

If your app is in testing mode, ensure your account is listed as a test user.
