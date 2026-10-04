---
icon: simple/googlecloud
---

# Google Services OAuth

This page covers the Google OAuth providers behind the Google Drive, Docs, Calendar, Sheets, Tasks, and Gmail tools: which scopes each requests and why, how to set up your own Google Cloud OAuth client, and how to restrict which Google accounts may connect.
Paired local installations need no Google Cloud setup and use MindRoom's provisioned client; see [Google Services OAuth For Local Installs](google-services-user-oauth.md).
Use a custom client when MindRoom is opened on any address other than a local loopback address such as `localhost` or `127.0.0.1`, when the installation is not paired, or when you need organization-specific Google policies or your own consent-screen branding.
The provisioned client works only on a loopback address; other addresses get `The provisioned OAuth client is available only when MindRoom is opened on localhost. Set MINDROOM_PUBLIC_URL (or MINDROOM_BASE_URL) and configure a custom OAuth client for remote access.`
How connections are made, stored per credential scope, disconnected, and reset is covered in the [OAuth Integration Framework](../oauth-framework.md).

## Providers

Each Google tool has its own provider, so users connect and approve each service separately.

| Tool | Provider ID | Token service | Client config service | Settings service | Google scopes |
| --- | --- | --- | --- | --- | --- |
| Google Drive | `google_drive` | `google_drive_oauth` | `google_drive_oauth_client` | `google_drive` | `drive` |
| Google Docs | `google_docs` | `google_docs_oauth` | `google_docs_oauth_client` | `google_docs` | `documents` |
| Google Calendar | `google_calendar` | `google_calendar_oauth` | `google_calendar_oauth_client` | `google_calendar` | `calendar.events`, `calendar.calendarlist.readonly`, `calendar.freebusy`, `calendar.settings.readonly` |
| Google Sheets | `google_sheets` | `google_sheets_oauth` | `google_sheets_oauth_client` | `google_sheets` | `spreadsheets` |
| Google Tasks | `google_tasks` | `google_tasks_oauth` | `google_tasks_oauth_client` | `google_tasks` | `tasks` |
| Gmail | `google_gmail` | `google_gmail_oauth` | `google_gmail_oauth_client` | `gmail` | `gmail.modify` |

Scope names are relative to `https://www.googleapis.com/auth/`.
Every provider also requests the OpenID `openid`, email, and profile scopes.
Each provider's callback path is `/api/oauth/<provider-id>/callback`.

## Scope Rationale

MindRoom requests only the scopes needed for the operations it exposes to agents.

- `drive` lets an agent search, read, upload, create folders, move or rename files, and move files to trash across the connected account.
  It also authorizes permanent deletion at the Google API layer, but MindRoom exposes only trashing.
  The narrower `drive.file` scope would break account-wide search, because it covers only files created by or explicitly shared with the app.
- `documents` lets an agent create documents, read their full structure and content, and edit text without access to other Drive files.
  It also authorizes deleting documents, but the `google_docs` tool has no delete operation.
  `drive.file` is not enough here, because MindRoom opens documents by ID or URL rather than through Google Picker.
- `calendar.events` lets an agent read events and, when `allow_update` is enabled, create, update, delete, move, and respond to events, without access to calendar sharing or calendar deletion.
- `calendar.calendarlist.readonly` lets an agent list subscribed calendars without changing subscriptions.
- `calendar.freebusy` lets an agent check availability without seeing event details through that operation.
- `calendar.settings.readonly` lets an agent infer working hours from the Calendar locale without changing Calendar settings.
- `spreadsheets` lets an agent read and, when enabled, create or update spreadsheets the user names.
- `tasks` lets an agent list task lists and tasks and, when `manage_tasks` is enabled, create, update, complete, and delete tasks.
  It also authorizes creating and deleting task lists, but the `google_tasks` tool does not manage task lists.
- `gmail.modify` is the narrowest single Gmail scope that keeps mailbox search and reading, drafts and sending, replies, labels, archiving, and other organization.
  It does not allow permanent deletion that bypasses the trash, and MindRoom does not request the full `mail.google.com` scope.
