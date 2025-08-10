# Gmail OAuth Deployment Guide

This guide explains how to deploy MindRoom with Gmail integration for your users.

## Option 1: Shared OAuth App (Recommended for Teams/Products)

With this approach, you create ONE OAuth app that all your users share. Users just click "Connect Gmail" without any setup.

### Advantages
- ✅ Simple user experience - just click "Connect"
- ✅ No technical setup required for users
- ✅ You control the OAuth app
- ✅ Professional appearance

### Disadvantages
- ❌ Requires Google OAuth verification for production use
- ❌ You're responsible for the OAuth app
- ❌ Limited to 100 test users until verified

### Setup Steps

1. **Create your OAuth App in Google Cloud Console**
   - Go to https://console.cloud.google.com/
   - Create a new project or select existing
   - Enable Gmail API
   - Create OAuth 2.0 credentials (Web application)

2. **Configure OAuth Consent Screen**
   - Choose "External" user type
   - Fill in app information
   - Add scopes:
     - `https://www.googleapis.com/auth/gmail.modify`
     - `https://www.googleapis.com/auth/calendar`
     - `https://www.googleapis.com/auth/drive.file`
   - Add test users (for testing phase)

3. **Set Multiple Redirect URIs** (to support different ports):
   ```
   http://localhost:8765/api/gmail/callback
   http://localhost:8765/api/auth/google/callback
   http://localhost:8000/api/gmail/callback
   http://localhost:8000/api/auth/google/callback
   http://localhost:8001/api/gmail/callback
   http://localhost:8001/api/auth/google/callback
   ```

4. **Configure MindRoom Backend**

   Create `.env` file in `widget/` directory:
   ```bash
   # Shared OAuth app credentials
   MINDROOM_OAUTH_CLIENT_ID=your-app-client-id.apps.googleusercontent.com
   MINDROOM_OAUTH_CLIENT_SECRET=your-app-client-secret
   BACKEND_PORT=8765
   ```

5. **For Production (>100 users)**
   - Submit for OAuth verification
   - Provide privacy policy
   - Provide terms of service
   - Wait for Google approval (can take weeks)

## Option 2: Individual OAuth (Current Setup)

Each user creates their own Google Cloud project and OAuth credentials.

### Advantages
- ✅ No verification needed
- ✅ Unlimited users
- ✅ Users control their own data access

### Disadvantages
- ❌ Complex setup for non-technical users
- ❌ Each user needs Google Cloud Console access
- ❌ Poor user experience

### Setup Steps

Users follow the guide in `docs/gmail_setup.md` to:
1. Create their own Google Cloud project
2. Enable Gmail API
3. Create OAuth credentials
4. Add credentials to `.env` file

## Option 3: Hybrid Approach

Offer both options:
- Provide a shared OAuth app for easy setup
- Allow power users to bring their own credentials

### Implementation

The backend already supports this. It checks for credentials in this order:
1. `MINDROOM_OAUTH_CLIENT_ID` (shared app)
2. `GOOGLE_CLIENT_ID` (individual credentials)

Users who want to use their own credentials can still do so by setting:
```bash
GOOGLE_CLIENT_ID=their-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=their-client-secret
```

## Security Considerations

### For Shared OAuth App
- Never commit credentials to git
- Use environment variables or secrets management
- Implement proper token storage and encryption
- Consider implementing user access controls
- Monitor OAuth app usage

### Token Storage
- Tokens are stored in `google_token.json`
- This file contains refresh tokens - keep it secure
- Consider encrypting tokens at rest
- Implement token rotation

## Troubleshooting

### "The OAuth client was not found"
- Verify Client ID is correct
- Check if OAuth app is enabled
- Ensure redirect URIs match exactly

### "Access blocked: Authorization Error"
- App might be in testing mode
- User email needs to be in test users list
- Or submit for OAuth verification

### "Redirect URI mismatch"
- Add all possible redirect URIs to Google Cloud Console
- Check that ports match (8765, 8000, 8001, etc.)
