---
icon: lucide/mail
---

# Google Services OAuth (Admin Setup)

This guide covers the one-time admin setup for Google Services in MindRoom (Gmail, Calendar, Drive, and Sheets).

After this is configured, end users only need to click **Login with Google** in the MindRoom frontend.

## Who This Is For

Use this guide if you are running MindRoom for a team, organization, or hosted deployment.

If you are a single local user and want to bring your own Google OAuth app, see [Google Services OAuth (Individual Setup)](google-services-user-oauth.md).

## Overview

MindRoom backend uses these environment variables for Google OAuth:

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REDIRECT_URI` (optional, defaults to `http://localhost:8765/api/google/callback`)
- `GOOGLE_PROJECT_ID` (optional metadata)

The OAuth callback endpoint in MindRoom is:

```text
/api/google/callback
```

## Step 1: Create OAuth App in Google Cloud

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project.
3. Enable these APIs:
   - Gmail API
   - Google Calendar API
   - Google Drive API
   - Google Sheets API
4. Configure OAuth consent screen:
   - User type: `External` (or `Internal` for Workspace-only)
   - Add test users while app is in testing mode
   - Add these scopes:
     - `https://www.googleapis.com/auth/gmail.readonly`
     - `https://www.googleapis.com/auth/gmail.modify`
     - `https://www.googleapis.com/auth/gmail.compose`
     - `https://www.googleapis.com/auth/calendar`
     - `https://www.googleapis.com/auth/spreadsheets`
     - `https://www.googleapis.com/auth/drive.file`
     - `openid`
     - `https://www.googleapis.com/auth/userinfo.email`
     - `https://www.googleapis.com/auth/userinfo.profile`
5. Create OAuth 2.0 credentials:
   - Application type: `Web application`
   - Add authorized redirect URI(s), for example:
     - `http://localhost:8765/api/google/callback` (local)
     - `https://<your-domain>/api/google/callback` (production)

## Step 2: Configure MindRoom Backend

Set environment variables for the backend process:

```bash
GOOGLE_CLIENT_ID=your-app-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-app-client-secret
GOOGLE_PROJECT_ID=your-project-id
GOOGLE_REDIRECT_URI=http://localhost:8765/api/google/callback
```

You can set them in `.env` (local) or your secret manager/deployment config (production).

## Step 3: Verify in MindRoom Frontend

1. Open **Integrations → Google Services**.
2. If setup is correct, the card shows **Ready to Connect**.
3. Users can now click **Login with Google** and authorize access.

## Production Notes

- Apps in testing mode are limited to test users.
- For broad public usage, complete Google OAuth verification (consent screen, policies, branding, etc.).
- Never commit `GOOGLE_CLIENT_SECRET` to git.

## Security Notes

- OAuth access/refresh tokens are stored in MindRoom credentials storage, typically:
  - `mindroom_data/credentials/google_credentials.json`
- Restrict filesystem access to your MindRoom data directory.
- Revoke app access from Google account settings if needed.

## Troubleshooting

### "Google OAuth is not configured"

`GOOGLE_CLIENT_ID` or `GOOGLE_CLIENT_SECRET` is missing in backend environment.

### "Redirect URI mismatch"

Ensure the URI in Google Cloud Console exactly matches the backend callback URL.

### Users cannot authorize while app is in testing mode

Add those users to OAuth consent screen test users.
