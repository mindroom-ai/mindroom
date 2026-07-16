# Google Services OAuth For Local Installs

Paired local installations automatically retrieve MindRoom's Google desktop OAuth client from the provisioning service.
You do not need to create a Google Cloud project, register callback URLs, or copy a client secret.
The client configuration is not bundled in the package or committed to the source repository.
The local runtime uses OAuth PKCE and a callback on `localhost`, `127.0.0.1`, or `::1`.
Run `mindroom connect --pair-code ...` before connecting Google, or configure a custom Google OAuth client for an unpaired self-hosted installation.

## Choose Providers

Add only the Google tools your agents need.

```yaml
agents:
  personal:
    display_name: Personal
    role: Help with my Google workspace
    worker_scope: user_agent
    tools:
      - google_drive
      - google_calendar
      - google_sheets
      - gmail
```

## Connect

After enabling a Google tool for an agent, ask the agent to perform a harmless read or list operation with that tool.
If the tool is disconnected, its result contains structured `OAuthConnectionRequired` data with `oauth_connection_required: true` and the exact `connect_url` for that provider, requester, agent, and execution scope.
The agent should present that `connect_url` directly instead of sending you to the dashboard.
If the result includes `requires_host_browser: true`, open the localhost link in a browser on the computer where the MindRoom process is running, not on a phone or another computer.
If you made the request from another device, open the conversation on the MindRoom computer or copy the complete URL into a browser there.
Google asks you to choose an account and approve only that provider's scopes.
After the browser flow completes, retry the operation and the integration is ready for the selected agent and execution scope.
As a manual alternative, open the dashboard and select **Connect** for the Google integration when a tool result does not provide a connect URL.
The dashboard explains which installation and credential scope will receive the connection before you continue.

OAuth tokens are stored under provider token services such as `google_drive_oauth`.
Editable tool settings are stored separately under services such as `google_drive`, `google_calendar`, `google_sheets`, and `gmail`.
MindRoom does not mirror Google OAuth tokens into worker containers.

## Privacy and Access Scope

For a local installation, the MindRoom project maintainers do not automatically receive your OAuth tokens or Google data.
The paired provisioning service sends the Google desktop app client configuration to the local runtime but does not receive the Google authorization code, access token, refresh token, or Google API data.
Google returns the authorization response directly to the local loopback callback, and the local runtime performs the token exchange and stores the resulting connection.
The software running on your machine stores the connection, while the Google API, your configured AI model provider, and your Matrix homeserver process the data sent to each of them.
The installation operator and anyone with administrative or filesystem access to its storage may be able to access locally stored credentials and data.

The example above uses `worker_scope: user_agent`, which keeps each authenticated Matrix requester's connection separate for that agent.
With `worker_scope: user`, one requester can reuse the connection across that requester's user-scoped agents.
With `worker_scope: shared`, any user authorized to invoke the selected shared agent can cause it to use the connected Google Account and may receive Google data in its response.
With no worker scope configured, the connection is stored at the installation level and is not isolated by requester.

Being signed in to the computer does not itself determine access.
MindRoom uses authenticated Matrix requester identity, agent authorization, and the configured credential scope, while operating-system and storage permissions remain the installation operator's responsibility.
See the [Privacy Policy](https://docs.mindroom.chat/privacy/) for the complete data-handling disclosure.

## Custom Google OAuth App

A custom Google OAuth app is optional for a paired local installation and required for an unpaired self-hosted installation.
Use one when you operate a public MindRoom origin, need organization-specific Google policies, or want your own consent-screen branding.

Select **Use custom client** in the dashboard and enter the client ID and client secret from a Google Cloud **Web application** OAuth client.
Enable the APIs for the tools you use and add the matching callback URLs.

```text
http://localhost:8765/api/oauth/google_drive/callback
http://localhost:8765/api/oauth/google_calendar/callback
http://localhost:8765/api/oauth/google_sheets/callback
http://localhost:8765/api/oauth/google_gmail/callback
```

Replace the origin when `MINDROOM_PUBLIC_URL` or `MINDROOM_BASE_URL` points to a public deployment.
For a shared custom client, store the configuration under `google_oauth_client`.
Provider-specific services such as `google_drive_oauth_client` override the shared client.

When using standalone dashboard API-key auth, also set `MINDROOM_OWNER_USER_ID` to your Matrix user ID, such as `@alice:matrix.example.com`.
Do not use `MINDROOM_OWNER_USER_ID` as the identity model for hosted multi-user private agents.
Use [Trusted Upstream Browser Auth](https://docs.mindroom.chat/deployment/trusted-upstream-auth/) for those deployments.
