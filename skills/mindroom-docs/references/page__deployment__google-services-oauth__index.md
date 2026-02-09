# Google Services OAuth (Admin Setup)

This is the one-time setup for a shared Google OAuth app in MindRoom. After you finish these steps, users only click **Login with Google** in the frontend.

## Who This Is For

Use this guide if you are running MindRoom for a team, organization, or hosted deployment.

If you are a single local user and want to bring your own Google OAuth app, see [Google Services OAuth (Individual Setup)](https://docs.mindroom.chat/deployment/google-services-user-oauth/index.md).

## What You Need Before Starting

- Your backend URL (local example: `http://localhost:8765`, production example: `https://mindroom.example.com`)
- Access to [Google Cloud Console](https://console.cloud.google.com/)
- Access to set backend environment variables

The MindRoom callback path is always:

```
/api/google/callback
```

Your full callback URL is:

```
<your-backend-origin>/api/google/callback
```

## Step 1: Create a Google Cloud Project

1. Open [Google Cloud Console](https://console.cloud.google.com/).
1. Create a new project (or select an existing one).
1. Save the project ID. You will use it as `GOOGLE_PROJECT_ID`.

## Step 2: Enable APIs

1. In Google Cloud Console, go to **APIs & Services → Library**.
1. Enable:
1. Gmail API
1. Google Calendar API
1. Google Drive API
1. Google Sheets API

## Step 3: Configure OAuth Consent Screen

1. Go to **APIs & Services → OAuth consent screen**.
1. User type:
1. `External` for public or mixed users
1. `Internal` for Google Workspace-only
1. Fill required app info and save.
1. Add test users if app is still in testing mode.
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
1. Under **Authorized redirect URIs**, add:
1. Local: `http://localhost:8765/api/google/callback`
1. Production: `https://<your-domain>/api/google/callback`
1. Copy the generated client ID and client secret.

## Step 5: Configure MindRoom Backend Environment

Set these env vars in your backend deployment (`.env`, Kubernetes secret, or hosting config):

```
GOOGLE_CLIENT_ID=your-app-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-app-client-secret
GOOGLE_PROJECT_ID=your-project-id
GOOGLE_REDIRECT_URI=http://localhost:8765/api/google/callback
```

Notes:

- `GOOGLE_REDIRECT_URI` must match one of your Google Console redirect URIs exactly.
- If omitted, MindRoom defaults to `http://localhost:8765/api/google/callback`.

Restart the MindRoom backend after setting env vars.

## Step 6: Verify Backend Is Configured

Run:

```
curl -s http://localhost:8765/api/google/status
```

Expected result includes:

- `"has_credentials": true`

If `connected` is false at this point, that is normal until a user authorizes.

## Step 7: Verify Frontend User Flow

1. Open **Integrations → Google Services**.
1. If setup is correct, the card shows **Ready to Connect**.
1. Users can now click **Login with Google** and authorize access.

## End User Steps (After Admin Setup)

Each user does only this:

1. Open **Integrations → Google Services**.
1. Click **Login with Google**.
1. Approve scopes.
1. Confirm status shows **Connected**.

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

Ensure all three are identical:

- `GOOGLE_REDIRECT_URI` in backend env
- Redirect URI in Google Console
- Actual backend callback URL

### Users cannot authorize while app is in testing mode

Add those users to OAuth consent screen test users.
