---
icon: lucide/key-round
---

# OAuth Integration Framework

MindRoom owns OAuth state, callback handling, credential scoping, and token persistence because those steps decide which human and agent scope receive access to an external account.
Providers supply only provider-specific metadata and parsing behavior, such as OAuth endpoints, scopes, client config services, optional PKCE requirements, requester-only credential placement, explicitly permitted manual fallback fields and runtime environment names, token response parsing, claim validation, the token credential service name used by OAuth, and the optional tool config service name used by dashboard settings.

The generic API surface is `/api/oauth/{provider}/connect`, `/api/oauth/{provider}/authorize`, `/api/oauth/{provider}/callback`, `/api/oauth/{provider}/success`, `/api/oauth/{provider}/status`, `/api/oauth/{provider}/disconnect`, and the authenticated `GET`/`POST` `/api/oauth/{provider}/reset` confirmation flow.
When a scoped token exists but cannot be decoded, status returns `reset_required: true`, and the dashboard offers the decode-free scoped disconnect path before reconnecting.
Agent-facing OAuth tools return the same structured `reset_required` signal and direct the requester to the authenticated dashboard Integrations page, which supports every credential scope and avoids prescribing an unavailable agent tool or unusable connect link.
Dashboard flows can call `connect` to receive an authorization URL, while conversation flows can show the `authorize` URL so the user opens a normal authenticated MindRoom page before MindRoom redirects to the external provider.
Dashboard OAuth state is opaque, time-limited, single-use, and bound to the authenticated MindRoom user plus the persisted agent execution scope resolved by the existing credentials target machinery.
When an OAuth request targets an agent with `agent_name`, MindRoom also requires the authenticated dashboard requester to match the policy's static `users` entries in `authorization.agent_reply_permissions` for that agent.
Membership in a policy's `joined_rooms` grants conversation access only and never grants OAuth-management access.
Unauthorized agent-scoped OAuth connect, authorize, status, disconnect, and callback requests return HTTP 403 before credentials are exposed or changed.
Conversation OAuth links use an additional opaque, time-limited, single-use connect token that binds the browser flow to the requester that produced the missing-credentials tool result.
That connect token also carries the requester identity from the tool runtime, and MindRoom rejects redemption unless the authenticated dashboard user resolves to the same requester for scoped credentials.
Standalone deployments should set `MINDROOM_OWNER_USER_ID` through pairing so dashboard credential management and agent-issued OAuth links resolve to the owner Matrix user instead of the generic dashboard API-key principal.
`MINDROOM_OWNER_USER_ID` is a single-owner shortcut and is not suitable for a hosted multi-user private-agent deployment.
Hosted deployments that put MindRoom behind an external access layer should enable trusted upstream auth and configure the exact headers MindRoom may trust.
When trusted upstream auth is enabled, MindRoom reads the configured stable user ID and optional email headers into `request.scope["auth_user"]`.
For Matrix-backed private agents, the trusted identity must resolve to a Matrix user ID either from a configured Matrix user ID header or from `MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE`.
The email-to-Matrix template must contain exactly one `{localpart}` placeholder and requires `MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER`.
If a browser request cannot map to the requester stored in the conversation connect token, the OAuth authorize or callback path fails closed and no credential is saved.
The access layer must strip any client-supplied copies of the trusted headers before injecting verified values.

Plugins may declare an `oauth_module` in `mindroom.plugin.json`.
That module exposes `register_oauth_providers(settings, runtime_paths)` and returns `OAuthProvider` objects.
This keeps FastAPI routing and state handling in core while still letting plugin authors define provider IDs, scopes, token exchange details, optional claim validators, and tool metadata.
Plugins can use `OAuthDiscoveryConfig` with `oauth_runtime_bootstrapper()` to resolve protected-resource and authorization-server metadata lazily and optionally register an OAuth client.
Automatic discovery first checks protected-resource metadata at the resource origin and path, then uses the advertised authorization server or falls back to authorization-server metadata at the resource origin.
Dynamic client registration requires a provider-specific `client_config_services` entry and stores generated client configuration only in the primary runtime.

OAuth token writes always resolve the provider's canonical credential target and publish through the OAuth credential lifecycle into that scope's private SQLite store.
On first access, MindRoom adopts a scope's legacy `<credential_service>_credentials.json` token document into a sibling `<credential_service>_credentials.sqlite3` store and removes the legacy file only after its bytes are durably represented or an explicit reset or replacement commits.
If enabling credential encryption leaves a plaintext legacy token unadopted, disabling encryption before reset or replacement lets MindRoom re-adopt that retained token into the unencrypted store.
Back up both files during an upgrade if an immediate rollback may be necessary.
Versions that predate the SQLite lifecycle ignore the new store, so downgrading can expose stale legacy state or require the user to reconnect rather than preserving changes made after adoption.
Providers can declare that credentials follow the requester independently of agent worker reuse.
GitHub uses that policy, so its managed token always lands in the requester's `user` scope and can never fall back to a shared or global token store.
For providers without that policy, private-agent tokens follow the authenticated requester and the agent's saved `worker_scope`, shared-scope agent tokens use a per-agent primary-runtime store, and unscoped agents use the global credential store.
If MindRoom cannot resolve the authenticated dashboard user to the requester carried by a conversation-issued link, the link fails closed and no credential is saved.
Credential placement and visibility policy is centralized in `src/mindroom/credential_policy.py`.
That module owns service classification, OAuth token field filtering, local-only credential service names, and worker-grantable rejections.
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
Built-in Google providers use the generic framework for Drive, Docs, Calendar, Sheets, and Gmail.
Each provider has minimal service-specific scopes, stores OAuth tokens under its own `*_oauth` service, stores editable tool settings separately, and uses `/api/oauth/*`.
Each provider first checks its provider-specific client config service, then the shared `google_oauth_client` service.
The shared `google_oauth_client` service supplies only `client_id` and `client_secret`; MindRoom derives the provider-specific redirect URI.

OAuth-backed remote MCP servers also use the generic framework.
MindRoom synthesizes an OAuth provider from each `mcp_servers.*.auth.type: oauth` config entry and exposes it through the same `/api/oauth/{provider}/*` routes.
Generated MCP OAuth token services use the `<provider_id>_oauth` naming pattern; the default provider ID is already `mcp_<server_id>`.
Custom provider IDs that do not start with `mcp_` get an `mcp_` credential-service prefix.
These token services stay in the primary runtime credential store.
The matching MCP toolkit always loads the token for the current canonical requester from a `user` credential scope before opening the remote MCP transport.
Generated MCP OAuth credentials and sessions remain requester-isolated even when the agent's execution worker uses `shared` or `user_agent`; `worker_scope` still controls worker-runtime reuse, not the MCP OAuth credential identity.
Generated MCP OAuth providers can use public clients with `token_endpoint_auth_method: none`, PKCE, and empty scope lists.
Generated MCP OAuth providers can also discover protected-resource metadata and authorization-server metadata lazily when the first OAuth flow starts.
If the authorization server advertises dynamic client registration and no client config is stored yet, MindRoom registers a public client and persists the returned registration metadata in the generated OAuth client config service.
