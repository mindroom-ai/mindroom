# Gmail Integration Setup Guide

This guide explains how to set up Gmail integration with MindRoom, allowing agents to read and search your emails.

> **Note**: This guide is for INDIVIDUAL setup where each user creates their own OAuth credentials.
> If you're deploying MindRoom for multiple users, see [Gmail OAuth Deployment Guide](./gmail_oauth_deployment.md) for a better approach.

## Prerequisites

1. A Google account
2. Google Cloud Console access (free tier is sufficient)

## Step 1: Create Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Note down your project ID

## Step 2: Enable Gmail API

1. In the Google Cloud Console, go to "APIs & Services" > "Library"
2. Search for "Gmail API"
3. Click on it and press "Enable"

## Step 3: Create OAuth 2.0 Credentials

1. Go to "APIs & Services" > "Credentials"
2. Click "Create Credentials" > "OAuth client ID"
3. If prompted, configure the OAuth consent screen:
   - Choose "External" for user type
   - Fill in the required fields (app name, user support email, etc.)
   - Add your email to test users
   - For scopes, add:
     - `https://www.googleapis.com/auth/gmail.modify`
     - `https://www.googleapis.com/auth/calendar` (optional)
     - `https://www.googleapis.com/auth/drive.file` (optional)

4. Back in credentials, create OAuth client ID:
   - Application type: "Web application"
   - Name: "MindRoom Gmail Integration"
   - Authorized redirect URIs: `http://localhost:8765/api/gmail/callback`
   - Note: Change the port if you're using a different BACKEND_PORT

5. Download the credentials JSON file

## Step 4: Configure MindRoom

### Option A: Using .env File (Recommended)

Create a `.env` file in the `widget` directory:

```bash
cd /path/to/mindroom/widget
cp .env.example .env
```

Edit the `.env` file and add your credentials:
```bash
BACKEND_PORT=8765
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
```

### Option B: Using Environment Variables

Set these environment variables before starting the widget:

```bash
export BACKEND_PORT=8765
export GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export GOOGLE_CLIENT_SECRET="your-client-secret"
```

## Step 5: Connect Gmail in Widget

1. Start the MindRoom widget:
   ```bash
   cd widget
   ./run.sh
   ```

2. Open the widget in your browser (usually http://localhost:5173)

3. Navigate to the "Gmail" tab

4. Click "Connect Gmail"

5. Sign in with your Google account and authorize the app

6. You should see "Connected" status with your email address

## Step 6: Add Gmail Tool to Agents

You can now add the `gmail` tool to any agent in your configuration:

```yaml
agents:
  email_assistant:
    display_name: "Email Assistant"
    role: "Help manage and respond to emails"
    tools:
      - gmail
      - file
    instructions:
      - "Search for important emails"
      - "Summarize unread messages"
      - "Draft responses when asked"
```

## Available Gmail Tools

The Gmail integration provides three tools:

1. **gmail_search**: Search emails with Gmail query syntax
   - Examples: `is:unread`, `from:boss@company.com`, `subject:meeting`

2. **gmail_latest**: Read the latest emails from inbox

3. **gmail_unread**: Read only unread emails

## Security Notes

- MindRoom only requests **read-only** access to your Gmail
- Credentials are stored locally on your machine
- The OAuth token can be revoked at any time from your Google account settings
- Never share your `gmail_token.json` or credentials files

## Troubleshooting

### "Gmail OAuth credentials not configured"
- Make sure you've set the environment variables or placed the credentials file correctly

### "Failed to complete OAuth flow"
- Check that the redirect URI matches exactly: `http://localhost:8000/api/gmail/callback`
- Ensure the Gmail API is enabled in your Google Cloud project

### "Gmail not connected" in agents
- The Gmail token is shared between the widget and agents
- Make sure you've connected Gmail through the widget first

## Disconnecting Gmail

To disconnect Gmail:
1. Click "Disconnect" in the Gmail tab of the widget
2. The stored token will be deleted
3. Optionally, revoke access in your [Google Account settings](https://myaccount.google.com/permissions)
