# MCP Gateway

The optional MCP gateway lets external MCP clients, such as desktop AI apps, use MindRoom agents' tools through one endpoint at `/mcp`.
Each signed-in user sees only the agents they are authorized to use and have selected in the [Connections portal](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#connections-portal), and only those agents' compatible tools.
Calls run with the selected agent's credential scope, tool filters, and worker routing, so each user connects only the services they need.
The gateway exposes tools only; it does not run the agent's model or load its prompt, memories, or conversation history, so use MindRoom chat to talk to agents.

## How clients use the gateway

The gateway offers exactly three MCP tools:

| Operation | Purpose |
|-----------|---------|
| `search_tools` | Search assigned integrations, or select an agent and toolkit to search its functions |
| `get_tool` | Fetch the input schema for one selected function |
| `invoke_tool` | Run that function with the selected agent's connections |

A client starts with `search_tools` and `{}` (or a query such as `{"query": "calendar"}`) to discover agent/toolkit pairs.
It passes a returned `agent` and `toolkit` to another `search_tools` call to list functions, then calls `get_tool` with the returned `agent`, `toolkit`, and `function`, and finally `invoke_tool` with the same selectors and an `arguments` object.
Clients must copy identifiers from these results; display names and agent directories such as `list_agents` do not show which agents the gateway exposes.
MCP initialization instructions name the configured personal agent from `MINDROOM_CONNECTIONS_AGENT` and the instance's `/connections` URL, but the personal agent is usable only when it appears in discovery.

When a service needs authorization, the result contains `error.code: connection_required` and a `connection_url` pointing to `/connections`.
The user connects that service in the browser and the client retries.
An unconnected or unavailable integration does not block discovery or use of other integrations.
Neither the gateway nor clients should automatically retry a failed or timed-out `invoke_tool` call, because the action may already have taken effect.

## Enable the gateway

First configure the [Connections portal](https://docs.mindroom.chat/deployment/trusted-upstream-auth/#connections-portal), including strict signed upstream authentication (`MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED=true` and `MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT=true`) and a private personal agent.
Then enable the gateway:

```bash
MINDROOM_CONNECTIONS_AGENT=personal
MINDROOM_PUBLIC_URL=https://assistant.example.org
MINDROOM_MCP_GATEWAY_ENABLED=true
```

The gateway is disabled by default and stays disabled unless all of these settings are present.
`MINDROOM_PUBLIC_URL` must be an HTTPS origin without a path, query, or fragment; loopback HTTP origins are accepted for local development.
Restart the API after changing any gateway environment variable.
Until then, gateway routes return `404 MCP gateway is disabled`.
If a gateway setting is invalid, the API logs `MCP gateway disabled: check public origin, limits, authentication, and account provisioning` and starts without the gateway.

Agent eligibility follows the agent access rules described for the Connections portal.
`access.current_room_members` alone does not grant MCP access because MCP requests have no current Matrix room, and credential management alone does not grant tool execution.
Users can address only agents in their own saved selection and cannot act as another user or credential owner.
Access changes, including revoked grant-room membership, apply to the next call without a configuration change; calls already running may finish.

## Choose exposed agents and tools

Users choose what the gateway exposes on the Connections page, which lists agents in a searchable table with filters for personal, shared, and MCP-enabled agents.
Agents a user can only manage credentials for have no MCP selection controls.
Expand an agent to see each tool with a connection, and expand **Other tools** for tools that need no setup.
Under **MCP gateway**, select **All tools** for an agent or choose tools individually.

- Each selection applies to one agent, even when another agent has the same tool.
- An individual selection covers a whole toolkit or MCP server, not single functions inside it.
- **All tools** includes compatible tools assigned to that agent later; a custom selection includes only the checked tools.
- The eligible personal agent starts selected; shared agents start off, and new agents are not added to an existing selection.
- Turning every agent off is allowed and persists.
- Losing access to an agent or tool hides its saved choice; if the user has not saved a new selection in the meantime, restoring access brings the choice back.
- One saved selection applies to all of a user's MCP clients and changes no other user's selection or connections.
- Removing an agent or tool still works when stored data exceeds a lowered storage quota.

Tools that share a service connection share its OAuth controls, and room-dependent tools are labeled **MindRoom only** and are not exposed.
A shared agent's configured worker and credential scopes still decide whose service connection runs a call.
When managed account provisioning is enabled, users without an active provisioned account see an access notice instead of MCP controls until an administrator provisions them; their ordinary connections remain available.

## Route browser and machine traffic

These routes apply to the default `MINDROOM_MCP_AUTH_MODE=builtin`; for externally issued credentials, see [External authorization](#external-authorization), where `/mcp` and protected-resource discovery are unchanged.
Forward these paths to the MindRoom API:

| Paths | Authentication at the access proxy |
|-------|------------------------------------|
| `/connections`, `/connections/*`, `/api/connections`, `/api/connections/*`, existing `/api/oauth/*` | Existing signed browser authentication |
| `/mcp` | Pass through to gateway bearer authentication |
| `/mcp/oauth/authorize`, `/mcp/oauth/register`, `/mcp/oauth/token`, `/mcp/oauth/revoke` | Public OAuth endpoints; pass through |
| `/.well-known/oauth-authorization-server/mcp/oauth`, `/.well-known/oauth-protected-resource/mcp` | Public client discovery |
| `/mcp/oauth/.well-known/oauth-authorization-server` | Compatibility discovery alias |

Browser consent lives at `/connections/mcp/authorize`, inside the authenticated browser prefix, so keep it out of any other frontend's service-worker navigation fallback.
Machine endpoints must receive MCP and OAuth responses, not an access proxy's HTML login page.
Strip client-supplied trusted identity headers at the proxy, including on machine paths.
Preserve the public `Host` header and the `Authorization`, `Origin`, `Accept`, `Content-Type`, and `MCP-Protocol-Version` headers.
Forward the exact `/mcp` path without adding a trailing slash.

Native clients can omit `Origin`.
To allow a browser-based MCP client, list its exact origin:

```bash
MINDROOM_MCP_GATEWAY_ALLOWED_ORIGINS=https://client.example.org,http://localhost:6274
```

The gateway's public origin is allowed automatically.
Additional origins must be HTTPS or loopback HTTP; wildcards are rejected.
Cross-origin MCP requests never carry browser cookies and do not grant access to dashboard APIs.
The public registration, token, revocation, and discovery endpoints accept noncredentialed cross-origin requests.

## Connect an MCP client

Configure a Streamable HTTP server with URL `https://assistant.example.org/mcp`.
In built-in mode, the client must support OAuth discovery and dynamic registration; a static bearer-only configuration screen cannot perform the initial login.
In [external mode](#external-authorization), clients follow the issuer's flow instead.
The built-in flow uses the OAuth authorization code grant with PKCE S256 and dynamic public-client registration:

1. Read the resource metadata URL from the gateway's `401` bearer challenge.
2. Discover the authorization server at issuer `https://assistant.example.org/mcp/oauth`.
3. Register with `token_endpoint_auth_method: none`, `response_types: [code]`, and both `authorization_code` and `refresh_token` in `grant_types`.
4. Request scope `mcp:tools` and exact resource `https://assistant.example.org/mcp`.
5. Open the authorization URL, sign in through the existing browser login, and approve the named client.
6. Exchange the code with its original callback, PKCE verifier, and exact resource, then send the access token as a bearer on MCP requests.

Code exchange and refresh both require the exact resource.
Client callbacks must be registered HTTPS URLs or loopback HTTP URLs.
Dashboard API keys, browser cookies, and unsigned identity headers do not authenticate `/mcp`.
Client names are self-declared, so users should check the displayed client address before approving.

The gateway speaks the Python MCP SDK 1.x Streamable HTTP protocol and is tested with protocol version `2025-11-25`.
It uses stateless requests with JSON responses and does not offer resumable SSE sessions, resources, or prompts.

## External authorization

External mode accepts signed JWT access tokens from one configured authority instead of MindRoom's built-in OAuth server.
It requires [managed account provisioning](#managed-account-provisioning), and every request also checks for an active provisioned account, current agent eligibility, and the user's saved selection.

```bash
MINDROOM_MCP_AUTH_MODE=external
MINDROOM_MCP_EXTERNAL_AUTHORIZATION_SERVER=https://identity.example.org
MINDROOM_MCP_EXTERNAL_ISSUER=https://identity.example.org
MINDROOM_MCP_EXTERNAL_AUDIENCE=https://assistant.example.org/mcp
MINDROOM_MCP_EXTERNAL_JWKS_URL=https://identity.example.org/.well-known/jwks.json
MINDROOM_MCP_EXTERNAL_MATRIX_USER_ID_CLAIM=matrix_user_id
MINDROOM_MCP_SCIM_TOKEN=<at least 32 random characters>
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `MINDROOM_MCP_AUTH_MODE` | `builtin` | `builtin` or `external` |
| `MINDROOM_MCP_EXTERNAL_AUTHORIZATION_SERVER` | required | Issuer advertised in protected-resource metadata; its discovery document supplies the client's OAuth endpoints |
| `MINDROOM_MCP_EXTERNAL_ISSUER` | required | Required `iss` value; may differ from the authorization server, and significant trailing slashes matter |
| `MINDROOM_MCP_EXTERNAL_AUDIENCE` | required | Required `aud`; use an audience dedicated to this MCP resource, not a client ID or browser application audience |
| `MINDROOM_MCP_EXTERNAL_JWKS_URL` | required | Signing keys |
| `MINDROOM_MCP_EXTERNAL_TOKEN_HEADER` | `Authorization` | Header carrying the token; `Authorization` expects `Bearer <JWT>`, other headers carry the raw JWT |
| `MINDROOM_MCP_EXTERNAL_EMAIL_CLAIM` | `email` | Claim holding the user's email |
| `MINDROOM_MCP_EXTERNAL_MATRIX_USER_ID_CLAIM` | unset | Claim holding the Matrix user ID |
| `MINDROOM_MCP_EXTERNAL_EMAIL_DOMAIN` | unset | Only accepted email domain for the email-template mapping |
| `MINDROOM_MCP_EXTERNAL_EMAIL_TO_MATRIX_USER_ID_TEMPLATE` | unset | Matrix ID template such as `'@{localpart}:example.org'` |
| `MINDROOM_MCP_EXTERNAL_REQUIRED_SCOPES` | unset | Whitespace-separated scopes the signed `scope` claim must contain |
| `MINDROOM_MCP_EXTERNAL_CLIENT_ID` | unset | Exact signed `client_id` claim to require |

The authorization server, issuer, and JWKS URLs must be HTTPS without credentials, queries, or fragments.
Configure exactly one identity mapping: either the Matrix user ID claim, or the email template together with the email domain.
With the email mapping, the complete signed email must match the SCIM `userName` exactly, including case.

Tokens must be RS256 or ES256 JWTs with signed `iss`, `aud`, `exp`, `iat`, `sub`, and the email claim.
Opaque tokens, token introspection, and OIDC ID tokens are unsupported.
No scope is required unless `MINDROOM_MCP_EXTERNAL_REQUIRED_SCOPES` is set; a token missing a required scope gets HTTP 403.
Unsigned headers, browser cookies, owner API keys, and built-in gateway tokens cannot replace external credentials.

External mode disables MindRoom's OAuth registration, authorization, consent, token, revocation, and client-management endpoints and controls.
Agent selection on Connections still applies to all of the user's externally authenticated clients.
Manage client authorizations at the issuer; provider connections in the portal stay separate.
Because external mode shares the account store, unused `MINDROOM_MCP_OAUTH_*` settings must keep valid values.

Shared issuer keys, an operator-chosen audience, or an email domain do not isolate tenants.
Rely on external mode only when the issuer gives this resource exclusive resource and identity namespaces, or when it guarantees the signed user's identity within your tenant and you set `MINDROOM_MCP_EXTERNAL_CLIENT_ID` to a [JWT access-token `client_id`](https://www.rfc-editor.org/rfc/rfc9068.html#section-2.2) that other tenants cannot select or override.
A pinned client must use that preregistered client ID and support the authorization server's discovery and authorization flow.

### Direct OAuth and signed proxy assertions

Direct JumpCloud JWT authentication is unverified and not recommended.
[JumpCloud's OIDC guide](https://jumpcloud.com/support/sso-with-oidc) guarantees subject mapping only for ID tokens, and its shared regional issuer does not by itself isolate tenants.
Put JumpCloud behind an OAuth-aware proxy that enforces the tenant's login policy and sends signed assertions instead.

An OAuth-aware proxy can send a raw signed JWT in the header named by `MINDROOM_MCP_EXTERNAL_TOKEN_HEADER`.
For example, [Cloudflare Managed OAuth](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/) (beta) uses `Cf-Access-Jwt-Assertion`; configure that header, the assertion issuer and JWKS, the MCP Access application's audience, and the OAuth discovery issuer.
The edge must own OAuth, enforce the MCP policy, replace client-supplied assertions, and prevent origin bypass.
Do not keep a bypass policy on `/mcp` or combine Managed OAuth with built-in mode.
SCIM still needs its own authenticated route without interactive login redirects.

### External lifecycle and validation

After an account is created, renamed, migrated, reactivated, or deleted and recreated, the gateway accepts only JWTs issued more than 60 seconds after that event, so clients may need to fetch a new token after waiting; profile-only updates keep access.
Keep the issuer and gateway clocks synchronized; tokens with a future `iat` are rejected.
Signing-key revocation can take up to 60 seconds to apply.
The issuer owns refresh revocation and login policy, so after reactivation a client may renew silently without an interactive login.
The per-user concurrency limit spans all of a user's tokens, while the per-grant limit and cancellation follow the exact token, so cancelling a call requires the token that started it.

Hosted identity-provider login and connector delivery have not been verified end to end.
Before general access, test real client login and resource handling, two users' tools, disabling a user with a connected client, refresh rejection, and reactivation against the intended service.

## Access and lifecycle

This section covers built-in mode client grants; agent selection and call limits apply in both modes.

Access tokens last up to 15 minutes, and refresh tokens rotate on use; reusing a refresh token revokes the whole grant.
A grant expires after its idle lifetime without a successful refresh or tool use, and at its absolute lifetime after approval, which continued use never extends.
Background token refresh counts as activity, so a client that keeps refreshing stays connected until the absolute deadline.
Portal visits, failed calls, and invalid tokens do not extend the idle lifetime.

| Setting | Default | Meaning |
| --- | --- | --- |
| `MINDROOM_MCP_OAUTH_IDLE_TTL_DAYS` | `30` | Maximum days without successful refresh or tool use |
| `MINDROOM_MCP_OAUTH_GRANT_TTL_DAYS` | `180` with provisioning, otherwise `30` | Absolute lifetime in days from approval |
| `MINDROOM_MCP_SCIM_TOKEN` | unset | Provisioning bearer secret, at least 32 characters; enables managed accounts |

Lifetimes must be positive whole days, the idle lifetime cannot exceed the absolute lifetime, and the absolute lifetime cannot exceed 365 days.
Absolute lifetimes above 30 days require managed account provisioning, so that disabling an account can revoke long-lived access.
After you enable provisioning, existing clients without a provisioned account must be approved again once.

The Connections portal lists each authorized client with its address, last tool use, and expiry.
Users can disconnect one client or all clients without disconnecting their upstream service accounts, and disconnecting all also cancels their pending approvals.
A disconnected client can request fresh consent, and users can still manage clients after losing agent tool permission.
The OAuth revocation endpoint revokes the whole client grant; revoking with an expired access token may succeed without effect, so use the portal or an unexpired refresh token.
Browser logout does not revoke gateway grants.

Gateway OAuth state and user selections are stored in `mcp_gateway/oauth.sqlite3` under the storage root; keep that directory private and persistent across restarts.
Tokens, codes, and nonces are stored only as hashes, and upstream provider credentials stay in the scoped credential store.
Run a single API process for the gateway; multiple independently routed replicas are not supported.

## Limits

Each call has a 60-second deadline and three concurrency limits:

| Variable | Default | Scope |
| --- | --- | --- |
| `MINDROOM_MCP_GATEWAY_MAX_ACTIVE_CALLS` | `128` | All active calls in the API process |
| `MINDROOM_MCP_GATEWAY_MAX_USER_CALLS` | `16` | One user across all clients |
| `MINDROOM_MCP_GATEWAY_MAX_GRANT_CALLS` | `16` | One authorized client connection |

Each value must be a positive integer; zero does not mean unlimited.
The limits apply to all three operations, and a call over any limit fails immediately with `busy` instead of queueing.
A cancelled or timed-out call keeps its allowance until its tool work and cleanup finish.
The defaults are conservative starting values; tune them to available resources, tool latency, and upstream quotas.
Gateway tool work runs on 32 dedicated threads, and calls beyond that wait within their deadline, so keep `MINDROOM_MCP_GATEWAY_MAX_USER_CALLS` below 32 so one user cannot occupy them all.

Size limits:

- Search returns at most 10 results and 16 KiB.
- A tool schema is limited to 32 KiB, and tool arguments and results to 64 KiB each.
- HTTP request bodies and MCP responses are limited to 128 KiB.

Public client registration and authorization starts are rate limited per API process:

| Variable | Default | Meaning |
| --- | --- | --- |
| `MINDROOM_MCP_GATEWAY_ONBOARDING_RATE_LIMIT` | `60` | Requests per minute from all sources |
| `MINDROOM_MCP_GATEWAY_ONBOARDING_SOURCE_RATE_LIMIT` | `10` | Requests per minute from one client IP |

Both values must be positive integers; zero does not remove the limit, it disables the gateway.
Keep the source limit below the aggregate limit.
Excess requests get HTTP 429 with `Retry-After`; token exchange, refresh, revocation, discovery, and existing grants are unaffected.
The source is the client IP reported by the ASGI server, not forwarding headers.
Behind a proxy, set Uvicorn's `FORWARDED_ALLOW_IPS` to only your trusted proxies and have them sanitize the forwarded client address; otherwise every caller behind the proxy shares one allowance.
Users behind the same NAT also share an allowance.
These limits provide fair admission, not protection against distributed denial-of-service attacks.

Stored gateway state has logical size budgets:

| Variable | Default | Covers |
| --- | --- | --- |
| `MINDROOM_MCP_GATEWAY_ONBOARDING_MAX_BYTES` | `67108864` (64 MiB) | Pending consent and registered clients without a live grant |
| `MINDROOM_MCP_OAUTH_MAX_BYTES` | `268435456` (256 MiB) | All retained OAuth state and selections |
| `MINDROOM_MCP_OAUTH_USER_MAX_BYTES` | `16777216` (16 MiB) | One user's selection, grants, and clients |

Each value is a positive integer in bytes.
When the onboarding budget is full, registration returns HTTP 503 with `Retry-After: 60` and authorization returns OAuth `temporarily_unavailable`.
When a durable budget is full, token issuance and consent return HTTP 503 with `Retry-After: 60` and `temporarily_unavailable`, while the existing refresh token or code stays usable.
Space is freed as grants are revoked or expire, which can take longer than the retry interval, and lowering a budget can block refreshes until usage falls below it.
Registrations without a grant or pending consent expire after 24 hours.
A grant can issue at most six token pairs in any 60-second window, including the initial code exchange.
These budgets do not cap the physical database file; use a filesystem or volume quota for a hard disk bound.

## Managed account provisioning

Managed account provisioning lets an identity provider control who may connect MCP clients, and is required for external mode and for grant lifetimes above 30 days.
Set a dedicated `MINDROOM_MCP_SCIM_TOKEN` and expose `/mcp/scim/v2` to the identity provider over HTTPS, without interactive login redirects or browser CORS access.
Do not share the provisioning secret with MCP clients or browser applications.
In a compatible custom SCIM connector, use the base URL `https://assistant.example.org/mcp/scim/v2` with that bearer token.

The endpoint implements a restricted SCIM 2.0 User profile: create, list, read, replace, PATCH, and delete, plus service-provider and schema discovery.
List filters support only one exact `eq` comparison on `userName`, `emails.value`, or `id`, such as `userName eq "ada@example.org"`; other filters, including `externalId` and compound filters, return `400 invalidFilter`.
Create and replace requests must include a boolean `active`; omitting it returns `400 invalidValue`.
Create and replace requests may also declare the Enterprise User extension, whose data is ignored; other extensions are rejected.
Group management, bulk operations, password management, and sorting are unsupported, so disable group management in the connector.
PATCH supports `userName`, `active`, `displayName`, `externalId`, `name`, `emails`, `name.givenName`, `name.familyName`, and `name.formatted`.
Replace emails through the whole `emails` attribute; filtered paths such as `emails[type eq "work"].value` return `400 invalidPath`, and an unsupported operation rolls back the whole PATCH.
Rejected schema declarations log `SCIM schema validation failed` with the expected and recognized schemas, which helps spot SCIM 1.1 payloads or wrong schemas.

Set the connector's `userName` to the user's email exactly as verified by browser login in built-in mode or by the signed token in external mode, including case.
Provisioning does not authenticate users or grant tool permissions.
Unknown or inactive accounts cannot authorize clients.
Deactivating, renaming, or deleting an account removes all its client grants and pending consent; reactivation allows new approvals but never restores old grants, and a deleted and recreated account does not get its previous selection back.
Deactivation applies when MindRoom receives the update, so monitor connector delivery; provider actions already started cannot be undone.
Provisioning does not deactivate Matrix accounts, erase upstream service credentials, or log out browser sessions.

To test offboarding in built-in mode, provision an active test user, approve an MCP client through the portal, deactivate the user, and check that token refresh, tool use, and new consent are rejected.
Test the connector's real update and deactivation payloads against this profile before relying on it.

## Error codes

Tool errors arrive as an `error` object with a `code` inside a successful MCP response.

| Code | Meaning and fix |
| --- | --- |
| `connection_required` | Open `connection_url` and connect the service, then retry |
| `tool_not_found` | The identifier is not assigned or selected; repeat discovery, and if a shared-agent tool is missing, check the agent and tool selections in Connections |
| `invalid_arguments` | Arguments do not match the schema or exceed 64 KiB |
| `approval_required` | The tool needs confirmation or a configured approval and cannot run through the gateway |
| `unauthorized` | Agent access or selection changed; check Connections |
| `tool_unavailable` | The tool could not be loaded or failed; its outcome may be unknown |
| `schema_too_large`, `result_too_large` | The schema or result exceeds the size limit |
| `busy` | A concurrency limit is full |
| `duplicate_request` | A call with the same request ID is already running |
| `timeout`, `cancelled` | The call stopped waiting; the action may still have taken effect |

Tools that need the live Matrix room, such as Matrix messaging, scheduling, and conversation attachments, are never exposed.
Explicit MCP cancellation applies only to a matching request ID from the same client.
Cancellation cannot forcibly stop synchronous tool work, so a stuck provider call can hold capacity and delay shutdown until the process supervisor terminates the API.

## Inbound request diagnostics

These log events describe inbound `/mcp` traffic, separately from outbound MCP integration logs, and omit credentials, arguments, queries, and results.

- `mcp_gateway_http_completed` records the HTTP status, elapsed milliseconds, and authenticated `requester_id` when available.
- `mcp_gateway_call_completed` records the operation, elapsed milliseconds, outcome, and error code; an HTTP 200 can still carry a tool error.
- `mcp_gateway_call_failed` records unexpected failures with the exception type.
- `mcp_gateway_agent_selection` records whether the requested agent is eligible, saved, selected, or matches an eligible name ignoring case, plus eligible and selected agent counts.

Join these events by the server-generated `request_id`, which differs from the client's JSON-RPC ID.
In `mcp_gateway_agent_selection`, `agent_eligible=false` with `agent_case_match=true` means a case mismatch, and `agent_eligible=true` with `agent_selected=false` means the user has not selected that agent.
A zero `selected_agent_count` explains empty discovery.
A 400 in `mcp_gateway_http_completed` with `unsupported_protocol_version=true` means the client asked for an MCP revision the gateway does not support.
It is logged at info because compatible clients send this as a probe and then fall back to a supported handshake; if no successful request from the same requester follows, that client cannot connect.
