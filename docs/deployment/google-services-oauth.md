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
Saving redacted client config may omit or blank `client_secret` only when `client_id` is unchanged.
Changing `client_id` requires submitting the matching new `client_secret`.
First-time client config saves require `client_id` and `client_secret`.
Client config services are not worker-grantable and are never mirrored into worker containers.
Client config services cannot be copied into ordinary credential services.

## Environment Variables

Optional account restrictions are service-specific:

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
