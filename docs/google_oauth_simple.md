# Simplified Google OAuth Integration

## Overview

MindRoom now provides a simplified "Login with Google" experience where users don't need to:
- Create their own Google Cloud project
- Generate API keys
- Configure OAuth credentials
- Understand complex authentication flows

## How It Works

### For Users
1. Click "Login with Google" button in the widget
2. Authorize MindRoom to access their Google services
3. Done! Agents can now use Gmail, Calendar, and Drive

### Behind the Scenes
1. MindRoom uses its own Google Cloud project credentials
2. OAuth tokens are stored locally on the user's machine
3. Agno handles the integration with Google services
4. Tokens auto-refresh when needed

## Architecture

```
User clicks "Login with Google"
        ↓
Widget Frontend (React)
        ↓
Widget Backend (FastAPI)
        ↓
Google OAuth Flow (using MindRoom's credentials)
        ↓
Token stored locally (google_token.json)
        ↓
Agno Gmail/Calendar/Drive toolkits use token
        ↓
Agents access Google services
```

## Setup for MindRoom Administrators

### 1. Create Google Cloud Project (One-time setup)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project called "MindRoom"
3. Enable these APIs:
   - Gmail API
   - Google Calendar API
   - Google Drive API

### 2. Configure OAuth Consent Screen

1. Go to "APIs & Services" > "OAuth consent screen"
2. Choose "External" user type
3. Fill in:
   - App name: "MindRoom"
   - User support email: support@mindroom.ai
   - App logo: Upload MindRoom logo
   - App domain: mindroom.ai
   - Privacy policy: https://mindroom.ai/privacy
   - Terms of service: https://mindroom.ai/terms

### 3. Create OAuth Credentials

1. Go to "APIs & Services" > "Credentials"
2. Click "Create Credentials" > "OAuth client ID"
3. Choose "Web application"
4. Configure:
   - Name: "MindRoom OAuth Client"
   - Authorized JavaScript origins:
     - http://localhost:3003
     - http://localhost:5173
     - https://app.mindroom.ai
   - Authorized redirect URIs:
     - http://localhost:8000/api/auth/google/callback
     - https://api.mindroom.ai/auth/google/callback

### 4. Set Environment Variables

Add to `.env` file:
```env
GOOGLE_CLIENT_ID=your-mindroom-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-mindroom-client-secret
GOOGLE_PROJECT_ID=mindroom-project
GOOGLE_REDIRECT_URI=http://localhost:8000/api/auth/google/callback
```

## Security Considerations

### What's Stored Locally
- OAuth access token (for API calls)
- Refresh token (to get new access tokens)
- User's email address
- Token expiry time

### What's NOT Stored
- User's Google password
- MindRoom's client secret (server-side only)
- Any email/calendar/drive content

### Token Security
- Tokens are stored in `google_token.json`
- File permissions should be restricted to user only
- Tokens expire and auto-refresh
- Users can revoke access anytime from Google Account settings

## Available Services

Once connected, agents can use:

### Gmail (via Agno's Gmail toolkit)
- `get_latest_emails()` - Get recent emails
- `get_unread_emails()` - Get unread messages
- `search_emails()` - Natural language search
- `send_email()` - Send emails
- `create_draft_email()` - Create drafts
- `get_starred_emails()` - Get starred messages
- `get_emails_by_date()` - Filter by date range
- `get_emails_from_user()` - Filter by sender

### Google Calendar (coming soon)
- View events
- Create events
- Update events
- Check availability
- Send invitations

### Google Drive (coming soon)
- List files
- Read documents
- Create files
- Share files
- Search content

## User Experience Flow

### First-Time Setup
1. User opens MindRoom widget
2. Goes to "Google" tab
3. Sees simple explanation: "Login to enable Gmail, Calendar, and Drive"
4. Clicks "Login with Google" button
5. Google OAuth popup appears
6. User logs in and grants permissions
7. Popup closes, widget shows "Connected"
8. All agents now have access to Google services

### Using Google Services
```
User: @assistant check my unread emails
Assistant: You have 3 unread emails:
1. From boss@company.com: "Project Update"
2. From newsletter@service.com: "Weekly Digest"
3. From friend@gmail.com: "Lunch tomorrow?"

User: @assistant send an email to boss@company.com saying I'll have the report ready by Friday
Assistant: I've sent the email to boss@company.com confirming the report will be ready by Friday.
```

## Advantages of This Approach

### For Users
- **Zero configuration** - No API keys or technical setup
- **One-click setup** - Just "Login with Google"
- **Familiar flow** - Same as any "Login with Google" button
- **Secure** - OAuth tokens, no password storage
- **Reversible** - Can disconnect anytime

### For Developers
- **Centralized management** - One Google Cloud project
- **Easier support** - No user configuration issues
- **Better analytics** - Track usage across all users
- **Consistent experience** - Same flow for everyone

## Migration from Old System

If users have the old Gmail integration:
1. Remove old `gmail_token.json` and `gmail_credentials.json`
2. Click "Login with Google" in the new interface
3. Agents automatically use new integration

## Troubleshooting

### "Google OAuth is not configured"
- MindRoom admin needs to set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET

### "Failed to complete Google login"
- Check if popup was blocked
- Ensure redirect URI matches configuration
- Try clearing browser cache

### Agents can't access Gmail
- Check if Google is connected in widget
- Verify token file exists: `google_token.json`
- Try disconnecting and reconnecting

## Future Enhancements

1. **Selective Permissions** - Let users choose which services to enable
2. **Multiple Accounts** - Support multiple Google accounts
3. **Workspace Integration** - Support Google Workspace domains
4. **Mobile Support** - OAuth flow for mobile apps
5. **Token Encryption** - Encrypt stored tokens for extra security
