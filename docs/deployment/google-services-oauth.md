---
icon: lucide/mail
---

# Google Services OAuth

MindRoom uses the generic OAuth framework for Google tools.
Each Google service has its own provider ID, callback URL, token service, and editable tool settings service.
There is no bundled `/api/google/*` OAuth flow.

## Providers

| Tool | Provider ID | Callback path | Token service | Settings service | Scopes |
| --- | --- | --- | --- | --- | --- |
| Google Drive | `google_drive` | `/api/oauth/google_drive/callback` | `google_drive_oauth` | `google_drive` | Drive read-only plus OpenID email/profile |
| Google Calendar | `google_calendar` | `/api/oauth/google_calendar/callback` | `google_calendar_oauth` | `google_calendar` | Calendar read/write plus OpenID email/profile |
| Google Sheets | `google_sheets` | `/api/oauth/google_sheets/callback` | `google_sheets_oauth` | `google_sheets` | Sheets read/write, plus OpenID email/profile |
| Gmail | `google_gmail` | `/api/oauth/google_gmail/callback` | `google_gmail_oauth` | `gmail` | Gmail readonly, modify, compose plus OpenID email/profile |

## Google Cloud Setup

Create an OAuth client in Google Cloud Console.
Enable only the APIs for the tools you plan to use.
Add one authorized redirect URI for each provider you enable.

For local development, the redirect URIs are:

```text
http://localhost:8765/api/oauth/google_drive/callback
http://localhost:8765/api/oauth/google_calendar/callback
http://localhost:8765/api/oauth/google_sheets/callback
http://localhost:8765/api/oauth/google_gmail/callback
```

For production, replace the origin with your public MindRoom origin.

## Environment Variables

You can configure each provider independently:

```bash
GOOGLE_DRIVE_CLIENT_ID=...
GOOGLE_DRIVE_CLIENT_SECRET=...
GOOGLE_DRIVE_REDIRECT_URI=https://mindroom.example.com/api/oauth/google_drive/callback

GOOGLE_CALENDAR_CLIENT_ID=...
GOOGLE_CALENDAR_CLIENT_SECRET=...
GOOGLE_CALENDAR_REDIRECT_URI=https://mindroom.example.com/api/oauth/google_calendar/callback

GOOGLE_SHEETS_CLIENT_ID=...
GOOGLE_SHEETS_CLIENT_SECRET=...
GOOGLE_SHEETS_REDIRECT_URI=https://mindroom.example.com/api/oauth/google_sheets/callback

GOOGLE_GMAIL_CLIENT_ID=...
GOOGLE_GMAIL_CLIENT_SECRET=...
GOOGLE_GMAIL_REDIRECT_URI=https://mindroom.example.com/api/oauth/google_gmail/callback
```

The providers also accept shared `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` as a fallback.
Use service-specific variables when different Google Cloud OAuth clients or restrictions are needed.

Optional restrictions are service-specific:

```bash
GOOGLE_DRIVE_ALLOWED_EMAIL_DOMAINS=example.com
GOOGLE_CALENDAR_ALLOWED_HOSTED_DOMAINS=example.com
GOOGLE_SHEETS_ALLOWED_EMAIL_DOMAINS=example.com
GOOGLE_GMAIL_ALLOWED_HOSTED_DOMAINS=example.com
```

## Runtime Behavior

Dashboard and agent-issued connect links use `/api/oauth/{provider}/connect` or `/api/oauth/{provider}/authorize`.
OAuth callback state is stored server-side as an opaque token and bound to the authenticated dashboard user and scoped credential target.
Disconnecting a provider removes both the token service and that provider's settings service for the selected scope.
