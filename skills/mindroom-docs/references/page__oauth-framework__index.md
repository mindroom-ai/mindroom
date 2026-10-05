# OAuth Integration Framework

This page covers how MindRoom stores credentials and how users connect OAuth accounts for tools and MCP servers.
Use it to import or seed credentials, encrypt the credential store, connect or reset an OAuth account, decide who may manage a connection, and build OAuth providers in plugins.

## Credential Storage

### Automatic Credential Import

When MindRoom starts or `mindroom doctor` runs, it copies these variables from the process environment or the config-adjacent `.env` into the shared credentials store: `ANTHROPIC_API_KEY`, `AZURE_OPENAI_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `CEREBRAS_API_KEY`, `GROQ_API_KEY`, `ZAI_API_KEY`, `OLLAMA_HOST`, `GITHUB_TOKEN` (stored as `github_private`), `EMBEDDER_API_KEY` (stored as `embedder`), and `GOOGLE_APPLICATION_CREDENTIALS` (stored as `google_vertex_adc`).
Every one of these except `GOOGLE_APPLICATION_CREDENTIALS` also accepts a `_FILE` variant, read only when the plain variable is unset or empty.
Services declared through [Credential Seeds](#credential-seeds) are imported the same way.
Import never overwrites credentials saved through the dashboard or credentials with no recorded source.
When an imported value is first stored or changes, MindRoom logs a notice naming the service, the supplying variable, and whether it came from the process environment or `.env`; values are never logged.
To stop an import, remove the variable and any `_FILE` variant from both the process environment and `.env`, then delete the stored credential in the dashboard or with `DELETE /api/credentials/{service}`.

### Credential Seeds

Credential seeds create shared credential services at startup for deployment-managed credentials, such as an OAuth app's client ID and secret.
Set `MINDROOM_CREDENTIAL_SEEDS_FILE` to a JSON file path (relative paths resolve from the config directory), or `MINDROOM_CREDENTIAL_SEEDS_JSON` to the same JSON inline.

```json
[
  {
    "service": "example_oauth_client",
    "credentials": {
      "client_id": {"env": "EXAMPLE_CLIENT_ID"},
      "client_secret": {"env": "EXAMPLE_CLIENT_SECRET"}
    }
  }
]
```

- Each credential field is `{"env": "NAME"}`, `{"file": "path"}` (relative paths resolve from the config directory), `{"value": ...}`, or a plain literal.
- If an env ref such as `EXAMPLE_CLIENT_SECRET` is unset, MindRoom also checks `EXAMPLE_CLIENT_SECRET_FILE` and reads that file.
- If any declared field is missing or empty, MindRoom skips that seed instead of creating a partial credential.
- Env and file refs always produce strings, so a tool's boolean field accepts the exact strings `true` and `false` as well as JSON booleans, and any other value makes that tool fail to load.
- Seeds are reapplied on every startup but never replace credentials saved through the dashboard or with no recorded source.
- OAuth token services, whose names end with `_oauth`, cannot be seeded; connect them through OAuth instead.
  OAuth client configuration services, whose names end with `_oauth_client`, can be seeded.
- To stop seeding one service, remove its entry from each declaration that lists it and delete the stored credential with `DELETE /api/credentials/{service}`.

### Credential Storage Encryption

Set `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` to encrypt stored credential files at rest.
The key must be a base64-encoded 32-byte value and must stay the same across restarts.
Generate one with:

```bash
python - <<'PY'
import base64
import os

