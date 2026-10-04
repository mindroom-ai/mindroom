# OAuth Integration Framework

## Credential Storage

### Automatic Credential Import

**Automatic credential import**: When MindRoom starts or `mindroom doctor` runs, it copies these variables from the process environment or the config-adjacent `.env` into the shared credentials store: `ANTHROPIC_API_KEY`, `AZURE_OPENAI_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `CEREBRAS_API_KEY`, `GROQ_API_KEY`, `ZAI_API_KEY`, `OLLAMA_HOST`, `GITHUB_TOKEN` (stored as `github_private`), `EMBEDDER_API_KEY` (stored as `embedder`), and `GOOGLE_APPLICATION_CREDENTIALS` (stored as `google_vertex_adc`).
Every one of these except `GOOGLE_APPLICATION_CREDENTIALS` also accepts a `_FILE` variant, read only when the plain variable is unset or empty.
Services declared through [Credential Seeds](#credential-seeds) are imported the same way.
Environment import never overwrites credentials saved through the dashboard or credentials with no recorded source.
When a stored value is first imported or changes, MindRoom logs a notice naming the service, the supplying variable, and whether it came from the process environment or `.env`; values are never logged.
To stop an import, remove the variable and any `_FILE` variant from both the process environment and `.env`, then delete the stored credential in the dashboard or with `DELETE /api/credentials/{service}`.

### Credential Seeds

MindRoom can create shared credential services at startup from explicit seed declarations, for deployment-managed credentials.
Set `MINDROOM_CREDENTIAL_SEEDS_FILE` to a JSON file path (relative paths resolve from the config directory), or `MINDROOM_CREDENTIAL_SEEDS_JSON` to equivalent inline JSON.
Credential fields can read from env vars, from files, or from literal values:

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

- If an env ref such as `EXAMPLE_CLIENT_SECRET` is unset, MindRoom also checks `EXAMPLE_CLIENT_SECRET_FILE` and reads that file.
- If any declared field is missing or empty, MindRoom skips that seed instead of creating a partial credential.
- Env and file refs always produce strings, so a tool's boolean field accepts the exact strings `true` and `false` as well as JSON booleans, and any other value makes that tool fail to load.
- Seeds are reapplied on later startups but never replace credentials saved through the dashboard or with no recorded source.
- OAuth token services whose names end with `_oauth` cannot be seeded; connect them through OAuth instead.
  OAuth client configuration services whose names end with `_oauth_client` can be seeded.
- To stop seeding one service, remove its entry from each declaration that lists it and delete the stored credential with `DELETE /api/credentials/{service}`.

### Credential Storage Encryption

Set `MINDROOM_CREDENTIALS_ENCRYPTION_KEY` to encrypt stored credential files at rest.
The key must be a base64-encoded 32-byte value and must stay stable across restarts.
Generate one with:

```bash
python - <<'PY'
import base64
import os