- The OpenID email and profile scopes identify the connected account and enforce [account restrictions](#account-restrictions).

Google API data is used only for the agent operation the user requested.
Relevant results may be sent to the installation's configured AI model provider solely to generate that response, as disclosed before connection and in the [Privacy Policy](../privacy.md).
MindRoom does not use Google user data to train or improve generalized, foundational, or frontier AI models.

## Custom Google Cloud Setup

1. In Google Cloud Console, create an OAuth client of type **Web application**.
2. Enable only the Google APIs for the tools you plan to use, such as the Google Docs API for `google_docs` and the Google Tasks API for `google_tasks`.
3. Add one authorized redirect URI per provider you enable.
   With the default local origin these are:

   ```text
   http://localhost:8765/api/oauth/google_drive/callback
   http://localhost:8765/api/oauth/google_docs/callback
   http://localhost:8765/api/oauth/google_calendar/callback
   http://localhost:8765/api/oauth/google_sheets/callback
   http://localhost:8765/api/oauth/google_tasks/callback
   http://localhost:8765/api/oauth/google_gmail/callback
   ```

   For a public deployment, replace the origin with your `MINDROOM_PUBLIC_URL` (see [callback URL rules](../oauth-framework.md#connect-an-account)).
4. Save the client ID and secret in the dashboard with **Use custom client** on the Google integration, in a client config service, or through a credential seed, as described below.

A stored custom client always takes precedence over the provisioned client.

### Client Config Services

Store the client in a provider-specific service, such as `google_drive_oauth_client`, to apply it to that provider only.
Store it in `google_oauth_client` to apply one client to every Google provider.
A provider-specific service wins over `google_oauth_client`.

```json
{
  "client_id": "your-client-id.apps.googleusercontent.com",
  "client_secret": "your-client-secret",
  "redirect_uri": "https://mindroom.example.com/api/oauth/google_drive/callback"
}
```

`redirect_uri` is optional and only needed when the callback differs from the derived one.
Only provider-specific services honor it; `google_oauth_client` ignores it and each provider uses its own derived callback.
Secret redaction and the rules for editing a saved client are in [Provider And Credential Service Rules](../oauth-framework.md#provider-and-credential-service-rules).

For non-interactive deployments, seed the shared client at startup with a [credential seed](../oauth-framework.md#credential-seeds) file named by `MINDROOM_CREDENTIAL_SEEDS_FILE`:

```json
[
  {
    "service": "google_oauth_client",
    "credentials": {
      "client_id": {"env": "GOOGLE_CLIENT_ID"},
      "client_secret": {"env": "GOOGLE_CLIENT_SECRET"}
    }
  }
]
```

## Service Account

To use a Google Workspace service account instead of per-user OAuth, set `GOOGLE_SERVICE_ACCOUNT_FILE` to the service-account key file path in the environment or the config-adjacent `.env`.
Set `GOOGLE_DELEGATED_USER` to the account to impersonate through domain-wide delegation, and authorize the service account's client ID for the scopes in the [Providers](#providers) table in the Google Workspace Admin console.
The service account then serves every Google tool on this page instead of stored user connections, and the dashboard shows these services as connected.

## Production Verification Follow-up

Google classifies the `documents` and `tasks` scopes as sensitive and the `drive` scope as restricted.
Until Google approves a sensitive scope on your production project, users can see an unverified-app warning and test-user limits still apply, so do not enable that provider for general users before approval.
To add the Docs or Tasks provider to a project whose consent screen is already in verification:

1. Do not add the new scope to the production consent configuration while another verification submission is under review, unless you intend to change that submission.
2. Validate the integration in a separate Google Cloud testing project with test users: enable the API, add the scope, and register the callback.
3. After the production review completes, add the API and scope to the production project's Data Access configuration.
4. Submit the sensitive-scope verification follow-up with a scope justification and an end-to-end demo.

## Account Restrictions

Restrict which Google accounts may connect with comma-separated domain lists, set per provider in the environment or the config-adjacent `.env`:

| Variable | Accepts an account when |
| --- | --- |
| `<PROVIDER>_ALLOWED_EMAIL_DOMAINS` | Its verified email address is in one of the listed domains |
| `<PROVIDER>_ALLOWED_HOSTED_DOMAINS` | It belongs to one of the listed Google Workspace domains |

`<PROVIDER>` is the uppercased provider ID: `GOOGLE_DRIVE`, `GOOGLE_DOCS`, `GOOGLE_CALENDAR`, `GOOGLE_SHEETS`, `GOOGLE_TASKS`, or `GOOGLE_GMAIL`.
Each variable also accepts a `MINDROOM_OAUTH_` prefix, such as `MINDROOM_OAUTH_GOOGLE_GMAIL_ALLOWED_HOSTED_DOMAINS`.

```bash
GOOGLE_DRIVE_ALLOWED_EMAIL_DOMAINS=example.com
GOOGLE_GMAIL_ALLOWED_HOSTED_DOMAINS=example.com,example.org
```

A rejected account saves nothing, and the callback returns HTTP 400 with `OAuth account email domain is not allowed`, `OAuth account email ownership is not verified`, or `OAuth hosted domain claim is not allowed`.
