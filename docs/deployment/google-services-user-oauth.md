---
icon: lucide/user-round
---

# Google Services OAuth (Individual Setup)

This guide is for one person running MindRoom and creating their own Google OAuth app.

For team/shared deployments, use [Google Services OAuth (Admin Setup)](google-services-oauth.md).

## What You Need Before Starting

- A Google account
- Access to Google Cloud Console
- A running MindRoom backend/frontend (default backend URL: `http://localhost:8765`)

The callback path is always:

```text
/api/google/callback
```

So the default full callback URL is:

```text
http://localhost:8765/api/google/callback
```

## Step 1: Create Google Cloud Project

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project.
3. Save the project ID for `GOOGLE_PROJECT_ID`.

## Step 2: Enable APIs

1. Go to **APIs & Services → Library**.
2. Enable:
   - Gmail API
   - Google Calendar API
   - Google Drive API
   - Google Sheets API

## Step 3: Configure OAuth Consent Screen

1. Go to **APIs & Services → OAuth consent screen**.
2. Choose `External`.
3. Fill required fields and save.
4. Add your own email as a test user.
5. Add scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.modify`
   - `https://www.googleapis.com/auth/gmail.compose`
   - `https://www.googleapis.com/auth/calendar`
   - `https://www.googleapis.com/auth/spreadsheets`
   - `https://www.googleapis.com/auth/drive.file`
   - `openid`
   - `https://www.googleapis.com/auth/userinfo.email`
   - `https://www.googleapis.com/auth/userinfo.profile`

## Step 4: Create OAuth Client ID

1. Go to **APIs & Services → Credentials**.
2. Click **Create Credentials → OAuth client ID**.
3. Choose **Web application**.
4. Add redirect URI:
   - `http://localhost:8765/api/google/callback`
5. Copy client ID and client secret.

## Step 5: Configure MindRoom Backend

Add this to `.env` (or export in your shell):

```bash
BACKEND_PORT=8765
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
GOOGLE_PROJECT_ID=your-project-id
GOOGLE_REDIRECT_URI=http://localhost:8765/api/google/callback
```

Restart the MindRoom backend.

## Step 6: Verify Backend Reads Credentials

Run:

```bash
curl -s http://localhost:8765/api/google/status
```

Expected:
- `"has_credentials": true`

## Step 7: Connect in Frontend

1. Open **Integrations → Google Services**.
2. Click **Login with Google**.
3. Sign in and approve requested scopes.
4. You should see **Connected** and your available services.

## Use With Tools

After connecting, tools that depend on Google auth (for example `gmail`, `google_calendar`, `google_sheets`) can use the shared Google token.

## Disconnect Later (Optional)

1. In MindRoom frontend, click **Disconnect Google Account**.
2. Optional: also revoke app access in [Google Account Permissions](https://myaccount.google.com/permissions).

## Troubleshooting

### "Admin Setup Required" shown in frontend

Your backend does not have valid Google OAuth env vars yet.

### "Failed to complete OAuth flow"

Check redirect URI exact match between Google Cloud Console and MindRoom backend.

### Access blocked by Google

If your app is in testing mode, ensure your account is listed as a test user.