print(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
PY
```

Existing plaintext credential files become unreadable and cannot be overwritten once the key is set, so back them up and recreate them under encryption, or remove the key to read them again.
OAuth connections made before the key was set must likewise be reconnected.
Encrypted credentials become readable again when their correct key is restored.
Dedicated Docker and Kubernetes workers never receive the key; see [credential leases](https://docs.mindroom.chat/deployment/sandbox-proxy/#credential-leases) for how tool settings reach them.

## OAuth Framework

MindRoom owns OAuth state, callback handling, credential scoping, and token persistence because those steps decide which human and agent scope receive access to an external account.
Providers supply only provider-specific metadata and parsing behavior, such as OAuth endpoints, scopes, client config services, optional PKCE requirements, requester-only credential placement, explicitly permitted manual fallback fields and runtime environment names, token response parsing, claim validation, the token credential service name used by OAuth, and the optional tool config service name used by dashboard settings.

<video controls playsinline preload="metadata" aria-label="A user connects an account from the conversation, then the agent answers from it" style="width: 100%">
  <source src="https://github.com/user-attachments/assets/9c301417-f0ee-43d1-ac42-8c974d42888f#t=0.1" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/4689c253-d026-4dee-afb8-7f178ea793df#t=0.1" type="video/mp4">
</video>

The generic API surface is `/api/oauth/{provider}/connect`, `/api/oauth/{provider}/authorize`, `/api/oauth/{provider}/callback`, `/api/oauth/{provider}/success`, `/api/oauth/{provider}/status`, `/api/oauth/{provider}/disconnect`, and the browser-confirmed `GET`/`POST` `/api/oauth/{provider}/reset` flow.
When a scoped token exists but cannot be decoded, status returns `reset_required: true`, and the dashboard offers the decode-free scoped disconnect path before reconnecting.
Agent-facing OAuth tools return the same structured `reset_required` signal and direct the requester to the authenticated dashboard Integrations page, which supports every credential scope and avoids prescribing an unavailable agent tool or unusable connect link.
Dashboard flows can call `connect` to receive an authorization URL, while conversation flows can show the browser-openable `authorize` URL before MindRoom redirects to the external provider.
Dashboard OAuth state is opaque, time-limited, single-use, and bound to the authenticated MindRoom user plus the persisted agent execution scope resolved by the existing credentials target machinery.
When an agent-targeted OAuth request resolves to shared or unscoped credentials, the authenticated requester must be a platform `administrator` or a concrete user in `agents.<name>.credential_managers`.
For Connections portal routes and dashboard credential targeting, a verified Matrix requester with current responder access can manage their own `user` or `user_agent` OAuth connection on a non-private agent definition, including when the provider requires requester-only storage.
An authenticated requester may also manage their own isolated OAuth connection for a requester-private agent without a static credential-manager entry.
These personal permissions do not grant general dashboard access or authority over shared credentials.
With the [Connections portal](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#connections-portal) enabled, eligible users use its dedicated `/api/connections/...` routes with signed Matrix authentication, while ordinary dashboard routes require administrator authority.
Ordinary generic `/api/oauth/...` requests remain subject to deployment authentication and the Connections route restrictions; conversation capabilities use the separately checked flow below.
For providers that follow agent scope, credential targets use saved configuration, and a different execution-scope override is rejected until that configuration is saved.
Unauthorized agent-scoped OAuth connect, authorize, status, disconnect, and callback requests return HTTP 403 before credentials are exposed or changed.
Conversation OAuth links use an additional opaque, time-limited, single-use connect token that binds the browser flow to the requester that produced the missing-credentials tool result.
The token binds the exact provider, Matrix requester, worker target, and credential connection generation.
Requester-scoped credentials require the browser to authenticate as that requester at authorization and callback, using the same identity check as requester-scoped resets.
Shared-scope credentials permit delegation through the short-lived token without a dashboard login.
MindRoom rechecks the conversation link requester's authority at authorization and callback, and rejects a link if its credential generation changed after issuance.
A requester-owned connection, one stored in the requester's user or user-agent credential scope as requester-scoped providers always are, needs only room-independent access to the agent, as on the Connections portal and the dashboard.
That access comes from `access.users`, administrator authority, or a `members_of_rooms` grant; `access.current_room_members` and team access do not apply because a browser request has no current room or team.
A shared-scope connection on a non-private agent requires an administrator or configured credential manager.
The requester-private-agent exception still applies to its isolated connection.
Agent-issued reset links apply the same rule.
Shared-scope reset links use the same capability model for configured credential managers: the GET is non-mutating, the confirmation POST consumes the reset capability before deleting the scoped credential, and reconnection continues through a fresh single-use connect capability.
Requester-scoped reset links still require the original authenticated browser user.
Executions without a concrete requester cannot form a conversation capability; their links omit the connect token and use the existing dashboard-authenticated flow.
Standalone deployments should set `MINDROOM_OWNER_USER_ID` through pairing so dashboard credential management and agent-issued OAuth links resolve to the owner Matrix user instead of the generic dashboard API-key principal.
`MINDROOM_OWNER_USER_ID` is a single-owner shortcut and is not suitable for a hosted multi-user private-agent deployment.
Hosted deployments that put MindRoom behind an external access layer should enable trusted upstream auth and configure the exact headers MindRoom may trust.
When trusted upstream auth is enabled, MindRoom reads the configured stable user ID and optional email headers into `request.scope["auth_user"]`.
For Matrix-backed private agents, the trusted identity must resolve to a Matrix user ID either from a configured Matrix user ID header or from `MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE`.
The email-to-Matrix template must contain exactly one `{localpart}` placeholder and requires `MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER`.
The access layer must strip any client-supplied copies of the trusted headers before injecting verified values.

Plugins may declare an `oauth_module` in `mindroom.plugin.json`.
That module exposes `register_oauth_providers(settings, runtime_paths)` and returns `OAuthProvider` objects.
This keeps FastAPI routing and state handling in core while still letting plugin authors define provider IDs, scopes, token exchange details, optional claim validators, and tool metadata.
Plugins can use `OAuthDiscoveryConfig` with `oauth_runtime_bootstrapper()` to resolve protected-resource and authorization-server metadata lazily and optionally register an OAuth client.
Automatic discovery first checks protected-resource metadata at the resource origin and path, then uses the advertised authorization server or falls back to authorization-server metadata at the resource origin.
Dynamic client registration requires a provider-specific `client_config_services` entry and stores generated client configuration only in the primary runtime.

OAuth token writes always resolve the provider's canonical credential target and publish through the OAuth credential lifecycle into that scope's private SQLite store.
Starting a connection resolves the provider's endpoints once, builds the authorization URL from them, and records the resolved token endpoint in the pending OAuth state.
The callback fails without contacting any token endpoint when the provider now resolves a different one; otherwise the built-in exchange sends the authorization code, PKCE verifier, and client secret to that recorded endpoint.
Stored credentials record the same endpoint as `token_uri`, including credentials produced by custom token exchangers and parsers.
Refreshes through the provider contract, used by the dashboard, MCP servers, GitHub, and Atlassian, send the refresh token only when the stored `token_uri` matches the token endpoint the provider currently resolves.
When it differs, or an older credential has no recorded endpoint, MindRoom deletes the credential without contacting the token endpoint and requires reconnection.
That rejection is logged as `oauth_credentials_refresh_failed` with `reason="token_endpoint_changed"` and the stored and current endpoint origins, so an authorization-server change is distinguishable from a revoked grant.
Google tool wrappers instead refresh through google-auth against the stored `token_uri` without re-resolving endpoints, and Google providers always store Google's fixed token endpoint there.
A reconnect keeps an earlier refresh token that the provider did not replace only when the verified identity, OAuth client, and `token_uri` all match.
Dynamic client registrations record the token endpoint they were issued for, and discovery registers a new client when the resolved token endpoint changes.
A code exchange or refresh also refuses a dynamically registered client whose recorded token endpoint differs from the endpoint it is about to use.
The SQLite store is authoritative on every OAuth credential read.
Legacy `<credential_service>_credentials.json` token documents and their sidecars are ignored and left unchanged.
An OAuth connection that exists only in JSON must be reconnected to publish current SQLite state.
Providers can declare that credentials follow the requester independently of agent worker reuse.
GitHub uses that policy, so its managed token always lands in the requester's `user` scope and can never fall back to a shared or global token store.
For providers without that policy, token placement follows the agent's saved effective execution scope: `private.per`, then `agents.<name>.worker_scope`, then `defaults.worker_scope`, otherwise unscoped.
Private agents use `private.per` and cannot also set `worker_scope`.
A `user` connection follows the authenticated requester across user-scoped agents, while `user_agent` binds it to that requester and agent.
A `shared` connection uses a per-agent primary-runtime store.
Only agents with no private, per-agent, or inherited scope use the global credential store.
Conversation capabilities reconstruct the bound requester and worker target from server-side state; invalid, expired, reused, unauthorized, or stale links fail closed and save no credentials.
Credential placement and visibility policy is centralized in `src/mindroom/credential_policy.py`.
That module owns service classification, OAuth token field filtering, local-only credential service names, and worker-grantable rejections.
Scoped tool settings are read from primary stores, never worker credential stores, with OAuth token and existing local-only service rules unchanged.
The primary builds every tool and leases a worker-routed tool's settings to the worker per call; settings use per-agent storage for `shared` and requester-scoped storage for `user` and `user_agent`, while explicit shared grants and shared mirrors still apply.
Storage, API routing, OAuth provider loading, and worker identity derivation stay in their existing modules.
Tools should declare `auth_provider` and, when credentials are missing, return a concise connect instruction that points at the generic `authorize` route for the provider and agent.
GitHub and Google OAuth tools always execute in the primary MindRoom runtime so worker runtimes never need OAuth client config or user refresh tokens.
OAuth token documents and editable tool setting documents should be separate services.
Every provider token `credential_service` must end with `_oauth` so placement and worker-grant policy recognize it without loading provider code.
The OAuth callback writes only the provider's `credential_service`, while dashboard configuration reads and writes the provider's `tool_config_service` when one is declared.
OAuth app client config is stored separately from both of those services.
Providers declare `client_config_services` in lookup order, and MindRoom reads `client_id`, `client_secret`, and optional `redirect_uri` from those services.
Providers can also declare shared client config services for shared app IDs and secrets.
Every client config service name must end with `_oauth_client` so credential placement and worker allowlist validation can identify plugin client config services without loading provider code.
Shared client config services do not supply redirect URIs because each provider must use its own callback route.
Client config services are local-only deployment configuration and cannot be mirrored into worker containers.
Generic credential responses redact `client_secret` for client config services.
Generic credential saves preserve the existing `client_secret` only when the saved `client_id` is unchanged.
Changing `client_id` requires submitting the matching new `client_secret`.
First-time confidential client config saves require both fields to be non-empty.
Public OAuth clients that use `token_endpoint_auth_method: none` require `client_id` and may omit `client_secret`.
Client config services cannot be copied through the generic copy route.
Generic credentials endpoints do not return OAuth token fields and reject direct writes to OAuth token services.

Providers that require PKCE should set `pkce_code_challenge_method="S256"`.
MindRoom generates one verifier per OAuth flow, stores it in pending server-side state, adds the S256 challenge to the authorization URL, and passes the verifier into token exchange.
Custom `token_exchanger` callbacks receive `(provider, code, client_config, runtime_paths, code_verifier)` so they can include the verifier in provider-specific exchange requests.

Identity restrictions are provider settings, not MindRoom policy.
Providers can enforce allowed email domains, allowed hosted-domain claims, and custom claim validators.
If a configured restriction cannot be checked from verified provider claims, the callback fails closed and no credential is saved.

The built-in GitHub provider uses the generic framework for GitHub App user tokens, requests no classic OAuth scopes, and requires S256 PKCE.
The built-in Atlassian provider uses the generic framework for Atlassian Cloud OAuth 2.0 (3LO), stores tokens only in the requester's `user` scope like GitHub, and requests only the scopes its [Jira and Confluence functions](https://docs.mindroom.chat/tools/atlassian/#scopes) call.
The built-in provider reads its app client from `atlassian_oauth_client`, and each additional site configured through `AtlassianConnectionConfig` gets a separate provider that defaults to its own `<name>_atlassian_oauth_client` service.
Built-in Google providers use the generic framework for Drive, Docs, Calendar, Sheets, Tasks, and Gmail.
Each provider has minimal service-specific scopes, stores OAuth tokens under its own `*_oauth` service, stores editable tool settings separately, and uses `/api/oauth/*`.
Each provider first checks its provider-specific client config service, then the shared `google_oauth_client` service.
The shared `google_oauth_client` service supplies only `client_id` and `client_secret`; MindRoom derives the provider-specific redirect URI.

OAuth-backed remote MCP servers also use the generic framework.
MindRoom synthesizes an OAuth provider from each `mcp_servers.*.auth.type: oauth` config entry and exposes it through the same `/api/oauth/{provider}/*` routes.
Generated MCP OAuth token services use the `<provider_id>_oauth` naming pattern; the default provider ID is already `mcp_<server_id>`.
Custom provider IDs that do not start with `mcp_` get an `mcp_` credential-service prefix.
These token services stay in the primary runtime credential store.
The matching MCP toolkit loads the token from the selected agent's effective credential scope before opening the remote MCP transport.
Generated MCP OAuth credentials and sessions follow the same `shared`, `user`, `user_agent`, or unscoped ownership policy as other OAuth providers that do not declare requester-only credentials.
Private agents derive the corresponding credential scope from `private.per`.
Generated MCP OAuth providers can use public clients with `token_endpoint_auth_method: none`, PKCE, and empty scope lists.
Generated MCP OAuth providers can also discover protected-resource metadata and authorization-server metadata lazily when the first OAuth flow starts.
If the authorization server advertises dynamic client registration and no client config is stored yet, MindRoom registers a public client and persists the returned registration metadata in the generated OAuth client config service.
Hosted OAuth entrypoints accept that dynamically registered client only when `MINDROOM_PUBLIC_URL` or `MINDROOM_BASE_URL` produces an exact, unambiguous HTTPS callback without a query or fragment on the same non-special-use fully qualified ASCII DNS hostname as the initiating request and the authorization server confirms that callback in its registration response.
Missing, replaced, insecure, local-only, IP-literal, cross-host, non-ASCII, or ambiguous callback metadata keeps the provisioned client restricted to localhost, and MindRoom rejects a new registration before persistence when the response does not confirm the requested callback.

## MindRoom-Managed OAuth Onboarding In Conversation

This flow applies to tools whose MindRoom catalog metadata names an `auth_provider`, including the Google provider tools and OAuth MCP servers.
It does not apply to every tool labeled `SetupType.OAUTH`; tools without `auth_provider` metadata use their own setup contract.
When `config_manager` creates or updates an agent with one of these tools, it returns a connect URL scoped to the updated agent and current authorized requester when that binding is available.
Present that URL directly instead of asking the configuring agent to call the new tool, because newly configured tools are not guaranteed to enter the current run's tool schema.
When the current agent already has a MindRoom-managed provider tool, call an appropriate safe status, read, or list operation to check its connection.
For an OAuth MCP server, use its generated `*_connection_status` or `*_list_tools` operation.
If the operation is disconnected, its structured `OAuthConnectionRequired` result includes `oauth_connection_required: true`, a scoped `connect_url` when available, and `requires_host_browser: true` when the URL uses a supported loopback host.
When `connect_url` is provided, present it directly instead of sending the user to the dashboard.
When `requires_host_browser` is true, explain that the loopback URL (`localhost`, `127.0.0.1`, or `::1`) must be opened in a browser on the computer where MindRoom is running, not on a phone or another computer.
After the user connects, have the target agent retry the safe operation or original request.
The dashboard remains a manual alternative only when no `connect_url` is available.

## [`oauth_connections`]

`oauth_connections` lets an agent recover a stuck or revoked MindRoom-managed OAuth connection without exposing broader credential-management controls.

### What It Does

The toolkit exposes only `reset_oauth_connection(provider_id)`.
The provider must back one of the current agent's configured tools.
The call returns a temporary browser link and does not change the connection by itself.
After you confirm, MindRoom removes its saved connection and opens the provider's sign-in page so you can reconnect.
It does not revoke access at the provider itself.

### Configuration

Enable the tool alongside the OAuth-backed tools the agent may recover.

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
| Shared (`shared`) | An administrator or configured credential manager can request the link. Anyone with the complete link can confirm it before it expires. | Everyone using this agent |
| Personal (`user`) | The same MindRoom user who requested the link, signed in to the dashboard | That user's connection across agents |
| Personal for one agent (`user_agent`) | The same MindRoom user who requested the link, signed in to the dashboard | That user's connection for this agent only |

A shared connection belongs to the agent rather than to one MindRoom user.
Anyone who can use the agent can reset their own personal connection, and on a private agent every connection is personal.
Resetting a shared connection requires platform `administrator` access or an entry in `agents.<name>.credential_managers`.
Some providers always use personal connections, regardless of the agent's configured scope.

### Reset A Connection

1. Ask the agent to call `reset_oauth_connection()` for the affected provider.
2. Open the returned link within 10 minutes.
3. Review which agent and connection type will be affected, then confirm the reset.
4. Sign in at the provider and retry the original request.

Keep a shared reset link private.
Anyone with the complete link can confirm it before it expires, and confirming it can disconnect the service for everyone using that agent until reconnection finishes.

### Notes

- `oauth_connections` always runs in the primary MindRoom runtime, even if it appears in `worker_tools`.
- Opening a reset link without confirming it does not change the connection.
- MindRoom refuses an expired, unauthorized, or outdated link before deleting credentials.
- A shared reset link works once. Run `reset_oauth_connection()` again if you need a new one.
- Installation-level connections that are not assigned to an agent scope must be reset from the dashboard.
- Normal `tool_approval` rules still apply when the agent creates the link.

For implementation details and lifecycle guarantees, see [OAuth Credential Lifecycle Design](https://docs.mindroom.chat/dev/oauth-credential-lifecycle-design/#browser-reset).
