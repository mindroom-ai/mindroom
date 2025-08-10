# Gmail Integration with Agno

MindRoom now uses Agno's native Gmail toolkit for better integration and more features.

## Features

Agno's Gmail toolkit provides:
- **Read Operations**: Get latest, unread, starred emails, search by sender/date/context
- **Write Operations**: Create drafts, send emails
- **Advanced Search**: Natural language email search
- **Automatic Authentication**: OAuth2 flow handled by Agno

## Setup Instructions

### 1. Install Dependencies

All required dependencies are already installed in the project:
```bash
# Already in pyproject.toml:
# - google-api-python-client
# - google-auth
# - google-auth-httplib2
# - google-auth-oauthlib
```

### 2. Google Cloud Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing one
3. Enable the Gmail API:
   - Go to "APIs & Services" > "Library"
   - Search for "Gmail API"
   - Click "Enable"

### 3. Create OAuth Credentials

1. Go to "APIs & Services" > "Credentials"
2. Click "Create Credentials" > "OAuth client ID"
3. Configure OAuth consent screen if needed:
   - Choose "External" for user type
   - Fill in required fields
   - Add scope: `https://www.googleapis.com/auth/gmail.modify`
4. For Application type, choose "Desktop app"
5. Name it "MindRoom Gmail Integration"
6. Download the credentials JSON

### 4. Set Environment Variables

Add these to your `.env` file:

```env
# Gmail OAuth credentials for Agno
GOOGLE_CLIENT_ID=your_client_id_here.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your_client_secret_here
GOOGLE_PROJECT_ID=your_project_id_here
GOOGLE_REDIRECT_URI=http://localhost
```

Or export them in your shell:
```bash
export GOOGLE_CLIENT_ID="your_client_id_here.apps.googleusercontent.com"
export GOOGLE_CLIENT_SECRET="your_client_secret_here"
export GOOGLE_PROJECT_ID="your_project_id_here"
export GOOGLE_REDIRECT_URI="http://localhost"
```

### 5. First-Time Authentication

When an agent first uses Gmail tools, Agno will:
1. Open a browser for OAuth consent
2. Ask you to authorize the app
3. Store the tokens securely
4. Reuse tokens for future requests

## Usage in Agents

Add `gmail` to any agent's tools in `config.yaml`:

```yaml
agents:
  assistant:
    display_name: "Assistant"
    role: "General purpose assistant"
    tools:
      - gmail
      - file
    instructions:
      - "Help manage emails and tasks"
```

## Available Gmail Functions

The Agno Gmail toolkit provides these functions:

- `get_latest_emails(max_results=5)` - Get the latest emails
- `get_emails_from_user(sender_email, max_results=5)` - Get emails from specific sender
- `get_unread_emails(max_results=5)` - Get unread emails
- `get_starred_emails(max_results=5)` - Get starred emails
- `get_emails_by_context(query, max_results=5)` - Search by context/content
- `get_emails_by_date(start_date, end_date, max_results=5)` - Get emails in date range
- `create_draft_email(to, subject, body)` - Create a draft
- `send_email(to, subject, body)` - Send an email immediately
- `search_emails(query, max_results=5)` - Natural language search

## Example Commands

```
@mindroom_assistant Show me my latest 5 unread emails
@mindroom_assistant Get emails from boss@company.com
@mindroom_assistant Search for emails about the project deadline
@mindroom_assistant Create a draft email to team@company.com about the meeting
@mindroom_assistant Send an email to support@service.com asking for help
```

## Comparison with Previous Implementation

| Feature | Old (Custom) | New (Agno) |
|---------|--------------|------------|
| Read emails | ✅ Basic | ✅ Advanced |
| Send emails | ❌ | ✅ |
| Create drafts | ❌ | ✅ |
| Search | ✅ Gmail syntax | ✅ Natural language |
| Starred emails | ❌ | ✅ |
| Date filters | ❌ | ✅ |
| Authentication | Manual OAuth | Automatic OAuth |
| Token refresh | Manual | Automatic |

## Troubleshooting

### "Gmail toolkit not available"
- Ensure you have `agno` installed with Gmail support
- Check that environment variables are set correctly

### Authentication Issues
- Make sure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are set
- Verify the Gmail API is enabled in Google Cloud Console
- Check that the OAuth consent screen is configured

### Permission Errors
- Ensure the OAuth scope includes `gmail.modify`
- Re-authenticate if you changed scopes

## Migration from Custom Implementation

The system automatically falls back to the custom implementation if Agno's Gmail toolkit isn't available, so there's no breaking change. However, to get the full benefits:

1. Set up the environment variables as described above
2. The agents will automatically use Agno's toolkit
3. Remove the old `gmail_token.json` if it exists (Agno manages its own tokens)

## Security Notes

- Agno stores OAuth tokens securely
- Tokens are user-specific and isolated
- The Gmail API uses OAuth2 for secure authentication
- Never commit credentials to version control
