---
icon: lucide/mail
---

# Google Services OAuth

MindRoom uses the generic OAuth framework for Google tools.
Each Google service has its own provider ID, callback URL, token service, OAuth client config service, and editable tool settings service.
There is no bundled `/api/google/*` OAuth flow.

## Providers

| Tool | Provider ID | Callback path | Token service | Client config service | Settings service | Scopes |
| --- | --- | --- | --- | --- | --- | --- |
| Google Drive | `google_drive` | `/api/oauth/google_drive/callback` | `google_drive_oauth` | `google_drive_oauth_client` | `google_drive` | Drive read-only plus OpenID email/profile |
| Google Calendar | `google_calendar` | `/api/oauth/google_calendar/callback` | `google_calendar_oauth` | `google_calendar_oauth_client` | `google_calendar` | Calendar read/write plus OpenID email/profile |
| Google Sheets | `google_sheets` | `/api/oauth/google_sheets/callback` | `google_sheets_oauth` | `google_sheets_oauth_client` | `google_sheets` | Sheets read/write, plus OpenID email/profile |
| Gmail | `google_gmail` | `/api/oauth/google_gmail/callback` | `google_gmail_oauth` | `google_gmail_oauth_client` | `gmail` | Gmail readonly, modify, compose plus OpenID email/profile |

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

## Stored Client Config

OAuth app client config is stored through normal credential storage, separate from user OAuth tokens and editable tool settings.
Use one provider-specific service when one Google Cloud OAuth client should apply to only that provider.
Use `google_oauth_client` when one shared Google Cloud OAuth client should apply to every Google provider.
Provider-specific services win over `google_oauth_client`.
Environment variables remain supported as bootstrap and fallback.

Store these fields on the client config service:

```json
{
  "client_id": "your-client-id.apps.googleusercontent.com",
  "client_secret": "your-client-secret",
  "redirect_uri": "https://mindroom.example.com/api/oauth/google_drive/callback"
}
```

`redirect_uri` is optional when `MINDROOM_PUBLIC_URL` or the local default origin is correct.
Only provider-specific client config services use stored `redirect_uri`.
The shared `google_oauth_client` service ignores `redirect_uri` and derives each provider's callback URI.
Dashboard credential responses redact `client_secret`.
Saving redacted client config preserves the existing `client_secret` when the field is omitted.
Client config services are not worker-grantable and are never mirrored into worker containers.
Client config services cannot be copied into ordinary credential services.

## Environment Variables

You can still configure each provider independently through environment variables:

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
Disconnecting a provider removes the token service for the selected scope and preserves that provider's editable tool settings.