print(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
PY
```

Once the key is set, existing plaintext credential files become unreadable and cannot be overwritten, so back them up and recreate them under encryption, or remove the key to read them again.
OAuth connections made before the key was set must be reconnected.
Encrypted credentials become readable again when their correct key is restored.
Dedicated Docker and Kubernetes workers never receive the key; tool settings reach them through [credential leases](https://docs.mindroom.chat/deployment/sandbox-proxy/#credential-leases).

## OAuth Framework

MindRoom runs the whole OAuth flow for OAuth-backed tools and MCP servers: connect links, the callback, token storage and refresh, status, disconnect, and reset.
Each connection is stored for one credential scope, so the right person's account is used for each agent.
Providers, built in or from plugins, supply only provider-specific details such as endpoints, scopes, and client configuration.

<video controls playsinline preload="metadata" aria-label="A user connects an account from the conversation, then the agent answers from it" style="width: 100%" poster="https://github.com/user-attachments/assets/03c90592-472a-42ea-8e57-d802e9129b6f" data-poster-light="https://github.com/user-attachments/assets/03c90592-472a-42ea-8e57-d802e9129b6f" data-poster-dark="https://github.com/user-attachments/assets/9849b052-508e-409a-867c-cf155a640d31">
  <source src="https://github.com/user-attachments/assets/c3a4166f-79a4-4ccb-a176-3a7c9922b828" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/62767b38-9729-4277-b615-e2b7b6863234" type="video/mp4">
</video>

### Connect An Account

Users connect an account from the dashboard **Tools** tab, the [Connections portal](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#connections-portal), or a link an agent posts in the conversation (see [MindRoom-Managed OAuth Onboarding In Conversation](#mindroom-managed-oauth-onboarding-in-conversation)).
The API routes are:

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/api/oauth/{provider}/connect` | Return an authorization URL for the dashboard |
| `GET` | `/api/oauth/{provider}/authorize` | Browser-openable link that redirects to the provider; used by conversation links |
| `GET` | `/api/oauth/{provider}/callback` | Provider callback |
| `GET` | `/api/oauth/{provider}/success` | Page shown after a successful connection |
| `GET` | `/api/oauth/{provider}/status` | Connection status |
| `POST` | `/api/oauth/{provider}/disconnect` | Remove the stored token; the tool's editable settings stay |
| `GET`, `POST` | `/api/oauth/{provider}/reset` | Browser-confirmed reset (see [`oauth_connections`](#oauth_connections)) |

Add `agent_name` to target a connection for one agent's credential scope.
The dashboard connects to the agent's saved scope, so save a `worker_scope` change before connecting.

The provider callback URL is `<MINDROOM_PUBLIC_URL or MINDROOM_BASE_URL>/api/oauth/<provider>/callback`, or `http://localhost:<MINDROOM_PORT>/api/oauth/<provider>/callback` (port `8765` by default) when neither is set.
Register that URL on the provider's OAuth app, or store an explicit `redirect_uri` in the provider-specific client config service.

Conversation connect links for a scoped connection are valid for 10 minutes, work once, and are tied to the requester, provider, and agent scope that produced them.
For a personal (`user` or `user_agent`) connection, the browser must be signed in to MindRoom as that same requester; for a shared connection, the link itself is enough and no dashboard login is needed.
MindRoom rechecks the requester's authority when the link is used and rejects the link if the connection changed after it was issued.
An expired, reused, unauthorized, or outdated link saves no credentials; rerun the original request for a fresh link.
For an unscoped connection, or a tool call without a concrete requester, the link is reusable and requires dashboard login instead.
Standalone installs identify the dashboard user through `MINDROOM_OWNER_USER_ID` (see [Platform and credential authority](https://docs.mindroom.chat/authorization/#platform-and-credential-authority)), and hosted multi-user deployments through [trusted upstream auth](https://docs.mindroom.chat/deployment/trusted-upstream-auth/).

### Who Can Connect An Account

| Connection | Who may connect, check, or disconnect it |
| --- | --- |
| Personal (`user` or `user_agent` scope, or a provider that always stores per requester) on a non-private agent | The requester, when they have room-independent access to the agent or are a platform `administrator` or credential manager for it |
| Isolated connection on a requester-private agent | That requester |
| Shared or unscoped connection | A platform `administrator` or a user listed in `agents.<name>.credential_managers` |

Room-independent access comes from `access.users`, administrator authority, or a `members_of_rooms` grant; `access.current_room_members` and team access do not apply, because a browser request has no current room or team.
Managing a personal connection grants no general dashboard access and no authority over shared credentials.
With the Connections portal enabled, eligible users manage their connections there, and ordinary dashboard routes require administrator authority.
Unauthorized connect, authorize, status, disconnect, and callback requests return HTTP 403 before any credential is exposed or changed.
See [Authorization](https://docs.mindroom.chat/authorization/#platform-and-credential-authority) for administrators and credential managers.

### Where Connections Are Stored

Providers that store credentials per requester, such as GitHub and Atlassian, always save the token in the requester's `user` scope and never fall back to a shared or global token.
For other providers, the token follows the agent's effective execution scope: `private.per`, then `agents.<name>.worker_scope`, then `defaults.worker_scope`.

| Scope | Whose connection is used |
| --- | --- |
| `shared` | One connection for the agent, used by every authorized caller |
| `user` | The requester's connection, reused across their `user`-scoped agents |
| `user_agent` | One connection per requester and agent |
| _(unset)_ | One installation-level connection in the global credential store |

Private agents derive the scope from `private.per` and cannot also set `worker_scope`.
OAuth tokens and OAuth client configuration stay in the primary runtime and are never mirrored into worker containers, and GitHub and Google OAuth tools always run in the primary runtime.

### Built-In Providers

| Provider | Setup |
| --- | --- |
| GitHub (GitHub App user tokens) | [GitHub OAuth Setup](https://docs.mindroom.chat/tools/project-management/#oauth-setup) |
| Atlassian Cloud (Jira and Confluence) | [Atlassian Cloud](https://docs.mindroom.chat/tools/atlassian/) |
| Google Drive, Docs, Calendar, Sheets, Tasks, and Gmail | [Google Services OAuth](https://docs.mindroom.chat/deployment/google-services-oauth/) |
| Remote MCP servers with `auth.type: oauth` | [OAuth-Backed Remote MCP](https://docs.mindroom.chat/mcp/#oauth-backed-remote-mcp) |

A dynamically registered MCP client works from a hosted address only when `MINDROOM_PUBLIC_URL` or `MINDROOM_BASE_URL` is an HTTPS URL without query or fragment on a public DNS hostname, that hostname matches the address the browser used to open MindRoom, and the authorization server confirmed that callback when registering.
Otherwise the client works only when MindRoom is opened on `localhost`, and other addresses get `The provisioned OAuth client is available only when MindRoom is opened on localhost. Set MINDROOM_PUBLIC_URL (or MINDROOM_BASE_URL) and configure a custom OAuth client for remote access.`

### Provider And Credential Service Rules

These rules apply to built-in providers and to providers that plugins register (see [OAuth providers](https://docs.mindroom.chat/plugins/#oauth-providers) for the plugin module and examples).

- A provider's token `credential_service` must end with `_oauth`.
  Only the OAuth callback writes it, and the generic credentials API neither returns OAuth token fields nor accepts direct writes to these services.
- An optional `tool_config_service` holds the tool's editable dashboard settings separately from the token.
- `tool_config_oauth_fallback_fields` (tuple of strings, default `()`) names token-like fields, such as `access_token`, that the dashboard may save to and show from `tool_config_service` as manual credentials; otherwise the dashboard strips token fields from that service.
- `tool_config_oauth_fallback_env_vars` (tuple of strings, default `()`) names environment variables that, when all are set, make the dashboard show the provider's tools as configured without an OAuth connection.
- `client_config_services` lists, in lookup order, services holding the OAuth app's `client_id`, `client_secret`, and optional `redirect_uri`; every name must end with `_oauth_client`.
- `shared_client_config_services` are checked after those and supply one app ID and secret for several providers, such as `google_oauth_client`; they never supply `redirect_uri`, so each provider keeps its own callback URL.
- Public clients set `token_endpoint_auth_method: none` and need only `client_id`; confidential clients need both `client_id` and `client_secret`.
- Credential responses redact `client_secret` for client config services.
  Saving a confidential client may omit `client_secret` only when `client_id` is unchanged, so changing its `client_id` requires the matching new secret.
- Client config services cannot be copied through the generic copy route.
- Automatic discovery with `oauth_runtime_bootstrapper()` can register a client dynamically only when the provider declares a provider-specific `client_config_services` entry to store it in.
- `requester_scoped_credentials=True` always stores the provider's tokens in the requester's `user` scope.
- `pkce_code_challenge_method="S256"` enables PKCE; a custom `token_exchanger` receives `(provider, code, client_config, runtime_paths, code_verifier)` so it can send the verifier.
- `allowed_email_domains`, `allowed_hosted_domains`, and a custom `claim_validator` restrict which accounts may connect.
  If a restriction cannot be checked from the provider's verified claims, the connection fails and nothing is saved.

### Troubleshooting OAuth Connections

- **Credentials cannot be read**: when a stored token exists but cannot be read, `/status` returns `reset_required: true`, and tools tell the requester that the credentials "cannot be read" and to reset the connection from the dashboard (the **Tools** tab), then reconnect.
- **Provider token endpoint changed**: MindRoom sends refresh tokens only to the token endpoint recorded when the account was connected.
  If the provider now resolves a different endpoint, MindRoom deletes the stored credential and the user must reconnect; the log shows `oauth_credentials_refresh_failed` with `reason="token_endpoint_changed"` and both endpoint origins.
- **Connections stored as `<credential_service>_credentials.json`**: these older token files are not read; reconnect the account.

## MindRoom-Managed OAuth Onboarding In Conversation

This flow applies to tools whose catalog metadata names an `auth_provider`, including the Google tools and OAuth MCP servers.
Tools labeled `SetupType.OAUTH` without an `auth_provider` use their own setup instructions.
When `config_manager` creates or updates an agent with one of these tools, it returns a connect URL scoped to that agent and the current requester when available.
Present that URL directly instead of asking the configuring agent to call the new tool, because newly configured tools may not be available in the current run.
When the current agent already has a MindRoom-managed provider tool, call a safe status, read, or list operation to check its connection; for an OAuth MCP server, use its `*_connection_status` or `*_list_tools` operation.
A disconnected tool returns an `OAuthConnectionRequired` result with `oauth_connection_required: true`, a scoped `connect_url` when available, and `requires_host_browser: true` when the URL uses a loopback host.
Present `connect_url` directly instead of sending the user to the dashboard; use the dashboard only when no `connect_url` is available.
When `requires_host_browser` is true, explain that the loopback URL (`localhost`, `127.0.0.1`, or `::1`) must be opened in a browser on the computer where MindRoom is running, not on a phone or another computer.
After the user connects, have the target agent retry the safe operation or the original request.

## [`oauth_connections`]

`oauth_connections` lets an agent recover a stuck or revoked MindRoom-managed OAuth connection without broader credential-management controls.
It exposes only `reset_oauth_connection(provider_id)`, where the provider must back one of the agent's configured tools.
The call returns a browser link and changes nothing by itself.
After you confirm on that page, MindRoom removes its saved connection and opens the provider's sign-in page so you can reconnect.
The reset does not revoke access at the provider itself.

```yaml
agents:
  researcher:
    display_name: Researcher
    role: Work with connected documents and recover revoked connections
    model: sonnet
    worker_scope: user_agent
    tools:
      - oauth_connections
      - google_drive
```

### Who Can Reset A Connection

| Connection type | Who confirms the reset | What the reset affects |
| --- | --- | --- |
| Shared (`shared`) | An administrator or configured credential manager can request the link. Anyone with the complete link can confirm it once before it expires. | Everyone using this agent |
| Personal (`user`) | The same MindRoom user who requested the link, signed in to the dashboard | That user's connection across agents |
| Personal for one agent (`user_agent`) | The same MindRoom user who requested the link, signed in to the dashboard | That user's connection for this agent only |

Requesting a personal reset link needs the same authority as connecting that account (see [Who Can Connect An Account](#who-can-connect-an-account)), and on a private agent every connection is personal.
Some providers always use personal connections, regardless of the agent's scope.
Keep a shared reset link private, because confirming it disconnects the service for everyone using that agent until someone reconnects.

### Reset A Connection

1. Ask the agent to call `reset_oauth_connection()` for the affected provider.
2. Open the returned link within 10 minutes.
3. Review which agent and connection type will be affected, then confirm the reset.
4. Sign in at the provider and retry the original request.

Opening the link without confirming changes nothing.
MindRoom refuses an expired, unauthorized, or outdated link without deleting credentials; ask the agent for a new link.
Installation-level connections that are not assigned to an agent scope must be reset from the dashboard.
`oauth_connections` always runs in the primary MindRoom runtime, even if it appears in `worker_tools`, and normal `tool_approval` rules apply when the agent creates the link.
