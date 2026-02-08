---
icon: lucide/user-round
---

# Google Services OAuth (Individual Setup)

This guide is for a single user running MindRoom and bringing their own Google OAuth app.

For team/shared deployments, use [Google Services OAuth (Admin Setup)](google-services-oauth.md).

## Prerequisites

- A Google account
- Access to Google Cloud Console
- A running MindRoom backend/frontend

## Step 1: Create Google OAuth Credentials

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create/select a project.
3. Enable APIs:
   - Gmail API
   - Google Calendar API
   - Google Drive API
   - Google Sheets API
4. Configure OAuth consent screen and add yourself as a test user.
5. Create OAuth 2.0 Web Application credentials.
6. Add redirect URI:
   - `http://localhost:8765/api/google/callback`
   - Adjust host/port/path if your backend runs elsewhere.

## Step 2: Configure MindRoom

Set env vars in `.env` or shell:

```bash
BACKEND_PORT=8765
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
GOOGLE_PROJECT_ID=your-project-id
GOOGLE_REDIRECT_URI=http://localhost:8765/api/google/callback
```

Restart MindRoom backend after changes.

## Step 3: Connect in Frontend

1. Open **Integrations → Google Services**.
2. Click **Login with Google**.
3. Approve the requested scopes.
4. You should see **Connected** and your available services.

## Use With Tools

After connecting, tools that depend on Google auth (for example `gmail`, `google_calendar`, `google_sheets`) can use the shared Google token.

## Troubleshooting

### "Admin Setup Required" shown in frontend

Your backend does not have valid Google OAuth env vars yet.

### "Failed to complete OAuth flow"

Check redirect URI exact match between Google Cloud Console and MindRoom backend.

### Access blocked by Google

If your app is in testing mode, ensure your account is listed as a test user.
