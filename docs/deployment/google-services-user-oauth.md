---
icon: simple/google
---

# Google Services OAuth For Local Installs

This page explains how to connect Google Drive, Docs, Calendar, Sheets, Tasks, Gmail, and Google Cloud on a paired local installation.
Paired installations use MindRoom's Google OAuth client, so you do not need a Google Cloud project, callback URLs, or a client secret.
Pair first with `mindroom connect`, or let `mindroom run` pair automatically on first run (see [Hosted Matrix](hosted-matrix.md)).
The provisioned client works only when MindRoom is opened on a loopback address (`localhost`, `127.0.0.1`, or `::1`).
For an unpaired install, a remote or public address, organization-specific Google policies, or your own consent-screen branding, set up a custom client instead (see [Custom Google Cloud Setup](google-services-oauth.md#custom-google-cloud-setup)).
That page also lists the scopes each Google tool requests and how to restrict which Google accounts may connect.

## Choose Providers

Add only the Google tools your agents need.
Each Google Workspace tool connects and asks for Google approval separately.
Google Cloud tools such as `google_bigquery` share one read-only **Google Cloud** connection, so you approve it once for all of them.

```yaml
agents:
  personal:
    display_name: Personal
    role: Help with my Google workspace
    worker_scope: user_agent
    tools:
      - google_drive
      - google_docs
      - google_calendar
      - google_sheets
      - google_tasks
      - gmail
```

`worker_scope` decides whose Google account the agent uses.
With `user_agent`, each Matrix user connects their own account for this agent.
With `shared`, everyone allowed to use the agent acts through one connected account and may receive its Google data in replies.
See [Where Connections Are Stored](../oauth-framework.md#where-connections-are-stored) for every scope.
The Google Cloud connection requests the sensitive `cloud-platform.read-only` scope, and what the agent can read is limited by the connected account's own IAM roles.

## Connect

1. Ask the agent to do something safe with the Google tool, such as listing files or upcoming events.
   If the account is not connected, the agent replies with a connect link.
   When `config_manager` has just added the Google tool to an agent, use the connect link it returns instead.
2. Open the link in a browser on the computer where MindRoom runs, not on a phone or another computer, because the link points to `localhost`.
   If you are chatting from another device, open the conversation on the MindRoom computer or copy the complete link into a browser there.
3. Choose a Google account and approve the scopes for that one service.
4. Ask the agent to retry the request.

Without a connect link, open the dashboard **Tools** tab, choose the agent in the selector, and select **Connect** on the Google integration.
Only agents with a worker scope appear in the selector; for an agent without one, keep **Shared deployment credentials** selected.
The dashboard shows how Google data is handled before you continue.
For personal connections from the dashboard, the dashboard must know your Matrix identity; see [Connect An Account](../oauth-framework.md#connect-an-account).
Link lifetime, who may connect, disconnecting, and resetting a connection are covered in the [OAuth Integration Framework](../oauth-framework.md).

## Privacy

On a local installation, the provisioning service only supplies the OAuth client configuration.
Google sends the authorization response straight to your MindRoom process, which exchanges and stores the tokens, so the provisioning service does not receive your authorization code, tokens, or Google API data.
Google data an agent reads goes to your configured AI model provider and Matrix homeserver as part of the conversation.
Anyone with administrative or filesystem access to the installation's storage may be able to read the stored credentials and data.
See the [Privacy Policy](../privacy.md#google-api-services) for the complete data-handling disclosure.

## Troubleshooting

- **`OAuth client configuration could not be resolved`**: MindRoom could not get a Google OAuth client, usually because the install is not paired or its pairing was revoked.
  Run `mindroom connect`, or configure a [custom client](google-services-oauth.md#custom-google-cloud-setup).
- **Google shows an unverified-app warning or blocks the Google Cloud connection**: `cloud-platform.read-only` is a sensitive scope, so an OAuth client that Google has not verified for it can be limited to test users.
  Add your account as a test user, or use a [custom client](google-services-oauth.md#custom-google-cloud-setup) whose consent screen is Internal to your organization; see [Production Verification Follow-up](google-services-oauth.md#production-verification-follow-up).
- **`The provisioned OAuth client is available only when MindRoom is opened on localhost...`**: open MindRoom on `localhost`, or configure a custom client for remote access.
