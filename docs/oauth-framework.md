---
icon: lucide/key-round
---

# OAuth Integration Framework

MindRoom owns OAuth state, callback handling, credential scoping, and token persistence because those steps decide which human and agent scope receive access to an external account.
Providers supply only provider-specific metadata and parsing behavior, such as OAuth endpoints, scopes, client config services, client environment variables, token response parsing, claim validation, the token credential service name used by OAuth, and the optional tool config service name used by dashboard settings.

The generic API surface is `/api/oauth/{provider}/connect`, `/api/oauth/{provider}/authorize`, `/api/oauth/{provider}/callback`, `/api/oauth/{provider}/status`, and `/api/oauth/{provider}/disconnect`.
Dashboard flows can call `connect` to receive an authorization URL, while conversation flows can show the `authorize` URL so the user opens a normal authenticated MindRoom page before MindRoom redirects to the external provider.
Dashboard OAuth state is opaque, time-limited, single-use, and bound to the authenticated MindRoom user plus the persisted agent execution scope resolved by the existing credentials target machinery.
Conversation OAuth links use an additional opaque, time-limited, single-use connect token that binds the browser flow to the requester that produced the missing-credentials tool result.
That connect token also carries the requester identity from the tool runtime, and MindRoom rejects redemption unless the authenticated dashboard user resolves to the same requester for scoped credentials.
Standalone deployments should set `MINDROOM_OWNER_USER_ID` through pairing so dashboard credential management and agent-issued OAuth links resolve to the owner Matrix user instead of the generic dashboard API-key principal.
`MINDROOM_OWNER_USER_ID` is a single-owner shortcut and is not suitable for a hosted multi-user private-agent deployment.
Hosted deployments that put MindRoom behind an external access layer should enable trusted upstream auth and configure the exact headers MindRoom may trust.
When trusted upstream auth is enabled, MindRoom reads the configured stable user ID and optional email headers into `request.scope["auth_user"]`.
For Matrix-backed private agents, the trusted identity must include a configured Matrix user ID header.
If a browser request cannot map to the requester stored in the conversation connect token, the OAuth authorize or callback path fails closed and no credential is saved.
The access layer must strip any client-supplied copies of the trusted headers before injecting verified values.

Plugins may declare an `oauth_module` in `mindroom.plugin.json`.
That module exposes `register_oauth_providers(settings, runtime_paths)` and returns `OAuthProvider` objects.
This keeps FastAPI routing and state handling in core while still letting plugin authors define provider IDs, scopes, token exchange details, optional claim validators, and tool metadata.

OAuth token writes always go through `resolve_request_credentials_target()` and `save_scoped_credentials()`.
For private agents, the target worker key is derived from the authenticated requester and the agent's saved `worker_scope`, so a user-owned OAuth token lands under the same scope normal tools will read at runtime.
If MindRoom cannot resolve the authenticated dashboard user to the requester carried by a conversation-issued link, the link fails closed and no credential is saved.
Credential placement and visibility policy is centralized in `src/mindroom/credential_policy.py`.
That module owns service classification, OAuth token field filtering, local-only credential service names, and worker-grantable rejections.
Storage, API routing, OAuth provider loading, and worker identity derivation stay in their existing modules.
Tools should declare `auth_provider` and, when credentials are missing, return a concise connect instruction that points at the generic `authorize` route for the provider and agent.
Google OAuth tools always execute in the primary MindRoom runtime so worker runtimes never need Google OAuth client config or user refresh tokens.
OAuth token documents and editable tool setting documents should be separate services.
The OAuth callback writes only the provider's `credential_service`, while dashboard configuration reads and writes the provider's `tool_config_service` when one is declared.
OAuth app client config is stored separately from both of those services.
Providers declare `client_config_services` in lookup order, and MindRoom reads `client_id`, `client_secret`, and optional `redirect_uri` from those services before falling back to environment variables.
Providers can also declare shared client config services for shared app IDs and secrets.
Shared client config services do not supply redirect URIs because each provider must use its own callback route.
Client config services are local-only deployment configuration and cannot be mirrored into worker containers.
Generic credential responses redact `client_secret` for client config services.
Generic credential saves preserve an existing redacted `client_secret` when a client config edit omits that field.
Client config services cannot be copied through the generic copy route.
Generic credentials endpoints do not return OAuth token fields and reject direct writes to OAuth token services.

Identity restrictions are provider settings, not MindRoom policy.
Providers can enforce allowed email domains, allowed hosted-domain claims, and custom claim validators.
If a configured restriction cannot be checked from verified provider claims, the callback fails closed and no credential is saved.

Built-in Google providers use the generic framework for Drive, Calendar, Sheets, and Gmail.
Each provider has minimal service-specific scopes, stores OAuth tokens under its own `*_oauth` service, stores editable tool settings separately, and uses `/api/oauth/*`.
Each provider first checks its provider-specific client config service, then the shared `google_oauth_client` service, then the existing environment variables.
The shared `google_oauth_client` service supplies only `client_id` and `client_secret`; MindRoom derives the provider-specific redirect URI.
