# Google Services OAuth (Individual Setup)

This guide is for one person running MindRoom and creating their own Google OAuth app.

For team/shared deployments, use [Google Services OAuth (Admin Setup)](https://docs.mindroom.chat/deployment/google-services-oauth/index.md).

## What You Need Before Starting

- A Google account
- Access to Google Cloud Console
- A running MindRoom backend/frontend (default backend URL: `http://localhost:8765`)

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

## Step 5: Configure MindRoom Backend

Add this to `.env` (or export in your shell):

```
BACKEND_PORT=8765
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
GOOGLE_PROJECT_ID=your-project-id
GOOGLE_REDIRECT_URI=http://localhost:8765/api/google/callback
```

Restart the MindRoom backend.

## Step 6: Verify Backend Reads Credentials

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

After editing `config.yaml`, restart MindRoom backend to reload configuration.

## Disconnect Later (Optional)

1. In MindRoom frontend, click **Disconnect Google Account**.
1. Optional: also revoke app access in [Google Account Permissions](https://myaccount.google.com/permissions).

## Troubleshooting

### "Admin Setup Required" shown in frontend

Your backend does not have valid Google OAuth env vars yet.

### "Failed to complete OAuth flow"

Check redirect URI exact match between Google Cloud Console and MindRoom backend.

### Access blocked by Google

If your app is in testing mode, ensure your account is listed as a test user.
