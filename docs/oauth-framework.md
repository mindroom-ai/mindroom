---
icon: lucide/key-round
---

# OAuth Integration Framework

MindRoom owns OAuth state, callback handling, credential scoping, and token persistence because those steps decide which human and agent scope receive access to an external account.
Providers supply only provider-specific metadata and parsing behavior, such as OAuth endpoints, scopes, client environment variables, token response parsing, claim validation, and the credential service name used by tools.

The generic API surface is `/api/oauth/{provider}/connect`, `/api/oauth/{provider}/authorize`, `/api/oauth/{provider}/callback`, `/api/oauth/{provider}/status`, and `/api/oauth/{provider}/disconnect`.
Dashboard flows can call `connect` to receive an authorization URL, while conversation flows can show the `authorize` URL so the user opens a normal authenticated MindRoom page before MindRoom redirects to the external provider.
Dashboard OAuth state is opaque, time-limited, single-use, and bound to the authenticated MindRoom user plus the persisted agent execution scope resolved by the existing credentials target machinery.
Conversation OAuth links use an additional opaque, time-limited, single-use connect token that binds the browser flow to the exact worker credential target that produced the missing-credentials tool result.

Plugins may declare an `oauth_module` in `mindroom.plugin.json`.
That module exposes `register_oauth_providers(settings, runtime_paths)` and returns `OAuthProvider` objects.
This keeps FastAPI routing and state handling in core while still letting plugin authors define provider IDs, scopes, token exchange details, optional claim validators, and tool metadata.

Credential writes always go through `resolve_request_credentials_target()` and `save_scoped_credentials()`.
For private agents, the target worker key is derived from the authenticated requester and the agent's saved `worker_scope`, so a user-owned OAuth token lands under the same scope normal tools will read at runtime.
Tools should declare `auth_provider` and, when credentials are missing, return a concise connect instruction that points at the generic `authorize` route for the provider and agent.

Identity restrictions are provider settings, not MindRoom policy.
Providers can enforce allowed email domains, allowed hosted-domain claims, and custom claim validators.
If a configured restriction cannot be checked from verified provider claims, the callback fails closed and no credential is saved.

Google Drive is the first built-in provider.
It uses the generic framework with Drive read scopes for file search and read workflows, stores credentials under the `google_drive` service, and does not reuse the legacy all-Google `/api/google/*` scope bundle.
The legacy Google Services routes remain available for the existing dashboard integration while new providers use `/api/oauth/*`.
